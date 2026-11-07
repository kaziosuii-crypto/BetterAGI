"""Per-profile execution adapter: one capture/keyboard interface for every game.

A profile declares in config.json:

    "adapter": {
      "capture": {"method": "screen"},   # screen（默认）| printwindow（诊断用）
      "match": {"title": "UNDERTALE", "exe": "UNDERTALE.exe", "class": "YYGameMakerYY"},
      "kill_dialogs": ["#32770"],
      "keys": {"up": "up", "down": "down", "left": "left", "right": "right",
               "confirm": ["z", "enter"], "cancel": ["x"], "menu": ["c"], "quit": ["esc"]}
    }

Keys are NAMES the agent reasons about (up/confirm/...). Which physical key a name
maps to is DATA in the profile, not code - so learning a game means editing the
profile, not the harness.

抓帧为什么默认走屏幕（2026-11-04 实测，别改回去）
------------------------------------------------
同一台机器（DPI 144、屏 1707x1067、客户区 641x480）同一时刻实测三条路：

1. PrintWindow 给的是裁剪错误的帧：641x480 输出里真实内容只占右下 457x327，
   左 184px、上下各 89px 全黑；被遮挡/最小化时还给全黑或上一秒的旧帧。
2. GetDC 客户区 BitBlt 全白：游戏是 GPU/DirectX 渲染，窗口 DC 上没有像素。
3. 屏幕 BitBlt（_desktop_image 裁窗口矩形）与用户所见一致：实测裁剪图覆盖整张
   645x507、无黑边。

所以 grab() 默认 = 抢前台（只在被遮挡/不在前台时抢）+ 屏幕裁剪；窗口被别的窗口
盖住的那一刻屏幕上那块本来就是别人的画面，先自愈（顶到前台），自愈失败才退回
PrintWindow 并在 info 里标降级。强制别的路径用 BETTERAGI_CAPTURE=printwindow。
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _import_harness():
    """ut_harness 在仓库 tools/ 下。不依赖 cwd：先按 __file__ 推出仓库根加进 sys.path。"""
    import sys

    repo_root = str(Path(__file__).resolve().parent.parent)
    for candidate in (repo_root, str(Path(__file__).resolve().parent)):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
    for loader in (
        lambda: __import__("tools.ut_harness", fromlist=["ut_harness"]),
        lambda: __import__("ut_harness", fromlist=["ut_harness"]),
    ):
        try:
            return loader()
        except ImportError:
            continue
    raise ImportError("找不到 ut_harness（应在 E:/BetterAGI/tools/ut_harness.py）")


ut_harness = _import_harness()


def _load_wgc_capture():
    """wgc_capture 与本文件同目录；用绝对路径导入，避免裸 import 撞上别的同名模块。"""
    import importlib.util

    path = Path(__file__).resolve().parent / "wgc_capture.py"
    spec = importlib.util.spec_from_file_location("betteragi_wgc_capture", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fit_image(image, fit):
    """缩放到 fit=(w,h)；fit 为空就原样返回（WGC 原图 966x760 直接给模型也行）。"""
    return ut_harness._fit_image(image, fit)


def _encode_image(image, fmt, quality):
    """把 PIL 图压成字节，全程不落盘。"""
    import io

    buffer = io.BytesIO()
    if str(fmt).upper() == "PNG":
        image.save(buffer, format="PNG", optimize=False, compress_level=1)
    else:
        image.save(buffer, format="JPEG", quality=int(quality), subsampling=0)
    return buffer.getvalue()


class AdapterError(RuntimeError):
    pass


# 平台级安全红线：这些虚拟键**任何路径都不许发**，不管模型怎么说。
# 0x73 = F4：Undertale 里是切换全屏，主播在直播时会直接黑屏/串画面。
# 用环境变量 BETTERAGI_FORBIDDEN_VKS（逗号分隔十进制）可以按场景追加。
# 真正的闸口在 ut_harness（唯一发送层），这里只是**读取同一份策略**用于向上报
# skipped_keys——两处共用 forbidden_vks() 的实现，不会各说各话。
def _clock_ms(clock: Any = None) -> float:
    """把一个单调时钟读成毫秒。

    clock 允许两种形状：零参可调用且返回**秒**（time.monotonic 的签名，测试里最容易
    注入），或带 now_ms() 的对象（timeline.Clock 的签名）。不传就用 time.monotonic——
    这是 play_steps 的默认口径，也保证老调用方拿到的时序仍然是单调的。
    """
    if clock is None:
        return time.monotonic() * 1000.0
    now_ms = getattr(clock, "now_ms", None)
    if callable(now_ms):
        return float(now_ms())
    return float(clock()) * 1000.0


def forbidden_vks() -> set:
    return ut_harness.forbidden_vks()


DEFAULTS: Dict[str, Any] = {
    "capture": {"method": "printwindow"},
    "match": {},
    "kill_dialogs": [],
    "keys": {},
    "launch": {},
}


def load_config(profile) -> Dict[str, Any]:
    """Read config.json of a profile and merge the adapter section over defaults."""
    cfg: Dict[str, Any] = {}
    path = profile.dir / "config.json"
    if path.exists():
        cfg = json.loads(path.read_text(encoding="utf-8"))
    adapter = dict(DEFAULTS)
    adapter.update(cfg.get("adapter") or {})
    return adapter


class ProfileAdapter:
    """Resolve windows, capture frames and press keys for ONE game profile."""

    def __init__(self, profile):
        self.profile = profile
        self.cfg = load_config(profile)
        self.match = self.cfg.get("match") or {}
        self.capture_cfg = self.cfg.get("capture") or {}
        self.keymap: Dict[str, List[str]] = {
            k: ([v] if isinstance(v, str) else list(v)) for k, v in (self.cfg.get("keys") or {}).items()
        }
        # 主窗口句柄缓存。windows() 每次都要跑一遍 tasklist 子进程（实测 ~1.1s），
        # 而直播是每轮都抓帧——那是真正的瓶颈，不是 PrintWindow 慢（实测 28~44ms）。
        # 句柄在一次游戏进程生命周期内稳定，缓存后只在失效时重新发现。
        self._hwnd: Optional[int] = None
        self._pid: Optional[int] = None
        self._discover_ms: float = 0.0
        # 窗口"摆到可见位置"每次会话只做一次（几百毫秒），之后每帧仍是高频抓取。
        self._capture_ready: Optional[Dict[str, Any]] = None
        # IME 守卫与输入时序同样按会话缓存/自校准：它们都是"一次成本摊到每轮"的典型。
        self._ime_guard: Optional[Dict[str, Any]] = None
        self._press_tune: Optional[Dict[str, float]] = None
        # 跨决策保持按住的键（vk）：play_steps(keep_held=True) 走完不松手，
        # 留给下一次 play_steps 接手——模型思考的那 600~700ms 里角色继续走，
        # 而不是每轮松手停住（用户 2026-11-05 明确要求连续输入）。
        self._held_vks: List[int] = []
        # 前台焦点缓存：确认过在前台之后，除非前台变了，否则不再每步重抢一次。
        self._focused: bool = False

    # ---- discovery -------------------------------------------------
    def windows(self, running: Optional[bool] = None) -> Dict[str, Any]:
        """枚举窗口。running 显式给值时跳过 tasklist（每轮省 0.5s 子进程）。

        按键/截帧的热路径只用窗口句柄判断"在不在"，进程存活由 main_hwnd 的缓存校验兜底；
        只有真的需要知道"进程跑没跑"（重启、诊断）时才让 running=None 去跑 tasklist。
        """
        import subprocess
        exe = self.match.get("exe")
        running = True
        if running is None and exe:
            running = exe.lower() in subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq %s" % exe], capture_output=True, text=True).stdout.lower()
        main = ut_harness.find_window(self.match.get("title") or "", exe=exe or None)
        if main is None and (self.match.get("title") or exe):
            main = ut_harness.find_window(self.match.get("title") or exe, exe=exe or None)
        dialogs = []
        if main and self.cfg.get("kill_dialogs"):
            for w in ut_harness.enum_windows():
                if w["pid"] == main["pid"] and ut_harness._class_of(w["hwnd"]) in self.cfg["kill_dialogs"]:
                    dialogs.append(w)
        return {"main": main, "dialogs": dialogs,
                "process_running": bool(running) if running is not None else None}

    def prepare(self) -> Dict[str, Any]:
        state = self.windows(running=False)
        if not state["main"]:
            raise AdapterError("window not found for profile %s (match=%s, running=%s)"
                               % (self.profile.game_id, self.match, state["process_running"]))
        report = {"dialogs": len(state["dialogs"]), "cleared": 0}
        for d in state["dialogs"]:
            ut_harness.u32.PostMessageW(d["hwnd"], 0x0010, 0, 0)
            report["cleared"] += 1
        if report["cleared"]:
            time.sleep(0.25)
            state = self.windows()
        report["foreground"] = self.focus_main().get("game") if state["main"] else False
        return report

    def quick_focus(self) -> bool:
        """一次 preflight 只做一次快路径：窗口已在前台就什么都不做。

        实测（2026-11-04）：prepare() 每次都跑 tasklist + 枚举窗口 + 切 IME，一次 1.1s，
        而快系统每步只要 0.5s——preflight 比决策还贵。窗口在前台时按游戏原生优先级
        本来就能收到输入，不需要再抢一次焦点。
        """

        try:
            hwnd = self.main_hwnd()
        except Exception:  # noqa: BLE001
            return False
        if ut_harness.u32.GetForegroundWindow() == hwnd:
            self._focused = True
            return True
        self._focused = bool(self.focus_main().get("game"))
        return self._focused

    def focus_main(self) -> Dict[str, Any]:
        """把握着屏幕的进程抢到前台，然后把游戏窗口顶上来。

        实测（2026-11-04）：本 agent 进程没有前台权限时 sethwnd_foreground 会失败
        （SetForegroundWindow failed），而 ut_harness.grant_foreground 是**借持屏进程**的
        ASFW_ANY 来拿权限——前提是那个进程（pylisten / Electron 宿主）本身就是前台。
        所以顺序必须是：先让持屏进程回到前台 → 再顶游戏窗口；反过来，
        游戏被顶到前台后持屏进程不再是前台，grant 就永远失败，按键全部打不进去。
        """

        attempts: List[Dict[str, Any]] = []
        pid = ut_harness.pylisten_pid()
        screen_hwnd = ut_harness.top_window_of_pid(pid) if pid else None
        granted = bool(ut_harness.sethwnd_foreground(screen_hwnd)) if screen_hwnd else False
        attempts.append({"try": "pylisten_window", "pid": pid, "hwnd": screen_hwnd, "ok": granted})
        if not granted:
            # 持屏进程没有可见窗口时，直接借它的前台权限（ASFW_ANY）。
            granted = bool(ut_harness.grant_foreground())
            attempts.append({"try": "grant_foreground", "ok": granted})
        ok = ut_harness.sethwnd_foreground(self.main_hwnd())
        attempts.append({"try": "game_foreground", "ok": ok})
        if not ok:
            # 最后的可靠通路：PostMessage 直接投递到游戏窗口队列，不需要前台权限。
            # UT 的 GameMaker 运行时**确实响应 post 进来的 WM_KEYDOWN**（实测反复推进过画面），
            # 所以这条退化路径不是"假装成功"，而是真正能让游戏动起来的那条路。
            ok = bool(self.post_keys_raw(self.main_hwnd()))
            attempts.append({"try": "post_message_only", "ok": ok})
        return {"screen_pid": pid, "screen_hwnd": screen_hwnd, "granted": granted,
                "game": ok, "attempts": attempts}

    def post_keys_raw(self, hwnd: int) -> bool:
        """投一个无害的按键（enter）确认窗口队列可写——不通就返回 False，绝不谎报。"""

        try:
            ut_harness.post_vk(hwnd, 0x0D)
            return True
        except Exception:  # noqa: BLE001 - 队列不可写就是 False
            return False

    def main_hwnd(self, force: bool = False) -> int:
        """游戏主窗口句柄，带缓存。

        每次抓帧都重新枚举窗口 = 每轮白付一次 tasklist 子进程（~1.1s）。
        这里缓存句柄，只在窗口已失效（进程退出/窗口重建）时才重新发现。
        """
        if not force and self._hwnd is not None:
            try:
                _x, _y, w, h = ut_harness.client_rect(ctypes.c_void_p(self._hwnd))
                if w > 0 and h > 0:
                    return self._hwnd
            except Exception:  # noqa: BLE001 - 句柄失效就重新发现
                pass
        t0 = time.time()
        state = self.windows()
        if not state["main"]:
            self._hwnd = None
            raise AdapterError("window not found for profile %s" % self.profile.game_id)
        self._hwnd = state["main"]["hwnd"]
        self._pid = state["main"].get("pid")
        self._discover_ms = (time.time() - t0) * 1000.0
        return self._hwnd

    # ---- perception -------------------------------------------------
    def fit_size(self) -> Optional[Tuple[int, int]]:
        """画面捕获器自适配：按 profile 的 capture 段决定取多大像素。

        口径：**默认按游戏原生分辨率原样取像素**，不放大也不缩小。
        只改窗口大小，绝不最大化/全屏——全屏会改游戏渲染模式并霸占桌面。
        放大是按 token 买像素：画面本身就是 640x480，放大到 1280x960 只是把
        同样的信息铺到 4 倍面积上，vision token 翻 4 倍却不多一个字。只有
        profile 显式写 min_readable 且确实读不出来时才放大。
        """
        cfg = self.capture_cfg or {}
        native = cfg.get("native") or {}
        target = cfg.get("target") or {}
        if native.get("width") and native.get("height"):
            nw, nh = int(native["width"]), int(native["height"])
            factor = 1
            readable = int(cfg.get("min_readable") or 0)
            if readable and nw and readable > nw:
                factor = max(1, int(math.ceil(readable / float(nw))))
            return (nw * factor, nh * factor)
        if target.get("width") and target.get("height"):
            return (int(target["width"]), int(target["height"]))
        return None

    def _ensure_front(self, hwnd: int) -> Dict[str, Any]:
        """截帧前的可见性自愈：摆窗口 → 需要时抢前台。返回本次做了什么，供诊断查。

        - 会话首次：call capture_ready() 把窗口摆进屏幕（不最大化、不抢前台）。
        - 之后每次：只有"窗口当前被盖住 / 出屏 / 不在前台"时才动手。
          已经可见就 0 成本跳过——每帧都抢焦点会打断用户，也拖慢热路径。
        - 可见比例仍 < 0.5 时再抢一次前台（SetForegroundWindow 不会把窗口挪走，
          但会把遮挡它的窗口压下去）。抢不到就算了，交给 grab_visible 的兜底路径。
        """
        report: Dict[str, Any] = {"ready": False, "foreground": False, "visible_ratio": None}
        try:
            if not self._capture_ready:
                self._capture_ready = ut_harness.capture_ready(hwnd) or {}
            report["ready"] = bool(self._capture_ready.get("ok"))
            ratio = ut_harness.visible_ratio(hwnd)
            fg = ut_harness.foreground_hwnd()
            if ratio >= 0.95 and fg == int(hwnd):
                report["visible_ratio"] = ratio
                report["foreground"] = True
                self._last_front = report
                return report
            if ratio < 0.95:
                # 窗口位置/尺寸不对（出屏、太小）就再摆一次。
                self._capture_ready = ut_harness.capture_ready(hwnd) or self._capture_ready
                ratio = ut_harness.visible_ratio(hwnd)
            if ratio < 0.5 or ut_harness.foreground_hwnd() != int(hwnd):
                ut_harness.sethwnd_foreground(hwnd)
                time.sleep(0.12)
            report["visible_ratio"] = ut_harness.visible_ratio(hwnd)
            report["foreground"] = ut_harness.foreground_hwnd() == int(hwnd)
        except Exception as error:  # noqa: BLE001 - 摆窗口失败不该打断截帧
            report["error"] = "%s: %s" % (type(error).__name__, error)
        self._last_front = report
        return report

    CAPTURE_MODES = ("wgc", "screen", "printwindow")

    def _crop_to_client(self, image: Any, hwnd: int, native: Any) -> Tuple[Any, Dict[str, Any]]:
        """把 WGC 整窗画布裁到客户区（去掉标题栏/边框）。

        画布尺寸与窗口矩形不一定相等（DPI 缩放），所以按比例换算偏移；
        裁出来的面积小于画布一半时**不裁**，退回原有的内容包围盒检测——宁可带一点边框，
        也不能把游戏画面裁掉。
        """

        try:
            cx, cy, cw, ch = ut_harness.client_rect(hwnd)
            wx, wy, ww, wh = ut_harness.window_rect(hwnd)
        except Exception as error:  # noqa: BLE001 - 拿不到矩形就退回旧路径
            return image, {"cropped": False, "client_crop_error": "%s: %s" % (type(error).__name__, error)}
        if ww <= 0 or wh <= 0 or cw <= 0 or ch <= 0:
            return image, {"cropped": False, "client_rect": [cx, cy, cw, ch]}
        sx = image.width / float(ww)
        sy = image.height / float(wh)
        box = (
            int(round((cx - wx) * sx)),
            int(round((cy - wy) * sy)),
            int(round((cx - wx + cw) * sx)),
            int(round((cy - wy + ch) * sy)),
        )
        left = max(0, min(box[0], image.width - 1))
        top = max(0, min(box[1], image.height - 1))
        right = max(left + 1, min(box[2], image.width))
        bottom = max(top + 1, min(box[3], image.height))
        area = (right - left) * (bottom - top)
        info = {"cropped": False, "client_box": [left, top, right, bottom],
                "client_size": [cw, ch], "canvas": list(native)}
        if area < 0.5 * image.width * image.height:
            info["too_small"] = True
            return image, info
        return image.crop((left, top, right, bottom)), dict(info, cropped=True)

    def _wgc_grab(self, fmt: str, quality: int, fit: Any = "auto", crop: bool = False) -> Tuple[bytes, Dict[str, Any]]:
        """走 WGC：抓窗口自己渲染的表层，不看屏幕、不怕被遮。

        这是默认路径。返回 (编码后字节, info)。首次调用会建 D3D11 设备 + 帧池（约 150ms），
        之后每帧只取新帧（实测 18~34ms），所以 WgcCapture 必须**长驻**在这个 adapter 上。
        """
        from PIL import Image

        hwnd = self.main_hwnd()
        cap = getattr(self, "_wgc", None)
        if cap is None or cap.hwnd != hwnd:
            if cap is not None:
                try:
                    cap.close()
                except Exception:  # noqa: BLE001
                    pass
            cap = _load_wgc_capture().WgcCapture(hwnd)
            self._wgc = cap
        frame = cap.grab(timeout=2.0)
        image = Image.frombytes("RGBA", (frame.width, frame.height), frame.bgra(), "raw", "BGRA", frame.width * 4)
        image = image.convert("RGB")  # JPEG 不接受 RGBA，先转三通道
        # WGC 画布是整窗（966x760），上面一条是窗口标题栏，下面是游戏画面 + 大片底色。
        # 实测把这片底色喂给策略模型，2B 会当成"黑屏，不用操作"而输出空按键组；
        # 只裁掉标题栏（宽度/高度原样保留，按包围盒裁会把真实画面切掉），再缩放编码。
        native = (frame.width, frame.height)
        crop_info: Dict[str, Any] = {"cropped": False}
        if crop:
            # 先按**客户区**裁：WGC 画布是整窗（含标题栏），标题栏和窗口边框喂给策略
            # 模型只会让它分心（实测带标题栏的 Undertale 标题画面被读成「无需操作」）。
            image, crop_info = self._crop_to_client(image, hwnd, native)
            if not crop_info.get("cropped"):
                import screen_crop

                crop_info = screen_crop.crop_content(image)
                image = crop_info.pop("image")
                crop_info["native"] = native
        # fit=None 表示原尺寸直给：策略模型要读屏幕上的小字（"PRESS Z OR ENTER"），
        # 缩到 640x480 会把 Z 读成 2，实测就这么按错键的。README 也要求原尺寸 quality 90。
        image = _fit_image(image, self.fit_size() if fit == "auto" else fit)
        info = {
            "source": "wgc",
            "width": image.width,
            "height": image.height,
            "native": native,
            "frames": cap.frames,
            "captured_at": frame.captured_at,
        }
        if crop_info.get("cropped"):
            info["cropped"] = True
            info["crop_top"] = crop_info.get("top")
        return _encode_image(image, fmt, quality), info

    def grab(self, fmt: str = "JPEG", quality: int = 85, fit: Any = "auto",
             crop: bool = True) -> Dict[str, Any]:
        """抓一帧到**内存**，绝不落盘。

        高频直播场景每轮都要看画面，写盘就是拿硬盘寿命换零收益；
        返回的 data 直接 base64 给模型即可。返回体里的 data 是原始字节，
        调用方拿走后自行编码，info 里没有 data 键。

        默认走 WGC（2026-11-04 实测决定）：屏幕 BitBlt 在窗口被 DSH 盖住时
        拿到的是**别人的画面**；PrintWindow 给的是左边 184px 全黑的错构图。
        只有 WGC 能稳定拿到窗口自己的 966x760 真画面。
        WGC 不可用（老系统/跨会话/窗口已销毁）时按 wgc → screen → printwindow 逐级降级，
        并在 info["fallback_chain"] 里记录真实走过的路，上层据此判断画面可不可信。
        强制某条路径用环境变量 BETTERAGI_CAPTURE=wgc|screen|printwindow。

        crop=True（默认）只作用于 WGC：先检测窗口标题栏的下沿并裁掉，再缩放编码。
        info["cropped"]/info["crop_top"] 会如实记录裁到哪里，取证时能对得上。
        """
        hwnd = self.main_hwnd()
        requested = (os.environ.get("BETTERAGI_CAPTURE") or "wgc").strip().lower()
        if requested not in self.CAPTURE_MODES:
            requested = "wgc"

        chain: List[str] = []
        info: Dict[str, Any] = {}
        data = b""

        order = [requested] + [m for m in ("wgc", "screen", "printwindow") if m != requested]
        for mode in order:
            chain.append(mode)
            try:
                if mode == "wgc":
                    data, info = self._wgc_grab(fmt, quality, fit, crop)
                elif mode == "screen":
                    front = self._ensure_front(hwnd)
                    data, info = ut_harness.grab_visible(
                        hwnd, fmt=fmt, quality=quality,
                        fit=self.fit_size() if fit == "auto" else fit,
                    )
                    info = dict(info or {})
                    info["front"] = front
                    if info.get("source") != "desktop" or ut_harness.is_blank(data):
                        data = b""
                else:
                    self._ensure_front(hwnd)
                    data, info = ut_harness.grab_bytes(
                        hwnd, fmt=fmt, quality=quality,
                        fit=self.fit_size() if fit == "auto" else fit,
                    )
                    info = dict(info or {})
                    info["front"] = getattr(self, "_last_front", None)
            except Exception as error:  # noqa: BLE001 - 任何抓帧异常都只是降级理由
                info = {"error": "%s: %s" % (type(error).__name__, error)}
                data = b""
            if data and not ut_harness.is_blank(data):
                break

        if not data:
            info = dict(info or {})
            info["degraded"] = "all_capture_paths_failed"
        info["hwnd"] = hwnd
        info["capture"] = requested
        info["fallback_chain"] = chain
        if len(chain) > 1:
            info["degraded"] = info.get("degraded") or "fell_back_to_%s" % chain[-1]
        info["data"] = data
        # 录播旁路：一次抓帧同时喂给 ffmpeg，不让采集跑两遍（设计 03 C.13）。
        # tap 自己吞异常——录播坏了绝不能把直播带崩。
        tap = getattr(self, "frame_tap", None)
        if data and callable(tap):
            try:
                tap(data)
            except Exception as error:  # noqa: BLE001
                info["record_error"] = "%s: %s" % (type(error).__name__, error)
        return info

    def capture(self, path: str) -> Dict[str, Any]:
        """把当前帧显式存到 path——只给诊断/取证用，直播热路径禁止调用。"""
        info = self.grab()
        data = info.pop("data", b"")
        if data:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(data)
            info["path"] = str(path)
            info["bytes"] = len(data)
        return info

    def close(self) -> None:
        """释放长驻的 WGC 会话（进程退出前或换窗口后调用）。"""
        cap = getattr(self, "_wgc", None)
        if cap is not None:
            try:
                cap.close()
            except Exception:  # noqa: BLE001
                pass
            self._wgc = None

    # ---- action -----------------------------------------------------
    def resolve(self, name: str) -> List[str]:
        """Map an agent-facing key name to physical keys from the profile.

        2026-11-06：名字匹配一律忽略大小写。脚本词表要求首字母大写（Up / Space），
        profile 里存的是小写（up），两边一撞就报「键不在 keymap 里」，方向键根本没
        按出去——线上表现就是「让角色向上走但角色一动不动」。
        """
        if name in self.keymap:
            return list(self.keymap[name])
        if name in ut_harness.UT_KEYS:  # physical key given directly
            return [name]
        low = str(name).lower()
        for alias, physical in self.keymap.items():
            if alias.lower() == low:
                return list(physical)
        for physical in ut_harness.UT_KEYS:
            if physical.lower() == low:
                return [physical]
        raise AdapterError("key %r is not in profile %s keymap (known: %s)"
                           % (name, self.profile.game_id, ", ".join(sorted(self.keymap))))

    def post_keys(self, keys: List[str], wait: float = 0.8) -> Dict[str, Any]:
        """不抢前台，直接把按键 PostMessage 给游戏窗口（前台抢不到时的兜底通道）。

        为什么需要：SetForegroundWindow 需要前台权限，而权限是**借持屏进程**的——
        一旦游戏已经在后台、持屏进程也不在前台，这条链就断了（grant_foreground 返回
        False）。但 PostMessage 进窗口队列不需要前台，Undertale 实测认这个。
        """
        hwnd = self.main_hwnd()
        vkmap = ut_harness.key_table()
        banned = forbidden_vks()
        pressed: List[str] = []
        skipped: List[str] = []
        for name in list(keys):
            try:
                physical = self.resolve(name)
            except AdapterError:
                skipped.append(name)
                continue
            vk = vkmap.get(str(physical[0]).lower())
            if vk is None or vk in banned:
                # 禁键和未知键一样处理：静默跳过，绝不发给游戏。
                skipped.append(name)
                continue
            ut_harness.post_vk(hwnd, vk)
            pressed.append("%s->%s" % (name, physical[0]))
            time.sleep(0.12)
        time.sleep(max(0.0, float(wait)))
        return {"pressed": pressed, "skipped_keys": skipped, "via": "post", "game": self.profile.game_id}

    def raw_press(self, spec: str) -> Dict[str, Any]:
        """直接按一个物理键（不走 profile 语义 keymap）。

        spec 形如 "k" / "enter" / "shift+a"（组合键按顺序同时按下）。
        为什么要有这条：命名屏、输入框这类界面要的是"按键本身"，语义名
        （confirm/cancel）在那一屏没有意义——实测按 confirm 再按也进不去。
        物理键表见 ut_harness.KEYMAP，禁键仍在下发层被拦。
        """
        hwnd = self.main_hwnd()
        focused = ut_harness.u32.GetForegroundWindow() == hwnd
        if not focused:
            focused = bool(self.focus_main().get("game"))
        self.ensure_input()
        tune = self.press_tune()
        parts = [p.strip().lower() for p in str(spec or "").split("+") if p.strip()]
        table = ut_harness.key_table()
        vks: List[int] = []
        skipped: List[str] = []
        for part in parts:
            vk = table.get(part)
            if vk is None or ut_harness.is_forbidden(vk):
                skipped.append(part)
                continue
            vks.append(vk)
        if not vks:
            return {"pressed": [], "skipped_keys": skipped or parts, "via": "none",
                    "game": self.profile.game_id}
        if focused:
            ut_harness.press_vks(vks, hold_ms=tune["hold_ms"])
            via = "sendinput"
        else:
            for vk in vks:
                ut_harness.post_vk(hwnd, vk)
            via = "post"
        time.sleep(tune["gap_ms"] / 1000.0)
        return {"pressed": ["%s->%s" % (spec, "+".join(parts))], "skipped_keys": skipped,
                "via": via, "focused": focused, "game": self.profile.game_id}

    def type_text(self, text: str, delay_ms: float = 45.0) -> Dict[str, Any]:
        """往当前焦点窗口输入真正的字符（unicode），用于命名/输入框。

        按键链路（VK 码）打不出大小写正确的一串字母，而且 Undertale 的命名屏
        实测连单个字母都不认（按 K 名字不变）——那一屏读的是 TextInput 事件，
        只有字符注入能进。调用方负责先确认窗口在前台。
        """
        hwnd = self.main_hwnd()
        focused = ut_harness.u32.GetForegroundWindow() == hwnd
        if not focused:
            focused = bool(self.focus_main().get("game"))
        self.ensure_input()
        n = ut_harness.type_unicode(str(text or ""), delay_ms=delay_ms)
        return {"typed": str(text or ""), "count": n, "focused": focused,
                "via": "unicode", "game": self.profile.game_id}

    def ensure_input(self) -> Dict[str, Any]:
        """输入法守卫：中文输入法会先截获按键，游戏收不到。

        实测（2026-11-04）：verify=True 走一遍扫描要 56ms，后面每次按键都白付一次。
        守卫的结果只取决于"持屏进程的键��布局"，同一会话内不变——缓存它，
        只在用户可能切过输入法的轮次（由调用方显式传 force）重扫。
        """
        if self._ime_guard is not None:
            return self._ime_guard
        guard = ut_harness.ensure_latin_input(self.main_hwnd())
        if guard.get("safe"):
            self._ime_guard = guard
        return guard

    def press_tune(self) -> Dict[str, float]:
        """实测按键时序常数：不靠猜，起手用默认值，用一次并落为会话常量。

        默认值来自实测（2026-11-04，本机 641x480 窗口）：
        SendInput 一组 keydown/keyup 约 15ms，每键之间留 80ms 让游戏帧循环吃到，
        按完等 90ms 再收尾，确保最后一个键被渲染进下一帧。
        """
        if self._press_tune is None:
            self._press_tune = {
                "hold_ms": float(os.environ.get("BETTERAGI_KEY_HOLD_MS") or 16.0),
                "gap_ms": float(os.environ.get("BETTERAGI_KEY_GAP_MS") or 24.0),
                "settle_ms": float(os.environ.get("BETTERAGI_KEY_SETTLE_MS") or 90.0),
            }
        return self._press_tune

    def act(self, keys: List[str], wait: float = 0.8, capture_after: bool = True,
            capture: bool = True) -> Dict[str, Any]:
        """按键。

        实测（2026-11-04）拆账：prepare 718ms + 每键 real_press 204ms，一次两键就 1.1s。
        其中 prepare 的 700ms 是 tasklist + 枚举 + 抢焦点全量重跑——窗口在前台时这些
        工作全是白做的。热路径改成：焦点已确认就不抢；输入法守卫走缓存；
        按键用 SendInput 原语（29ms/次）而不是带 80/90ms 固定等待的 real_press。
        """
        hwnd = self.main_hwnd()
        # 焦点判定**只查一次 GetForegroundWindow**（0ms）。抢焦点/重枚举窗口是"确认丢焦之后"
        # 才付的代价——前台本来就对时多付一次 focus_main 就是白等。
        focused = ut_harness.u32.GetForegroundWindow() == hwnd
        if not focused:
            focused = bool(self.focus_main().get("game"))
        self._focused = focused
        ime = self.ensure_input()
        tune = self.press_tune()
        banned = forbidden_vks()
        pressed: List[str] = []
        skipped: List[str] = []
        for name in keys:
            physical = self.resolve(name)
            vk = ut_harness.key_table().get(str(physical[0]).lower())
            if vk in banned:
                skipped.append(name)
                continue
            # 抢不到前台才退回 PostMessage（不需要前台权限）；抢到就走 SendInput。
            if focused:
                ut_harness.press_vks([vk], hold_ms=tune["hold_ms"])
            else:
                ut_harness.post_vk(hwnd, vk)
            pressed.append("%s->%s" % (name, physical[0]))
            # 实测（2026-11-04 按键矩阵）：gap 0 与 80ms 的输入命中率一致（帧哈希每次都变），
            # 多键之间唯一需要的是"别在同一帧里被合并"，hold 16ms 已足够。默认 gap 收到 24ms。
            time.sleep(tune["gap_ms"] / 1000.0)
        time.sleep(max(0.0, float(wait)))
        out: Dict[str, Any] = {"pressed": pressed, "skipped_keys": skipped,
                              "game": self.profile.game_id,
                              "input_safe": ime.get("safe"), "layout": ime["after"]["layout"]}
        # capture_after 与 capture 分开：前者是"动作语义上要不要回帧"，
        # 后者是"这一次调用要不要真的抓"——热路径上钱已经花在决策用的一帧上了，
        # 再抓一遍纯属重复付账（实测每帧 30~50ms 的编码 + 窗口合成）。
        if capture_after and capture:
            import base64
            frame = self.grab()
            data = frame.pop("data")
            out.update(frame)
            out["jpeg_b64"] = base64.b64encode(data).decode()
            out["mime"] = "image/jpeg"
        return out

    def _vk_for_step_key(self, name: str, banned: set):
        """把一步里的键名落到虚拟键码上（GameScaling token -> 物理键）。

        模型给的是**训练 token**（Z / Up / Enter / Back / One），不是 profile 里的语义名
        （confirm/cancel）。先按 token 查表，再退回 profile keymap 兼容人手写的语义名，
        最后才当物理键名硬试——顺序不能倒，否则 `Z` 会先被 profile 的 z 规则吃掉。
        """
        token = str(name or "").strip()
        if not token:
            return None, ""
        physical = ""
        try:
            import gamescaling_vocab as vocab_mod

            physical = vocab_mod.keymap_name(token)
        except Exception:  # noqa: BLE001 - 词表模块不可用就退回 profile
            physical = ""
        if not physical:
            try:
                physical = str(self.resolve(token)[0])
            except AdapterError:
                physical = ""
        if not physical:
            physical = token.lower()
        vk = ut_harness.key_table().get(physical.lower())
        if vk is None or vk in banned:
            return None, ""
        return vk, physical

    def play_steps(self, steps: List[List[str]], step_ms: int = 33,
                   clock: Any = None, keep_held: bool = False) -> Dict[str, Any]:
        """按 gamescaling 的**分步**动作序列真实按键，只回状态不回帧。

        一次 gamescaling 决策 = 未来 200ms、6 步、每步 33ms。组 i 是第 i 步要按的键，
        组内同时按下。

        **长按语义（README §3，2026-11-04 补齐）**：同一个键出现在相邻两组里表示一直按住，
        不是重复按两下。所以这里维护 held 集合：每步只补按"新出现的键"、只松"这一步不再
        需要的键"，6 步走完统一松开。之前用 press_vks 逐组按，等于每 33ms 点一下——
        走路走不起来，蓄力也蓄不上。

        没学过的键（查不到 VK）跳过而不是整步失败：宁可少按一下，也不让主播卡死。

        **时序回传（设计 03 §C.13 P1）**：每步记 plan_t（按 step_ms 递增的计划毫秒）、
        actual_t（真正开始执行这一步的单调毫秒，都相对本次调用起点）、lag_ms =
        actual_t - plan_t。clock 可以传零参可调用（返回秒，如 time.monotonic）或带
        now_ms() 的对象（如 timeline.Clock）；不传就退回 time.monotonic。

        每步还带 pressed/skipped_reason：pressed=False 表示这一步想按的键一个都没落到
        游戏里（窗口抢不到前台 -> no_window_focus；键名全查不到 VK -> unknown_key:*）；
        部分键没落地的成功步骤在 skipped_reason 里写 skipped_keys:*。空组是模型显式
        的 no-op，算成功（pressed=True）。
        """
        hwnd = self.main_hwnd()
        ime = self.ensure_input()
        banned = forbidden_vks()
        pressed: List[str] = []
        skipped: List[str] = []
        # 从上一轮接手还按着的键：键集合没变就不用松开再按下（那样会丢一帧输入，
        # 而且角色的走动会一顿一顿）。
        held: List[int] = list(self._held_vks)
        self._held_vks = []
        step_results: List[Dict[str, Any]] = []
        steps = list(steps)[:6]
        t0_ms = _clock_ms(clock)
        failed = False
        try:
            for index, step in enumerate(steps):
                plan_t = int(round(index * float(step_ms)))
                actual_t = int(round(_clock_ms(clock) - t0_ms))
                tick = time.monotonic()
                focused = ut_harness.u32.GetForegroundWindow() == hwnd
                if not focused:
                    ut_harness.sethwnd_foreground(hwnd)
                    # 抢完再读一次：SetForegroundWindow 失败时按键会打进别人的窗口，
                    # 这一步必须如实记成"被吞"，不能让历史以为按进去了。
                    focused = ut_harness.u32.GetForegroundWindow() == hwnd
                if not focused:
                    if held:
                        ut_harness.key_up_vks(held)
                        held = []
                    skipped.extend(name for name in step if name not in skipped)
                    step_results.append({
                        "index": index, "plan_t": plan_t, "actual_t": actual_t,
                        "lag_ms": actual_t - plan_t, "pressed": False,
                        "skipped_reason": "no_window_focus",
                    })
                    failed = True
                    break
                target: List[int] = []
                step_skipped: List[str] = []
                for name in step:
                    vk, physical = self._vk_for_step_key(name, banned)
                    if vk is None:
                        if name not in skipped:
                            skipped.append(name)
                        if name not in step_skipped:
                            step_skipped.append(name)
                        continue
                    if vk not in target:
                        target.append(vk)
                    label = "%s->%s" % (name, physical)
                    if label not in pressed:
                        pressed.append(label)
                release = [vk for vk in held if vk not in target]
                if release:
                    ut_harness.key_up_vks(release)
                    held = [vk for vk in held if vk in target]
                fresh = [vk for vk in target if vk not in held]
                if fresh:
                    ut_harness.key_down_vks(fresh)
                    held.extend(fresh)
                # 这一步生没生效：空组是显式 no-op；有键但一个都没落地 = 被吞/被跳过。
                if not step:
                    step_pressed, skipped_reason = True, ""
                elif not focused:
                    step_pressed, skipped_reason = False, "no_window_focus"
                elif not target:
                    step_pressed = False
                    skipped_reason = "unknown_key:" + ",".join(step_skipped)
                else:
                    step_pressed = True
                    skipped_reason = ("skipped_keys:" + ",".join(step_skipped)) if step_skipped else ""
                step_results.append({
                    "index": index,
                    "plan_t": plan_t,
                    "actual_t": actual_t,
                    "lag_ms": actual_t - plan_t,
                    "pressed": step_pressed,
                    "skipped_reason": skipped_reason,
                })
                # 对齐到下一步的边界：这一拍必须刚好用掉 step_ms，晚了就顺延不补偿。
                wait = float(step_ms) / 1000.0 - (time.monotonic() - tick)
                if wait > 0:
                    time.sleep(wait)
        except BaseException:
            failed = True
            raise
        finally:
            # keep_held=True 且这轮正常走完时才不松手：把这批键留给下一次
            # play_steps（模型思考期间角色继续走）。异常/中断一律松开——
            # 卡住的 key-down 会让角色一直往一个方向走，比不按键更糟。
            if held and keep_held and not failed:
                self._held_vks = list(held)
            elif held:
                ut_harness.key_up_vks(held)
                held = []
        return {"pressed": pressed, "skipped_keys": skipped,
                "game": self.profile.game_id, "steps": len(steps),
                "step_ms": int(step_ms), "hovered": len(held),
                "input_safe": ime.get("safe"), "layout": ime["after"]["layout"],
                "step_results": step_results}

    def release_held(self) -> Dict[str, Any]:
        """松开跨决策保持按住的键。

        意图结束、收手、熔断、异常退出时必须调——按住的 key-down 不松，角色会
        一直往一个方向走，比不按键更糟。
        """
        held = list(self._held_vks)
        self._held_vks = []
        if held:
            ut_harness.key_up_vks(held)
        return {"released": len(held), "game": self.profile.game_id}

    def restart(self, clear_save: bool = True) -> Dict[str, Any]:
        """按 profile 的 launch 段重启游戏：杀进程 → 清档 → 启动 → 等窗口。"""
        launch = self.cfg.get("launch") or {}
        exe = launch.get("exe") or self.match.get("exe")
        if not exe:
            raise AdapterError("profile 没有配置 launch.exe / match.exe，无法重启游戏")
        saves = launch.get("save_paths") or []
        saves = [str(Path(p).expanduser()) for p in saves]
        return ut_harness.restart_game(exe, Path(exe).name, saves, clear=clear_save)

    def learn_key(self, name: str, physical: List[str], confidence: float = 0.6) -> Dict[str, Any]:
        """Persist a learned binding (name -> physical keys) into the profile. M4 learning path."""
        try:
            from . import profiles as profiles_mod  # type: ignore
        except ImportError:  # pragma: no cover
            import profiles as profiles_mod  # type: ignore
        out = profiles_mod.apply_learned_key(self.profile, name, physical, confidence=confidence)
        # 学到的键必须立刻对本进程生效：keymap 是实例缓存，只写盘不刷新的话
        # 下一步 post_keys 还是会说 skipped_keys（实测踩过这个坑）。
        self.keymap[name] = list(physical)
        return out