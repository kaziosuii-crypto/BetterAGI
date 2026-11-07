"""UT harness: window discovery, client-area capture, keyboard input, save management.

Pure ctypes (win32 user32/gdi32), no third-party deps. Designed to fail loudly:
every call returns structured dicts and raises HarnessError with the Win32 error code.
"""
import ctypes, ctypes.wintypes as wt, io, json, os, sys, time, argparse

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)

class HarnessError(RuntimeError):
    pass


def _err(what):
    return HarnessError("%s failed: winerr=%s" % (what, ctypes.get_last_error()))


# ── 平台安全红线 ──────────────────────────────────────────────────────────
# 这些虚拟键**永不下发**，不管上层是模型、脚本还是人手误触。
# 0x73 = F4：Undertale 里是切换全屏，直播时会直接黑屏/串画面，属于事故级操作。
# 闸口设在这里（唯一的发送层）而不是调用方，是为了让绕过成为不可能：
# 上层可以忘记检查，但 _key / post_vk / press_vks 每次发送都要过这一道。
_FORBIDDEN_DEFAULT = {0x73}


def forbidden_vks():
    """禁键集合：内置红线 + 环境变量 BETTERAGI_FORBIDDEN_VKS（逗号分隔，支持 0x 前缀）。"""
    vks = set(_FORBIDDEN_DEFAULT)
    for piece in (os.environ.get("BETTERAGI_FORBIDDEN_VKS") or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            vks.add(int(piece, 0))
        except ValueError:
            continue
    return vks


def is_forbidden(vk):
    try:
        return int(vk) in forbidden_vks()
    except (TypeError, ValueError):
        return True  # 认不出的按键码一律不放行


def enum_windows():
    out = []
    CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, lp):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value.strip():
            pid = wt.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            r = wt.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            out.append({"hwnd": int(hwnd), "title": buf.value,
                        "pid": int(pid.value),
                        "rect": [r.left, r.top, r.right, r.bottom]})
        return True

    if not u32.EnumWindows(CB(cb), 0):
        raise _err("EnumWindows")
    return out


def _exe_of(pid):
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(h)


def find_window(substr, exe=None):
    """Find the newest visible window whose title contains substr (and exe matches, case-insensitive)."""
    hits = []
    for w in enum_windows():
        if substr.lower() not in w["title"].lower():
            continue
        if exe:
            path = _exe_of(w["pid"])
            if exe.lower() not in path.lower():
                continue
            w["exe"] = path
        hits.append(w)
    return hits[-1] if hits else None


def post_vk(hwnd, vk, hold_ms=16):
    """Send a key with WM_KEYDOWN/WM_KEYUP instead of SendInput.

    实测（2026-11-04）：没有前台权限时 SendInput 全部丢给别的窗口，游戏一点反应都没有；
    而 PostMessage 到游戏窗口的消息队列**不需要前台**（EWMH 语义也成立），
    Undertale 实测用它能从过场画面推进到下一幕。所以这条路径是前台抢不到时的兜底。
    """
    if is_forbidden(vk):
        return False
    hwnd = ctypes.c_void_p(int(hwnd)) if not isinstance(hwnd, ctypes.c_void_p) else hwnd
    # PostMessageW 的 lParam 是 **有符号** LPARAM：keyup 的 0xC0000001 位模式
    # 必须按 c_ssize_t 传负值，否则 ctypes 默认 int 会报 OverflowError（实测踩过）。
    u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    u32.PostMessageW.restype = ctypes.c_bool
    scan = (u32.MapVirtualKeyW(vk, 0) & 0xFF) << 16
    down = 1 | scan
    up = down | (1 << 30) | (1 << 31)
    u32.PostMessageW(hwnd, 0x0100, vk, down)
    if hold_ms:
        time.sleep(max(0, hold_ms) / 1000.0)
    u32.PostMessageW(hwnd, 0x0101, vk, up)
    return True


def post_vks(hwnd, vks, hold_ms=16):
    """Post several keys to the window queue in order (no foreground needed)."""
    for vk in list(vks):
        post_vk(hwnd, vk, hold_ms)


def top_window_of_pid(pid):
    """Pick the largest visible window owned by pid (the screen-holding helper's main window)."""
    best = None
    for w in enum_windows():
        if w["pid"] != int(pid):
            continue
        r = w["rect"]
        area = max(0, r[2] - r[0]) * max(0, r[3] - r[1])
        if best is None or area > best[0]:
            best = (area, w)
    return best[1]["hwnd"] if best else None


def client_rect(hwnd):
    r = wt.RECT()
    if not u32.GetClientRect(hwnd, ctypes.byref(r)):
        raise _err("GetClientRect")
    pt = wt.POINT(0, 0)
    if not u32.ClientToScreen(hwnd, ctypes.byref(pt)):
        raise _err("ClientToScreen")
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


def window_rect(hwnd):
    """整个窗口（含边框）的屏幕坐标 (x, y, w, h)。"""
    r = wt.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise _err("GetWindowRect")
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def crop_to_window(img, hwnd, pad=0):
    """把整屏截图裁成这一个窗口的区域。

    为什么要裁：PrintWindow 在窗口被遮住/移出屏幕时会**混进桌面像素**——实测
    「外面是一整屏不同内容、中间只有一块是游戏画面」，照这种图去推理，模型会把
    别人的桌面当成游戏剧情。裁到窗口矩形后，模型看到的就只是这个游戏。
    """
    if img is None:
        return img
    try:
        x, y, w, h = window_rect(hwnd)
    except Exception:  # noqa: BLE001
        return img
    sw, sh = img.size
    left = max(0, int(x) - pad)
    top = max(0, int(y) - pad)
    right = min(sw, int(x + w) + pad)
    bottom = min(sh, int(y + h) + pad)
    if right <= left or bottom <= top:
        return img
    return img.crop((left, top, right, bottom))


def pylisten_pid():
    """PID of the pylisten helper (holds ASFW_ANY so agents can grant foreground)."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_diag", "pylisten_pid.txt")) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def grant_foreground():
    """Allow this process to SetForegroundWindow (called by agents that do not own a GUI)."""
    p = pylisten_pid()
    if not p:
        return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x0400 | 0x0010, False, p)  # QUERY_INFORMATION|VM_OPERATION
    if not h:
        return False
    try:
        addr = ctypes.c_void_p(0xFFFFFFFF)  # ASFW_ANY
        ok = k32.WriteProcessMemory(h, ctypes.c_void_p(0x02D0), ctypes.byref(addr), ctypes.sizeof(addr), None)
        return bool(ok)
    finally:
        k32.CloseHandle(h)


def sethwnd_foreground(hwnd, timeout_ms=3000):
    """Robust focus: temporarily join the foreground thread's input queue, then SetForegroundWindow."""
    SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
    SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
    SPIF_SENDCHANGE = 0x02
    old = wt.DWORD()
    u32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(old), 0)
    u32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(wt.DWORD(0)), SPIF_SENDCHANGE)
    try:
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, 9)
        fg = u32.GetForegroundWindow()
        tid_fg = u32.GetWindowThreadProcessId(fg, None)
        tid_me = ctypes.windll.kernel32.GetCurrentThreadId()
        target_tid = u32.GetWindowThreadProcessId(hwnd, None)
        u32.AttachThreadInput(tid_me, tid_fg, True)
        u32.AttachThreadInput(tid_me, target_tid, True)
        u32.BringWindowToTop(hwnd)
        ok = u32.SetForegroundWindow(hwnd)
        u32.AttachThreadInput(tid_me, target_tid, False)
        u32.AttachThreadInput(tid_me, tid_fg, False)
        return bool(ok)
    finally:
        u32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(old), SPIF_SENDCHANGE)


def force_foreground(hwnd):
    """Alt-key trick: a synthetic Alt keypress makes the OS allow the next SetForegroundWindow."""
    _send_input(KEYBDINPUT(0x12, 0, 0, 0, None))   # Alt down
    _send_input(KEYBDINPUT(0x12, 0, 0x0002, 0, None))  # Alt up
    time.sleep(0.05)
    return bool(u32.SetForegroundWindow(hwnd)) or sethwnd_foreground(hwnd)


def focus(hwnd, tries=3):
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
    for _ in range(tries):
        if u32.SetForegroundWindow(hwnd):
            return True
        if not grant_foreground():
            break
        time.sleep(0.2)
    if not u32.SetForegroundWindow(hwnd):
        # foreground lock: attach to the foreground thread and retry once
        fg = u32.GetForegroundWindow()
        t_fg = u32.GetWindowThreadProcessId(fg, None)
        t_me = ctypes.windll.kernel32.GetCurrentThreadId()
        u32.AttachThreadInput(t_me, t_fg, True)
        ok = u32.SetForegroundWindow(hwnd)
        u32.AttachThreadInput(t_me, t_fg, False)
        if not ok:
            raise _err("SetForegroundWindow")
    return u32.GetForegroundWindow() == hwnd


def dpi_of(hwnd):
    try:
        return int(ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd)))
    except Exception:  # noqa: BLE001
        return 96


def unminimize(hwnd):
    """窗口被最小化时先还原（SW_RESTORE，不是 SW_MAXIMIZE）。

    最小化状态下 Windows 把窗口挪到 (-21333,-21333) 且客户区为 0x0，
    实测就是这一条让 capture 报 "client rect empty: 0x0"。
    还原只是回到原尺寸，不会最大化、不会全屏。
    """
    hwnd = hwnd if isinstance(hwnd, ctypes.c_void_p) else ctypes.c_void_p(hwnd)
    if not u32.IsIconic(hwnd):
        return {"was_minimized": False}
    u32.ShowWindow(hwnd, 9)  # SW_RESTORE
    time.sleep(0.35)
    x, y, w, h = client_rect(hwnd)
    return {"was_minimized": True, "restored_to": [w, h], "minimized_now": bool(u32.IsIconic(hwnd))}


def fit_client_size(hwnd, width, height, tolerance=2, native=None):
    """把窗口客户区调整成 width x height。

    只做 SetWindowPos 改尺寸，**绝不最大化、绝不切全屏**——全屏会改游戏渲染模式，
    也会把用户桌面吞掉。采购不到精确尺寸时返回实际值，由调用方决定够不够用。
    """
    # DPI：实测结论（E:\BetterAGI\_diag\dpi_probe.py，dpi=144）——
    # 对本进程 DPI-unaware 的窗口，SetWindowPos 用**物理像素**，GetClientRect
    # 返回的也是物理像素：要 640x480 就给 640x480，要 960x720 就给 960x720，
    # 三个尺寸下 client_after 与请求完全一致。**不要按 scale 折回**，折回会把
    # 640 变成 427（实测踩过，画面反而更小）。
    scale = dpi_of(hwnd) / 96.0
    px_w, px_h = int(width), int(height)
    unminimize(hwnd)  # 最小化窗口客户区是 0x0，必须先还原再量
    x, y, w, h = client_rect(hwnd)
    if abs(w - width) <= tolerance and abs(h - height) <= tolerance:
        return {"resized": False, "before": [w, h], "after": [w, h], "target": [width, height],
                "dpi": dpi_of(hwnd), "scale": scale}
    wr = wt.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(wr)):
        raise _err("GetWindowRect")
    # 客户区到底 = 窗口边 - 边框（用现有差值反推，不猜 DPI）
    frame_w = max(0, (wr.right - wr.left) - w)
    frame_h = max(0, (wr.bottom - wr.top) - h)
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    u32.SetWindowPos(hwnd, None, 0, 0, px_w + frame_w, px_h + frame_h,
                     SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
    time.sleep(0.3)  # 等游戏按新尺寸重排一帧
    _, _, w2, h2 = client_rect(hwnd)
    return {"resized": True, "before": [w, h], "after": [w2, h2], "target": [width, height],
            "dpi": dpi_of(hwnd), "scale": scale,
            "accurate": abs(w2 - width) <= 1 and abs(h2 - height) <= 1}


def foreground_hwnd():
    """当前前台窗口句柄；拿不到就返回 0（调用方自己判断要不要抢）。"""
    try:
        return int(u32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001 - 取证辅助，失败不该炸主链路
        return 0


def capture_ready(hwnd, settle_s=0.35):
    """把窗口摆到"能被完整看见"的状态：不最小化、整窗在屏内、客户区够大。

    为什么必须做：窗口在屏幕外/被别的窗口盖住时，**没有任何一种截屏方式**能拿到
    可信的游戏画面——PrintWindow 会糊旧帧或全黑，屏幕截取拿到的是别人的画面。
    与其让模型去猜一张假图，不如先把窗口摆到可见位置：这是自动化操作的必要前提，
    也是"用户随时能看到 AI 在打游戏"这个需求本身要求的。

    只动位置和大小，**绝不全屏、绝不最大化**。
    """
    from PIL import Image  # noqa: F401  (保持与其它抓帧函数一致的依赖)

    report = {"ok": False}
    try:
        unminimize(hwnd)
    except Exception as error:  # noqa: BLE001
        report["unminimize_error"] = f"{type(error).__name__}: {error}"
    sw, sh = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)
    x, y, w, h = window_rect(hwnd)
    # 客户区太小（例如 641x480 的窗口里只剩一条缝）就先给个可用的默认尺寸。
    try:
        _cx, _cy, cw, ch = client_rect(hwnd)
    except Exception:  # noqa: BLE001
        cw = ch = 0
    if cw < 320 or ch < 240:
        try:
            fit_client_size(hwnd, 640, 480)
        except Exception as error:  # noqa: BLE001
            report["fit_error"] = f"{type(error).__name__}: {error}"
        x, y, w, h = window_rect(hwnd)
    margin = 8
    new_x = max(margin, min(sw - w - margin, x if x >= 0 else margin))
    new_y = max(margin, min(sh - h - margin, y if y >= 0 else margin))
    if w > sw - 2 * margin or h > sh - 2 * margin:
        # 比屏幕还大：缩到能放下（不最大化）。
        try:
            fit_client_size(hwnd, max(320, min(1280, sw - 2 * margin - 10)),
                            max(240, min(960, sh - 2 * margin - 40)))
        except Exception as error:  # noqa: BLE001
            report["shrink_error"] = f"{type(error).__name__}: {error}"
        x, y, w, h = window_rect(hwnd)
        new_x = margin
        new_y = margin
    if (new_x, new_y) != (x, y):
        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
        u32.SetWindowPos(hwnd, None, new_x, new_y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    time.sleep(max(0.0, settle_s))
    report.update({"ok": True, "rect": list(window_rect(hwnd)), "screen": [sw, sh],
                   "visible_ratio": round(visible_ratio(hwnd), 3)})
    return report


def window_at(px, py):
    """屏幕上 (px,py) 这一点当前属于哪个顶层窗口。"""
    try:
        pt = wt.POINT(int(px), int(py))
        found = u32.WindowFromPoint(pt)
    except Exception:  # noqa: BLE001
        return None
    if not found:
        return None
    GA_ROOT = 2
    try:
        root = u32.GetAncestor(found, GA_ROOT)
    except Exception:  # noqa: BLE001
        root = found
    return int(root or found)


def visible_ratio(hwnd, samples=36):
    """这个窗口在屏幕上真正露出来的像素比例（0.0~1.0）。

    为什么要算：窗口被浏览器盖住时，屏幕上那块是**别人的画面**，直接截就是错的；
    窗口拖到一半出屏时，右半本来就不存在。两种情况的正确处理不一样，所以先量一下。
    """
    x, y, w, h = window_rect(hwnd)
    if w <= 0 or h <= 0:
        return 0.0
    hits = 0
    total = 0
    for gx in range(1, 7):
        for gy in range(1, 7):
            px, py = x + w * gx // 7, y + h * gy // 7
            if 0 <= px < 100000 and 0 <= py < 100000:
                total += 1
                hits += 1 if window_at(px, py) == int(hwnd) else 0
    return (hits / 36.0) if total else 0.0


def is_blank(data, threshold=0.004):
    """判断编码后的帧是不是"基本全黑/全白"（PrintWindow 在最小化/出屏时会这样）。

    只用缩略图做统计，代价是几毫秒；阈值按「非纯黑像素占比」算，
    默认 0.4%——少于这个比例就当这张图没有可用内容，让调用方去走备用路径。
    """
    try:
        from PIL import Image

        if not data:
            return True
        img = Image.open(io.BytesIO(data)).convert("L")
        img.thumbnail((64, 64))
        pixels = list(img.getdata())
        if not pixels:
            return True
        total = getattr(img, "width", 0) * getattr(img, "height", 0)
        if not total:
            return True
        nonblack = sum(1 for px in pixels if px > 12)
        return (nonblack / float(total)) < float(threshold)
    except Exception:  # noqa: BLE001 - 判断不了就当不是空帧，别误触发兜底
        return False


def grab_visible(hwnd, fmt="JPEG", quality=85, fit=None):
    """截**用户此刻真正看到的那一帧**：屏幕上露出来的部分从屏幕取，其余从窗口表面取。

    这条路径是踩了两轮坑才定下来的，三条实测结论决定了它：

    1. **PrintWindow 会给旧帧**：本机实测（2026-11-04）游戏已经切到「按 Z 继续」对话框，
       PrintWindow(PW_RENDERFULLCONTENT) 截出来还是标题画面，而同一时刻屏幕上是新画面。
       拿旧帧喂快系统，模型永远在描述上一秒的事。
    2. **窗口可能一半在屏幕外**：实测窗口 client 在 x=640、屏幕宽 1280，右半出屏。
       出屏那部分 PrintWindow 画不出来（全黑），但它是**本来就看不见**的，不是内容。
    3. **窗口被别的窗口盖住**时屏幕上那块是别人的画面，绝不能当成游戏画面。

    所以：先用 WindowFromPoint 量窗口露出的比例——露得多就信屏幕（真实、无遮挡污染），
    被盖住就用 PrintWindow（DWM 里窗口表面还在，录屏能拿到）；屏幕外的那部分一律补黑。
    任何情况下都返回**与窗口同尺寸**的一张图，调用方不需要知道这些细节。
    """

    from PIL import Image

    x, y, w, h = window_rect(hwnd)
    if w <= 0 or h <= 0:
        raise HarnessError("window rect empty: %dx%d" % (w, h))
    ratio = visible_ratio(hwnd)
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    on_screen_px = 0
    source = "printwindow"

    if ratio >= 0.5:
        desktop = _desktop_image()
        if desktop is not None:
            sw, sh = desktop.size
            vx0, vy0 = max(0, x), max(0, y)
            vx1, vy1 = min(sw, x + w), min(sh, y + h)
            if vx1 > vx0 and vy1 > vy0:
                canvas.paste(desktop.crop((vx0, vy0, vx1, vy1)), (vx0 - x, vy0 - y))
                on_screen_px = (vx1 - vx0) * (vy1 - vy0)
                source = "desktop"

    if on_screen_px < w * h:
        try:
            # PrintWindow 要临时把窗口挪回屏幕内才有内容（窗口出屏时它会直接画黑），
            # 所以先 import 了 restore 就用带自愈的版本；纯函数版本给单测用。
            captured = _restore_and_print(hwnd) if _can_restore(hwnd) else _window_image(hwnd)
        except Exception:  # noqa: BLE001 - 补不上就保留已有像素/黑边，别让整次截帧失败
            captured = None
        if captured is not None:
            cw, ch = captured.size
            cx0, cy0 = max(0, -x), max(0, -y)
            cx1, cy1 = min(cw, max(0, -(x + w))), min(ch, max(0, -(y + h)))
            if cx1 > cx0 and cy1 > cy0:
                canvas.paste(captured.crop((cx0, cy0, cx1, cy1)), (cx0, cy0))
            if source == "printwindow":
                on_screen_px = max(on_screen_px, 0)

    if fit:
        canvas = _fit_image(canvas, fit)
    data = encode_image(canvas, fmt=fmt, quality=quality)
    info = {"source": source, "width": canvas.size[0], "height": canvas.size[1],
            "bytes": len(data), "visible_ratio": round(ratio, 3),
            "on_screen_px": on_screen_px, "window_rect": [x, y, w, h]}
    return data, info

def encode_image(img, fmt="JPEG", quality=85):
    """把 PIL 图编码成字节（热路径用，纯内存）。"""
    buffer = io.BytesIO()
    if str(fmt).upper() == "PNG":
        img.save(buffer, format="PNG", optimize=False, compress_level=1)
    else:
        img.save(buffer, format="JPEG", quality=int(quality), subsampling=0)
    return buffer.getvalue()


def _desktop_image():
    """整屏截一张（BitBlt from screen DC），失败返回 None。"""
    from PIL import Image

    try:
        sm = u32.GetSystemMetrics
        vx, vy = sm(76), sm(77)  # SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN
        vw, vh = sm(78), sm(79)  # SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN
        hdc_screen = u32.GetDC(0)
        if not hdc_screen:
            return None
        memdc = g32.CreateCompatibleDC(hdc_screen)
        bmp = g32.CreateCompatibleBitmap(hdc_screen, vw, vh)
        g32.SelectObject(memdc, bmp)
        try:
            if not g32.BitBlt(memdc, 0, 0, vw, vh, hdc_screen, vx, vy, 0x00CC0020):
                return None
            raw = _dib_bytes(memdc, bmp, vw, vh)
        finally:
            g32.DeleteObject(bmp)
            g32.DeleteDC(memdc)
            u32.ReleaseDC(0, hdc_screen)
        return Image.frombuffer("RGBA", (vw, vh), raw, "raw", "BGRA", vw * 4, 1).convert("RGB")
    except Exception:  # noqa: BLE001
        return None


def _dib_bytes(memdc, bmp, w, h):
    class BMI(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    bmi = BMI(ctypes.sizeof(BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * 4 * h)
    if not g32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bmi), 0):
        raise _err("GetDIBits")
    return buf.raw


def _can_restore(hwnd):
    """自愈抓帧只在"窗口能挪"时启用：看得见、不是别的进程锁住的全屏独占。"""
    try:
        return bool(u32.IsWindowVisible(hwnd))
    except Exception:  # noqa: BLE001
        return False


def _restore_and_print(hwnd):
    """把窗口挪回屏幕内 → 抓 → 挪回去。返回与整窗同尺寸的图。

    实测（2026-11-04）：窗口 client 在 x=640、屏幕宽 1280，右半出屏时 PrintWindow
    整张全黑。而窗口一旦**完整在屏幕内**，PrintWindow 就有内容。所以自愈只在需要时
    发生（窗口被遮挡/出屏），且结束后一定挪回原位——不抢用户的布局。
    """
    from PIL import Image

    x, y, w, h = window_rect(hwnd)
    captured = _window_image(hwnd)
    try:
        distinct = len(set(captured.tobytes()[0::997]))
    except Exception:  # noqa: BLE001
        distinct = 2
    if distinct > 2:
        return captured
    sw, sh = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)
    new_x = max(0, min(sw - w, 8))
    new_y = max(0, min(sh - h, 8))
    if (new_x, new_y) == (x, y):
        return captured
    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
    try:
        u32.SetWindowPos(hwnd, None, new_x, new_y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        time.sleep(0.25)
        captured = _window_image(hwnd)
    finally:
        try:
            u32.SetWindowPos(hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        except Exception:  # noqa: BLE001
            pass
    return captured


def _window_image(hwnd):
    """只对窗口本身 PrintWindow（不受桌面遮挡影响，但会被"窗口外"的内容影响）。"""
    from PIL import Image

    raw, w, h, _info = _grab_raw(hwnd)
    return Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", w * 4, 1).convert("RGB")


def _fit_image(img, fit):
    """把图缩到 fit=(w,h)（只给屏幕裁剪路径用；PrintWindow 路径自带 fit 逻辑）。"""

    if not fit:
        return img
    try:
        tw, th = max(1, int(fit[0])), max(1, int(fit[1]))
    except Exception:  # noqa: BLE001 - fit 传坏了就当没传
        return img
    if img.size == (tw, th):
        return img
    return img.resize((tw, th), Image_lanczos())


def Image_lanczos():
    from PIL import Image

    return Image.LANCZOS


def grab_bytes(hwnd, fmt="JPEG", quality=85, fit=None):
    """截客户区并**直接返回内存字节**，全程不落盘。

    高频抓帧绝不能写磁盘：快系统每秒 1 帧、慢系统每轮 1 张，直播几小时就是
    成千上万次 PNG 落盘——这是拿用户的 SSD 寿命换方便，属于设计错误。
    需要看单帧时由调用方显式落盘，抓帧路径本身永远是纯内存。
    """

    raw, w, h, info = _grab_raw(hwnd, fit=fit)
    from PIL import Image

    stride = w * 4
    img = Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", stride, 1).convert("RGB")
    buffer = io.BytesIO()
    if fmt.upper() == "PNG":
        img.save(buffer, format="PNG", optimize=False, compress_level=1)
    else:
        img.save(buffer, format="JPEG", quality=int(quality), subsampling=0)
    data = buffer.getvalue()
    info = dict(info)
    info.update({"bytes": len(data), "format": fmt.upper(), "width": w, "height": h})
    return data, info


def _grab_raw(hwnd, fit=None):
    """PrintWindow 到内存 DIB，返回 (像素字节, 宽, 高, 元信息)。不碰磁盘。"""

    fit_info = None
    if fit:
        try:
            fit_info = fit_client_size(hwnd, int(fit[0]), int(fit[1]))
        except Exception as error:  # noqa: BLE001 - 调不动就用当前尺寸，别让截屏失败
            fit_info = {"resized": False, "error": f"{type(error).__name__}: {error}"}
    x, y, w, h = client_rect(hwnd)
    if w <= 0 or h <= 0:
        raise HarnessError("client rect empty: %dx%d" % (w, h))
    hdc = u32.GetDC(hwnd)
    if not hdc:
        raise _err("GetDC")
    memdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(memdc, bmp)
    try:
        if not u32.PrintWindow(hwnd, memdc, 3):  # PW_CLIENTONLY|PW_RENDERFULLCONTENT
            if not g32.BitBlt(memdc, 0, 0, w, h, hdc, 0, 0, 0x00CC0020):
                raise _err("BitBlt")

        class BMI(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

        bmi = BMI(ctypes.sizeof(BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        stride = w * 4
        buf = ctypes.create_string_buffer(stride * h)
        if not g32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bmi), 0):
            raise _err("GetDIBits")
    finally:
        g32.DeleteObject(bmp)
        g32.DeleteDC(memdc)
        u32.ReleaseDC(hwnd, hdc)
    data = buf.raw
    distinct = len(set(data[0:stride * h:97]))
    if distinct <= 1 and os.environ.get("UT_ALLOW_BLACK") != "1":
        raise HarnessError(
            "captured frame is a flat colour (window is minimised/covered or rendering suspended); "
            "refusing to return a misleading black screenshot")
    info = {"distinct_sample": distinct}
    if fit_info is not None:
        info["fit"] = fit_info
    return data, w, h, info


def capture(hwnd, path, fit=None):
    """把当前帧显式保存到 path（诊断用）。直播热路径请用 grab_bytes，不走这里。"""

    from PIL import Image

    raw, w, h, info = _grab_raw(hwnd, fit=fit)
    stride = w * 4
    img = Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", stride, 1).convert("RGB")
    img.save(path)
    out = {"path": path, "width": w, "height": h, "distinct_sample": info.get("distinct_sample")}
    if "fit" in info:
        out["fit"] = info["fit"]
    return out


KEYMAP = {"enter": 0x0D, "space": 0x20, "esc": 0x1B, "z": 0x5A, "x": 0x58,
          "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
          # 退格：UT 命名界面删字符（实测是唯一能撤回输入的方法）
          "backspace": 0x08, "delete": 0x2E,
          # 修饰键：命名屏的"按住 Shift 改名/用鼠标选字母"都要它，缺了就只能干瞪眼
          "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
          # 字母/数字：命名屏、作弊码、控制台都用物理字母键，缺了 press 就无从下手
          }
KEYMAP.update({chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)})
KEYMAP.update({str(d): 0x30 + d for d in range(10)})
# 2026-11-04 补齐：GameScaling 的 74 token 里有这些键，缺一个就等于那一步按不出去。
# 虚拟键码取自 winuser.h（VK_*），不是拍脑袋。
KEYMAP.update({
    "tab": 0x09, "capslock": 0x14, "pause": 0x13,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
    "pageup": 0x21, "pagedown": 0x22,
    "minus": 0xBD, "equal": 0xBB, "period": 0xBE, "slash": 0xBF, "quote": 0xDE,
})
KEYMAP.update({"f%d" % i: 0x6F + i for i in range(1, 13)})

def press(hwnd, key, hold_ms=60):
    """Press a key into the game window.

    Default delivery is PostMessage (no cursor movement). If the game ignores
    posted keys (GameMaker reads the async keyboard state, not WM_KEYDOWN),
    pass via="sendinput" to inject real key events at the OS level.
    IMPORTANT: SendInput goes to whatever window has focus - always focus() first.
    """
    k = key.lower()
    if k not in KEYMAP and len(k) != 1:
        raise HarnessError("unknown key: %r (known: %s)" % (key, ", ".join(sorted(KEYMAP))))
    vk = KEYMAP[k] if k in KEYMAP else ord(k.upper())
    focus(hwnd)
    scan = u32.MapVirtualKeyW(vk, 0)
    u32.PostMessageW(hwnd, 0x0100, vk, (scan << 16) | 1)
    time.sleep(hold_ms / 1000.0)
    u32.PostMessageW(hwnd, 0x0101, vk, (scan << 16) | 1 | (1 << 30) | (1 << 31))
    return vk


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]

class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _send_input(ki):
    inp = INPUT(type=1, ki=ki)
    if u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        raise _err("SendInput")


def real_press(hwnd, key, hold_ms=90):
    """OS-level key press. Requires the game window to be focused."""
    if not focus(hwnd):
        raise HarnessError("cannot focus window %s" % hwnd)
    time.sleep(0.08)
    k = key.lower()
    if k not in KEYMAP and len(k) != 1:
        raise HarnessError("unknown key: %r" % key)
    vk = KEYMAP[k] if k in KEYMAP else ord(k.upper())
    _key(vk, hold_ms)
    time.sleep(0.03)
    return vk


def _key(vk, hold_ms=90):
    if is_forbidden(vk):
        return False
    scan = u32.MapVirtualKeyW(vk, 0)
    _send_input(KEYBDINPUT(vk, scan, 0, 0, None))
    time.sleep(hold_ms / 1000.0)
    _send_input(KEYBDINPUT(vk, scan, 0x0002, 0, None))


def key_table():
    """语义/物理键名 -> 虚拟键码。play_steps 逐拍发键时用它查表。"""
    return KEYMAP


def key_down_vks(vks):
    """按下（不松开）一组虚拟键。

    为什么需要「按下 / 松开」两个原语：GameScaling 的 6 组动作里，同一个键出现在相邻组
    表示**一直按住**，不是重复按两下（README §3）。用 press_vks 逐组按就是 6 次按下+松开，
    长按语义全丢——走路走不起来，蓄力也蓄不上。
    """
    vks = [vk for vk in list(vks) if not is_forbidden(vk)]
    if not vks:
        return []
    downs = [INPUT(type=1, ki=KEYBDINPUT(vk, u32.MapVirtualKeyW(vk, 0), 0, 0, None)) for vk in vks]
    if u32.SendInput(len(downs), (INPUT * len(downs))(*downs), ctypes.sizeof(INPUT)) != len(downs):
        raise _err("SendInput/down")
    return vks


def key_up_vks(vks):
    """松开一组虚拟键（key_down_vks 的配对）。"""
    vks = [vk for vk in list(vks) if not is_forbidden(vk)]
    if not vks:
        return []
    ups = [INPUT(type=1, ki=KEYBDINPUT(vk, u32.MapVirtualKeyW(vk, 0), 0x0002, 0, None)) for vk in vks]
    u32.SendInput(len(ups), (INPUT * len(ups))(*ups), ctypes.sizeof(INPUT))
    return vks


def press_vks(vks, hold_ms=16):
    """一次 SendInput 按住一组虚拟键，等 hold_ms 后一起松开。"""
    # 禁键在这里被剔除而不是整批拒绝：一次按 4 个键时，宁可少按一下也不许发红线键。
    vks = [vk for vk in list(vks) if not is_forbidden(vk)]
    if not vks:
        return []
    downs = [INPUT(type=1, ki=KEYBDINPUT(vk, u32.MapVirtualKeyW(vk, 0), 0, 0, None)) for vk in vks]
    if u32.SendInput(len(downs), (INPUT * len(downs))(*downs), ctypes.sizeof(INPUT)) != len(downs):
        raise _err("SendInput/down")
    try:
        time.sleep(max(0.0, hold_ms / 1000.0))
    finally:
        ups = [INPUT(type=1, ki=KEYBDINPUT(vk, u32.MapVirtualKeyW(vk, 0), 0x0002, 0, None)) for vk in vks]
        u32.SendInput(len(ups), (INPUT * len(ups))(*ups), ctypes.sizeof(INPUT))
    return vks

def step(hwnd, seconds=1.0, keys=(), x=None, y=None):
    """One action tick: optional keys, optional real mouse click at client (x,y), then idle."""
    focus(hwnd)
    time.sleep(0.05)
    for k in keys:
        real_press(hwnd, k)
        time.sleep(0.12)
    if x is not None:
        cx, cy, _, _ = client_rect(hwnd)
        u32.SetCursorPos(int(cx + x), int(cy + y))
        time.sleep(0.08)
        u32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.08)
        u32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(max(0.0, seconds))
    return capture(hwnd, r"E:\BetterAGI\_diag\ut_tick.png")


def type_text(hwnd, text, delay_ms=90):
    for ch in text:
        real_press(hwnd, ch, hold_ms=50)
        time.sleep(delay_ms / 1000.0)
    return len(text)


def type_unicode(text, delay_ms=45):
    """按字符发送真正的 Unicode 字符（KEYEVENTF_UNICODE），供命名/输入框用。

    为什么不复用 real_press：它走 VK 码，只能打大写字母（ord(k.upper())），
    且每个字符都要 focus() 一次（实测每字符 ~200ms）。命名屏要的是"输入的文字"，
    而不是"按了哪个键"——直接送字符，大小写都对，也不受输入法布局影响。

    实现口径与 BetterGI 一致：这不是"注入"（不碰 PostMessage / 窗口消息队列 /
    不伪造 WM_KEYDOWN），而是调用系统级 SendInput 模拟本地键入——游戏看到的
    与真人敲键盘完全同一路径。SendInput 只送到前台窗口，调用方负责先抢前台。

    Return: 实际发送的字符数。
    """
    count = 0
    for ch in str(text or ""):
        code = ord(ch)
        down = KEYBDINPUT(0, code, 0x0004, 0, None)
        up = KEYBDINPUT(0, code, 0x0004 | 0x0002, 0, None)
        if u32.SendInput(1, ctypes.byref(INPUT(type=1, ki=down)), ctypes.sizeof(INPUT)) != 1:
            raise _err("SendInput/unicode-down")
        time.sleep(max(0.0, delay_ms / 1000.0))
        if u32.SendInput(1, ctypes.byref(INPUT(type=1, ki=up)), ctypes.sizeof(INPUT)) != 1:
            raise _err("SendInput/unicode-up")
        count += 1
    return count


MAIN_CLASS = "YYGameMakerYY"
DIALOG_CLASS = "#32770"
UT_KEYS = ("up", "down", "left", "right", "z", "x", "enter", "esc", "c", "shift")


def _class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def ut_windows():
    """Return {'main': window|None, 'dialogs': [...], 'process_running': bool} for UNDERTALE."""
    import subprocess
    running = "UNDERTALE.exe" in subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq UNDERTALE.exe"], capture_output=True, text=True).stdout
    main = None
    dialogs = []
    for w in enum_windows():
        if not _exe_of(w["pid"]).lower().endswith("undertale.exe"):
            continue
        cls = _class_of(w["hwnd"])
        w["class"] = cls
        if cls == MAIN_CLASS:
            main = w
        elif cls == DIALOG_CLASS:
            dialogs.append(w)
    return {"main": main, "dialogs": dialogs, "process_running": running}


def prepare_ut():
    """Focus the game and clear any native dialog that stole it. Returns a status dict."""
    state = ut_windows()
    if not state["main"]:
        raise HarnessError("UNDERTALE window not found (process_running=%s)" % state["process_running"])
    report = {"dialogs": len(state["dialogs"]), "cleared": 0}
    for d in state["dialogs"]:
        u32.PostMessageW(d["hwnd"], 0x0010, 0, 0)  # WM_CLOSE
        report["cleared"] += 1
    time.sleep(0.3)
    main = find_window("UNDERTALE", exe="UNDERTALE.exe")
    state2 = ut_windows()
    report["foreground"] = sethwnd_foreground(state2["main"]["hwnd"]) if state2["main"] else False
    return report


def save_dir():
    return os.path.join(os.environ["LOCALAPPDATA"], "UNDERTALE")

SAVE_FILES = ("file0", "file9", "undertale.ini")

def clear_save(dry=False):
    d = save_dir()
    removed = []
    if not os.path.isdir(d):
        return {"dir": d, "removed": removed, "note": "no save dir"}
    for name in SAVE_FILES:
        p = os.path.join(d, name)
        if os.path.exists(p):
            removed.append({"file": p, "bytes": os.path.getsize(p)})
            if not dry:
                os.remove(p)
    return {"dir": d, "removed": removed, "dry": dry}

def act(actions, seconds=1.0, capture_after=True):
    """Run a batch of actions against UNDERTALE with proper focus handling.

    actions: list of {"key": "z"} | {"keys": ["down","z"]} | {"wait": 1.5}
    """
    state = ut_windows()
    if not state["main"]:
        raise HarnessError("UNDERTALE window not found")
    hwnd = state["main"]["hwnd"]
    if state["dialogs"]:
        prepare_ut()
        hwnd = ut_windows()["main"]["hwnd"]
    sethwnd_foreground(hwnd)
    time.sleep(0.05)
    log = []
    for a in actions or []:
        if "wait" in a:
            time.sleep(float(a["wait"]))
            log.append({"wait": float(a["wait"])})
            continue
        keys = a.get("keys") or ([a["key"]] if a.get("key") else [])
        for k in keys:
            if k.lower() not in UT_KEYS:
                raise HarnessError("key not allowed for UNDERTALE: %r (allowed: %s)" % (k, ", ".join(UT_KEYS)))
            if u32.GetForegroundWindow() != hwnd:
                sethwnd_foreground(hwnd)
            real_press(hwnd, k, hold_ms=int(a.get("hold_ms") or 90))
            time.sleep(0.06)
            log.append({"key": k})
        time.sleep(float(a.get("settle") or 0.12))
    time.sleep(max(0.0, float(seconds)))
    out = {"actions": log, "foreground": u32.GetForegroundWindow() == hwnd}
    if capture_after:
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), "ut_act_frame.png")
        info = capture(hwnd, tmp)
        with open(tmp, "rb") as f:
            out["png_b64"] = __import__("base64").b64encode(f.read()).decode()
        out.update({k: info[k] for k in ("width", "height")})
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("windows")
    sub.add_parser("procs")
    p = sub.add_parser("capture")
    p.add_argument("--match", default="UNDERTALE")
    p.add_argument("--out", default=r"E:\BetterAGI\_diag\ut_frame.png")
    k = sub.add_parser("key")
    k.add_argument("key")
    k.add_argument("--match", default="UNDERTALE")
    k.add_argument("--times", type=int, default=1)
    t = sub.add_parser("tick")
    t.add_argument("--match", default="UNDERTALE")
    t.add_argument("--seconds", type=float, default=1.0)
    t.add_argument("--keys", default="")
    t.add_argument("--x", type=int)
    t.add_argument("--y", type=int)
    s = sub.add_parser("clear-save")
    s.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.cmd == "windows":
        print(json.dumps(enum_windows(), ensure_ascii=False, indent=1))
    elif a.cmd == "procs":
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq UNDERTALE.exe"], capture_output=True, text=True).stdout
        print(out.strip())
    elif a.cmd == "capture":
        w = find_window(a.match, exe="UNDERTALE.exe") or (_ for _ in ()).throw(HarnessError("window not found: " + a.match))
        print(json.dumps(capture(w["hwnd"], a.out), ensure_ascii=False))
    elif a.cmd == "tick":
        w = find_window(a.match, exe="UNDERTALE.exe") or (_ for _ in ()).throw(HarnessError("window not found"))
        print(json.dumps(step(w["hwnd"], a.seconds, a.keys.split(",") if a.keys else (), a.x, a.y)))
    elif a.cmd == "key":
        w = find_window(a.match) or (_ for _ in ()).throw(HarnessError("window not found: " + a.match))
        for _ in range(a.times):
            press(w["hwnd"], a.key)
        print(json.dumps({"ok": True, "key": a.key, "times": a.times}))
    elif a.cmd == "clear-save":
        print(json.dumps(clear_save(a.dry), ensure_ascii=False))

if __name__ == "__main__":
    main()


# ---------------------------------------------------------------- 输入法守卫
imm32 = ctypes.WinDLL("imm32", use_last_error=True)
imm32.ImmGetContext.restype = ctypes.c_void_p
imm32.ImmGetContext.argtypes = [ctypes.c_void_p]
imm32.ImmGetConversionStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
imm32.ImmSetConversionStatus.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
imm32.ImmReleaseContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
u32.GetKeyboardLayout.argtypes = [ctypes.c_uint]
u32.GetKeyboardLayout.restype = ctypes.c_void_p
u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
u32.GetWindowThreadProcessId.restype = ctypes.c_uint
u32.LoadKeyboardLayoutW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
u32.LoadKeyboardLayoutW.restype = ctypes.c_void_p

WM_INPUTLANGCHANGEREQUEST = 0x0050
KLF_ACTIVATE = 0x00000001
IME_CMODE_NATIVE = 0x0001
IME_CMODE_ALPHANUMERIC = 0x0000
US_LAYOUT = "00000409"


def input_state(hwnd):
    """读目标窗口线程的键盘布局与 IME 转换模式（不做任何修改）。"""
    hwnd = hwnd if isinstance(hwnd, ctypes.c_void_p) else ctypes.c_void_p(hwnd)
    tid = u32.GetWindowThreadProcessId(hwnd, None)
    hkl = u32.GetKeyboardLayout(tid)
    langid = (hkl or 0) & 0xFFFF
    state = {"thread": tid, "layout": hex(langid or 0), "is_chinese_layout": langid in (0x0804, 0x0404, 0x0C04, 0x1004),
             "ime_open": None, "native": None}
    ctx = imm32.ImmGetContext(hwnd)
    if ctx:
        conversion = ctypes.c_uint(0)
        sentence = ctypes.c_uint(0)
        if imm32.ImmGetConversionStatus(ctx, ctypes.byref(conversion), ctypes.byref(sentence)):
            mode = conversion.value
            state["ime_open"] = bool(mode & 0x0001)  # IME_CMODE_OPEN 语义由 native 位体现
            state["native"] = bool(mode & IME_CMODE_NATIVE)
            state["mode"] = hex(mode)
        imm32.ImmReleaseContext(hwnd, ctx)
    return state


def ensure_latin_input(hwnd):
    """把目标窗口的输入切成英文直通，避免中文输入法吃掉按键。

    两步都要做，缺一不可：
      1) 键盘布局切到 en-US（中文布局下 IME 会先截获按键）；
      2) IME 转换模式设成 ALPHANUMERIC（布局已是英文但 IME 仍可能处于中文态）。
    """
    hwnd = hwnd if isinstance(hwnd, ctypes.c_void_p) else ctypes.c_void_p(hwnd)
    before = input_state(hwnd)
    tid = u32.GetWindowThreadProcessId(hwnd, None)
    us = u32.LoadKeyboardLayoutW(US_LAYOUT, KLF_ACTIVATE)
    if us:
        u32.PostMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, us)
    ctx = imm32.ImmGetContext(hwnd)
    ime_changed = False
    if ctx:
        sentence = ctypes.c_uint(0)
        if imm32.ImmSetConversionStatus(ctx, IME_CMODE_ALPHANUMERIC, sentence):
            ime_changed = True
        imm32.ImmReleaseContext(hwnd, ctx)
    time.sleep(0.05)
    after = input_state(hwnd)
    return {"before": before, "after": after, "layout_switched": bool(us), "ime_alnum": ime_changed,
            "safe": (not after["is_chinese_layout"]) and (after["native"] is not True)}


# ---------------------------------------------------------------- 重启到干净状态
def _taskkill(exe):
    import subprocess
    subprocess.run(["taskkill", "/F", "/IM", exe], capture_output=True, text=True)
    time.sleep(1.0)


def restart_game(exe_path, exe_name=None, save_paths=(), clear=True, wait_s=25.0):
    """杀进程 → 清存档 → 重新启动 → 等主窗口出现。返回每一步的真实结果。

    clear=False 时只重启不清档（用于"接着上次进度"）。
    """
    from pathlib import Path

    exe_name = exe_name or Path(exe_path).name
    report = {"killed": False, "cleared": [], "launched": False, "window": None}
    if ut_windows()["main"] is not None:
        _taskkill(exe_name)
        report["killed"] = True
    if clear:
        for item in save_paths:
            path = Path(item)
            if not path.exists():
                report["cleared"].append({"path": str(path), "existed": False})
                continue
            removed = 0
            for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                try:
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                        removed += 1
                    elif child.is_dir():
                        child.rmdir()
                except OSError:
                    continue
            report["cleared"].append({"path": str(path), "existed": True, "removed": removed})
    import subprocess
    # launched 必须代表"窗口真的起来了"，而不是 Popen 没抛异常——
    # 上一版把 Popen 成功写成 launched=true，游戏其实没起来，直播空转了半分钟。
    proc = subprocess.Popen([str(exe_path)], cwd=str(Path(exe_path).parent))
    report["pid"] = proc.pid
    deadline = time.time() + wait_s
    # 实测（2026-11-04）：Undertale 是**双进程**——一个启动器负责拉起真正的游戏进程后
    # 自己退出（退出码 0xC0000005 / 3221225477，看着像崩溃，其实是正常行为）。上一版盯着
    # Popen 那个 pid 判死活，于是每次"成功启动"都被误判成"游戏进程已退出"。
    # 判定标准只能是：镜像名还有没有活进程 + 有没有真窗口。
    gone_since = None
    while time.time() < deadline:
        state = ut_windows()
        if state["process_running"]:
            gone_since = None
        else:
            gone_since = gone_since or time.time()
            if proc.poll() is not None and time.time() - gone_since > 2.0:
                report["launched"] = False
                report["error"] = f"游戏进程已退出，exit={proc.returncode}"
                return report
        if state["main"] is not None and state["process_running"]:
            hwnd = state["main"]["hwnd"]
            _, _, cw, ch = client_rect(hwnd)
            if cw > 0 and ch > 0:  # 客户区有尺寸才算真渲染出来了
                report["launched"] = True
                report["window"] = {"hwnd": hwnd, "title": state["main"]["title"],
                                    "class": _class_of(hwnd), "client": [cw, ch]}
                return report
        time.sleep(0.4)
    report["launched"] = False
    report["error"] = f"{wait_s}s 内没等到游戏主窗口（客户区已有尺寸）"
    return report