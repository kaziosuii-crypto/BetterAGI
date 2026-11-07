#!/usr/bin/env python3
"""BetterAGI 统一命令行入口。"""

from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent
HEADLESS_MARKER = b"bettergi-headless-cli-v1"
COMMANDS = {
    "agent": ROOT / "dsh" / "agent_client.py",
    "player": ROOT / "player" / "run_shadow.py",
    "bridge": ROOT / "lumine" / "lumine_bridge.py",
    "motor": ROOT / "lumine" / "autodl_server.py",
    "localmotor": ROOT / "lumine" / "local_motor.py",
    "episode": ROOT / "session" / "run_episode.py",
    "record": ROOT / "session" / "record_session.py",
    "session": ROOT / "session" / "show_session.py",
    "render": ROOT / "session" / "render_session.py",
    "journey": ROOT / "player" / "run_shadow.py",
    "setup": ROOT / "player" / "setup_mvp.py",
    "calibrate": ROOT / "player" / "dialogue_calibration.py",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="统一控制 BetterAGI 宿主、玩家、录制和后期渲染。",
        epilog=(
            "例如：betteragi agent --help；betteragi episode --help；"
            "betteragi host start --exe 路径。游戏本身仍需可见画面；"
            "宿主需要管理员权限时，请在已提升的终端启动并自行确认 UAC。"
        ),
    )
    parser.add_argument("command", choices=["host", "game", "tunnel", "profile", *COMMANDS], help="选择要运行的组件")
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="原样传递给子命令的参数")
    return parser


def _host(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="betteragi host",
        description="以前台进程启动已安装的无界面 BetterGI 宿主。",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start", help="启动具备无界面能力的宿主")
    start.add_argument("--exe", required=True, type=Path, help="BetterGI.exe 的完整路径")
    start.add_argument("--config", "--headless-config", dest="config", required=True, type=Path,
                       help="独立无界面配置文件的绝对路径，不使用图形界面配置")
    start.add_argument("--token-file", type=Path, help="访问令牌文件的绝对路径，不传递令牌内容")
    init = actions.add_parser("init", help="创建独立无界面配置，不覆盖已有文件")
    init.add_argument("--config", required=True, type=Path, help="要创建的配置文件绝对路径")
    init.add_argument("--token-file", required=True, type=Path, help="已存在的令牌文件绝对路径")
    init.add_argument("--port", type=int, default=30648, help="本机回环端口，1024–65535")
    init.add_argument("--player-evidence", type=Path, help="setup 生成的玩家身份配置片段")
    init.add_argument("--dialogue-calibration", type=Path, help="calibrate 生成的对话标定配置片段")
    stop = actions.add_parser("stop", help="安全停止无界面宿主并等待进程退出")
    stop.add_argument("--base-url", default=os.environ.get("BETTERGI_AGENT_BASE_URL", "http://127.0.0.1:30648"),
                      help="本机外部访问服务地址")
    stop.add_argument("--token-file", type=Path, help="令牌文件；省略时读取 BETTERGI_AGENT_TOKEN")
    stop.add_argument("--timeout", type=float, default=30.0, help="关闭等待秒数")
    args = parser.parse_args(arguments)
    if args.action == "stop":
        return _stop_host(args)
    if args.action == "init":
        return _init_host(args)
    executable = args.exe.expanduser().resolve()
    config = args.config.expanduser()
    if not config.is_absolute() or not config.is_file():
        print("宿主启动失败：--config 必须指向已有配置文件的绝对路径。", file=sys.stderr)
        return 2
    if executable.name.casefold() != "bettergi.exe" or not executable.is_file():
        print("宿主启动失败：--exe 必须指向已有的 BetterGI.exe。", file=sys.stderr)
        return 2
    marker = executable.with_name("BetterGI.headless-v1")
    try:
        with marker.open("rb") as handle:
            raw = handle.read(65)
            supported = len(raw) <= 64 and raw.decode("utf-8-sig").rstrip("\r\n") == HEADLESS_MARKER.decode("ascii")
    except (OSError, UnicodeError):
        supported = False
    if not supported:
        print("宿主启动失败：未找到有效的无界面能力标识，拒绝启动旧版图形界面。", file=sys.stderr)
        return 2
    token_file = args.token_file.expanduser() if args.token_file else None
    if token_file is not None and (not token_file.is_absolute() or not token_file.is_file()):
        print("宿主启动失败：--token-file 必须指向已有文件的绝对路径。", file=sys.stderr)
        return 2
    command = [str(executable), "--headless", "--headless-config", str(config.resolve())]
    if token_file is not None:
        command.extend(["--headless-token-file", str(token_file.resolve())])
    try:
        return subprocess.run(command, check=False).returncode
    except OSError as exc:
        print(f"宿主启动失败：{exc}", file=sys.stderr)
        return 1


def _profile_local_path(path: Path) -> Path:
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//"))
            or len(path.parts) > 128 or any(part == ".." or ":" in part for part in path.parts[1:])):
        raise ValueError("档案路径必须是本机绝对路径")
    return path


def _profile_kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _profile_open_no_reparse(kernel, path: Path, *, directory: bool):
    class AttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    access = 0x80 if directory else 0x80000000
    share = 0x3 if directory else 0x1
    flags = 0x00200000 | (0x02000000 if directory else 0)
    handle = kernel.CreateFileW(str(path), access, share, None, 3, flags, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError("无法安全打开档案路径")
    info = AttributeTagInfo()
    if (not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info))
            or info.attributes & 0x400 or bool(info.attributes & 0x10) != directory):
        kernel.CloseHandle(handle)
        raise ValueError("档案路径不能包含重解析点")
    return handle


@contextmanager
def _profile_locked_directories(*directories: Path):
    kernel = _profile_kernel()
    handles = []
    seen = set()
    try:
        for directory in directories:
            for path in reversed((directory, *directory.parents)):
                if path == path.parent:
                    continue
                key = str(path).casefold()
                if key not in seen:
                    handles.append(_profile_open_no_reparse(kernel, path, directory=True))
                    seen.add(key)
        yield kernel
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def _profile_template_bytes(kernel, template: Path) -> bytes:
    import msvcrt

    handle = _profile_open_no_reparse(kernel, template, directory=False)
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("档案模板过大")
    return data


def _profile(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="betteragi profile", allow_abbrev=False,
                                     description="只从明确指定的模板初始化档案，不创建记忆、不连接游戏。")
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init", help="在已有玩家目录中新建档案，不覆盖已有文件")
    init.add_argument("--home", required=True, type=Path, help="已有玩家目录的本机绝对路径")
    init.add_argument("--template", required=True, type=Path, help="已有 JSON 档案模板的本机绝对路径")
    init.add_argument("--output", required=True, type=Path, help="玩家目录内新档案的本机绝对路径")
    args, unknown = parser.parse_known_args(arguments)
    if unknown:
        print("档案命令不接受额外参数或凭据。", file=sys.stderr)
        return 2
    if sys.platform != "win32":
        print("档案初始化需要 Windows 安全路径支持。", file=sys.stderr)
        return 2
    temporary = None
    try:
        home = _profile_local_path(args.home)
        template = _profile_local_path(args.template)
        output = _profile_local_path(args.output)
        if (not output.is_relative_to(home) or output == home or template.suffix.casefold() != ".json"
                or output.suffix.casefold() != ".json"):
            raise ValueError("档案目标必须是玩家目录内的 JSON 文件")
        with _profile_locked_directories(home, template.parent, output.parent) as kernel:
            raw = _profile_template_bytes(kernel, template)
            data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
            sys.path.insert(0, str(ROOT / "player"))
            try:
                from player_profile import profile_from_dict
                profile_from_dict(data, source_path=template)
            finally:
                sys.path.pop(0)
            fd, temporary = tempfile.mkstemp(prefix=".betteragi-profile-", suffix=".tmp", dir=str(output.parent))
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                os.link(temporary, output)
        print("玩家档案已初始化；未创建经历或记忆，也未连接游戏。")
        return 0
    except FileExistsError:
        print("档案初始化失败：目标已存在，不会覆盖。", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, TypeError):
        print("档案初始化失败：路径或模板无效，未创建档案。", file=sys.stderr)
        return 2
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def _ssh_host(value: str) -> str:
    if (not value or value != value.strip() or value.startswith("-") or len(value) > 253
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or any(char in value for char in "@/\\\\%?#")):
        raise ValueError("SSH 主机无效")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        try:
            name = value.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            raise ValueError("SSH 主机无效") from None
        if (not name or len(name) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                               for label in name.split("."))):
            raise ValueError("SSH 主机无效") from None
        return name


def _ssh_port(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,5}", value):
        raise ValueError("SSH 端口无效")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("SSH 端口无效")
    return port


def _ssh_binary() -> str:
    if sys.platform == "win32" and os.environ.get("SystemRoot"):
        candidate = Path(os.environ["SystemRoot"]) / "System32" / "OpenSSH" / "ssh.exe"
        if candidate.is_file():
            return str(candidate)
    executable = shutil.which("ssh.exe" if sys.platform == "win32" else "ssh")
    if not executable:
        raise OSError("未找到 OpenSSH 客户端")
    resolved = Path(executable).resolve()
    if sys.platform == "win32" and (resolved.name.casefold() != "ssh.exe" or str(resolved).startswith(("\\\\", "//"))):
        raise OSError("OpenSSH 客户端无效")
    return str(resolved)


def _stop_tunnel(process) -> bool:
    try:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                if process.poll() is None:
                    raise
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _tunnel(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="betteragi tunnel", allow_abbrev=False,
                                     description="以前台 OpenSSH 进程建立本机回环端口转发，不启动远端机器。")
    parser.add_argument("--host", required=True, help="已存在的 SSH 服务主机")
    parser.add_argument("--ssh-port", required=True, help="SSH 服务端口")
    parser.add_argument("--user", required=True, help="SSH 用户名")
    parser.add_argument("--local-port", required=True, help="本机回环监听端口")
    parser.add_argument("--remote-port", required=True, help="远端回环服务端口")
    parser.add_argument("--identity", type=Path, help="本机现有 SSH 私钥文件的绝对路径")
    parser.add_argument("--known-hosts", type=Path, help="本机现有 known_hosts 文件的绝对路径")
    args, unknown = parser.parse_known_args(arguments)
    if unknown:
        print("SSH 转发不接受额外参数、密码或外网绑定地址。", file=sys.stderr)
        return 2
    try:
        host = _ssh_host(args.host)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", args.user):
            raise ValueError("SSH 用户名无效")
        ssh_port, local_port, remote_port = map(_ssh_port, (args.ssh_port, args.local_port, args.remote_port))
        identity = _local_absolute(str(args.identity), must_exist=True) if args.identity else None
        known_hosts = _local_absolute(str(args.known_hosts), must_exist=True) if args.known_hosts else None
        if known_hosts and '"' in known_hosts:
            raise ValueError("known_hosts 路径无效")
        executable = _ssh_binary()
    except (OSError, ValueError):
        print("SSH 转发参数无效或 OpenSSH 不可用。", file=sys.stderr)
        return 2

    command = [executable, "-F", "none", "-N", "-T", "-q",
               "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
               "-o", "ExitOnForwardFailure=yes", "-o", "GatewayPorts=no",
               "-o", "PermitLocalCommand=no", "-o", "ForwardAgent=no",
               "-p", str(ssh_port), "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
               "-l", args.user]
    if identity:
        command.extend(["-i", identity])
    if known_hosts:
        command.extend(["-o", 'UserKnownHostsFile="' + known_hosts.replace("\\", "/") + '"',
                        "-o", "GlobalKnownHostsFile=none"])
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        command.extend(["-o", "BatchMode=yes"])
    command.extend(["--", host])
    environment = {key: value for key, value in os.environ.items()
                   if not re.search(r"token|secret|password|passphrase|api[_-]?key|credential|ssh_askpass", key, re.IGNORECASE)}
    environment["SSH_ASKPASS_REQUIRE"] = "never"
    process = None
    try:
        process = subprocess.Popen(command, env=environment)
        result = process.wait()
        if result:
            print(f"SSH 转发已退出（状态 {result}）。", file=sys.stderr)
        return result
    except KeyboardInterrupt:
        if process is not None and not _stop_tunnel(process):
            print("SSH 转发退出未确认。", file=sys.stderr)
            return 1
        return 130
    except OSError:
        if process is not None and not _stop_tunnel(process):
            print("SSH 转发退出未确认。", file=sys.stderr)
            return 1
        print("无法启动或等待 OpenSSH。", file=sys.stderr)
        return 1


GAME_WINDOW_TITLES = frozenset({"原神", "Genshin Impact"})
GAME_EXECUTABLES = frozenset({"yuanshen.exe", "genshinimpact.exe"})
GAME_LAUNCHERS = frozenset({"launcher.exe", "hoyoplay.exe"})


def _scan_game_windows() -> tuple[list[int], bool]:
    """枚举窗口并只通过进程路径元数据确认游戏，不访问游戏内存。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD))
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    matches: list[int] = []
    uncertain = False

    @callback_type
    def visit(hwnd, _):
        nonlocal uncertain
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0 or length > 256:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            if not user32.GetWindowTextW(hwnd, title, len(title)) or title.value not in GAME_WINDOW_TITLES:
                return True
            pid = wintypes.DWORD()
            if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
                uncertain = True
                return True
            process = kernel32.OpenProcess(0x1000, False, pid.value)
            if not process:
                uncertain = True
                return True
            try:
                size = wintypes.DWORD(32768)
                path = ctypes.create_unicode_buffer(size.value)
                if not kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                    uncertain = True
                elif Path(path.value).name.casefold() in GAME_EXECUTABLES:
                    matches.append(int(hwnd))
            finally:
                kernel32.CloseHandle(process)
        except Exception:
            uncertain = True
        return True

    if not user32.EnumWindows(visit, 0):
        raise OSError("无法枚举游戏窗口")
    return matches, uncertain


def _running_process_names() -> set[str]:
    """枚举进程名称以避免在窗口尚未出现时重复启动。"""
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("name", wintypes.WCHAR * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(2, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise OSError("无法枚举现有进程")
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        ctypes.set_last_error(0)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        if not found:
            raise OSError("无法读取现有进程")
        names = set()
        while found:
            names.add(entry.name.casefold())
            ctypes.set_last_error(0)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise OSError("现有进程枚举未完成")
        return names
    finally:
        kernel32.CloseHandle(snapshot)


def _foreground_window() -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    return int(user32.GetForegroundWindow() or 0)


def _focus_game_window(hwnd: int) -> bool:
    """只调用标准窗口激活 API，不模拟按键、不绕过焦点保护。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    if user32.IsIconic(hwnd):
        return False
    return bool(user32.SetForegroundWindow(hwnd)) and int(user32.GetForegroundWindow() or 0) == hwnd


def _game(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="betteragi game", description="使用常规 Windows 进程和窗口操作管理原神。")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("status", help="查看游戏窗口与前台状态")
    start = actions.add_parser("start", help="启动显式指定的本机游戏或启动器")
    start.add_argument("--exe", required=True, type=Path, help="官方游戏或启动器的绝对路径")
    actions.add_parser("focus", help="通过标准窗口 API 请求原神前台")
    args, unknown = parser.parse_known_args(arguments)
    if unknown:
        print("游戏命令不接受额外参数或账号凭据。", file=sys.stderr)
        return 2
    if sys.platform != "win32":
        print("游戏命令仅支持 Windows。", file=sys.stderr)
        return 2
    try:
        windows, uncertain = _scan_game_windows()
        if args.action == "status":
            if uncertain:
                print(json.dumps({"gameWindowPresent": None, "gameForeground": None}, ensure_ascii=False))
                print("游戏窗口状态未知；请确认正常的 Windows 权限。", file=sys.stderr)
                return 1
            foreground = _foreground_window()
            print(json.dumps({"gameWindowPresent": bool(windows), "gameForeground": foreground in windows},
                             ensure_ascii=False))
            return 0
        if uncertain:
            print("游戏窗口身份未知，未执行操作；请确认正常的 Windows 权限。", file=sys.stderr)
            return 1
        if args.action == "focus":
            if len(windows) != 1 or not _focus_game_window(windows[0]):
                print("无法切换游戏前台：窗口缺失、重复、最小化或系统拒绝。", file=sys.stderr)
                return 1
            print("已通过系统窗口 API 切换到原神。")
            return 0
        executable = args.exe.expanduser()
        if (not executable.is_absolute() or str(executable).startswith(("\\\\", "//"))
                or not executable.is_file()):
            print("游戏启动失败：--exe 必须是已有的本机绝对文件路径。", file=sys.stderr)
            return 2
        executable = executable.resolve()
        if str(executable).startswith(("\\\\", "//")) or executable.name.casefold() not in GAME_EXECUTABLES | GAME_LAUNCHERS:
            print("游戏启动失败：只接受指定的官方游戏或启动器文件名。", file=sys.stderr)
            return 2
        if windows or _running_process_names() & (GAME_EXECUTABLES | {executable.name.casefold()}):
            print("检测到游戏窗口或相关进程，未重复启动。", file=sys.stderr)
            return 1
        environment = {key: value for key, value in os.environ.items()
                       if not re.search(r"token|secret|password|api[_-]?key|credential", key, re.IGNORECASE)}
        subprocess.Popen([str(executable)], cwd=str(executable.parent), env=environment)
        print("游戏启动请求已发出；登录与画面状态尚未确认。")
        return 0
    except OSError:
        print("游戏操作失败；如需管理员权限，请通过正常 UAC 在相应权限下运行。", file=sys.stderr)
        return 1


def _fields(value, names: Sequence[str], required: Sequence[str] = ()) -> dict:
    if not isinstance(value, dict):
        raise ValueError("配置片段必须为 JSON 对象")
    allowed = {name.casefold(): name for name in names}
    result = {}
    for key, item in value.items():
        name = allowed.get(key.casefold()) if isinstance(key, str) else None
        if name is None or name in result:
            raise ValueError("配置片段包含未知或重复字段")
        result[name] = item
    if not set(required).issubset(result):
        raise ValueError("配置片段缺少必需字段")
    return result


def _unique_object(pairs):
    result = {}
    seen = set()
    for key, value in pairs:
        folded = key.casefold()
        if folded in seen:
            raise ValueError("配置片段包含重复字段")
        seen.add(folded)
        result[key] = value
    return result


def _local_absolute(value, *, must_exist: bool = False) -> str:
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 32 for char in value) or value.startswith(("\\\\", "//"))):
        raise ValueError("配置路径必须是本机绝对路径")
    path = Path(value)
    if not path.is_absolute() or (must_exist and not path.is_file()):
        raise ValueError("配置路径无效或文件不存在")
    resolved = path.resolve()
    if str(resolved).startswith(("\\\\", "//")):
        raise ValueError("不允许网络路径")
    return str(resolved)


def _region(value) -> dict:
    region = _fields(value, ("x", "y", "width", "height"), ("x", "y", "width", "height"))
    if (any(type(item) not in (int, float) or not math.isfinite(item) for item in region.values())
            or region["x"] < 0 or region["y"] < 0 or region["width"] < 0.02 or region["height"] < 0.02
            or region["x"] + region["width"] > 1 or region["y"] + region["height"] > 1):
        raise ValueError("标定区域无效")
    return region


def _covers(outer: dict, inner: dict) -> bool:
    return (outer["x"] <= inner["x"] and outer["y"] <= inner["y"]
            and outer["x"] + outer["width"] >= inner["x"] + inner["width"]
            and outer["y"] + outer["height"] >= inner["y"] + inner["height"])


def _read_fragment(path: Path) -> dict:
    location = _local_absolute(str(path), must_exist=True)
    with open(location, "rb") as handle:
        raw = handle.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("配置片段过大")
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)


def _evidence(value) -> dict:
    evidence = _fields(value, ("databasePath", "playerScope"), ("databasePath", "playerScope"))
    evidence["databasePath"] = _local_absolute(evidence["databasePath"])
    if not isinstance(evidence["playerScope"], str) or not re.fullmatch(r"player-[0-9a-f]{64}", evidence["playerScope"]):
        raise ValueError("玩家身份配置无效")
    return evidence


def _calibration(value) -> dict:
    names = ("playerEvidence", "dialogueCandidate", "dialogueActionAuthorization", "dialogueExitAuthorization")
    root = _fields(value, ("externalAccessConfig",), ("externalAccessConfig",))
    config = _fields(root["externalAccessConfig"], names, names)
    evidence = _evidence(config["playerEvidence"])
    candidate = _fields(config["dialogueCandidate"], ("enabled", "message", "options", "exitFrames"),
                        ("enabled", "message", "options", "exitFrames"))
    if (type(candidate["enabled"]) is not bool or type(candidate["exitFrames"]) is not int
            or not 2 <= candidate["exitFrames"] <= 10):
        raise ValueError("对话候选配置无效")
    candidate["message"], candidate["options"] = _region(candidate["message"]), _region(candidate["options"])
    action_names = ("allowSelect", "allowAdvance", "advanceTemplatePath", "advanceTemplateCertified",
                    "advanceRegion", "possibleOptionsRegion", "optionCoverageConfirmed")
    action = _fields(config["dialogueActionAuthorization"], action_names, ("allowSelect", "allowAdvance"))
    if type(action["allowSelect"]) is not bool or type(action["allowAdvance"]) is not bool:
        raise ValueError("对话动作配置无效")
    for key in ("advanceTemplateCertified", "optionCoverageConfirmed"):
        if key in action and type(action[key]) is not bool:
            raise ValueError("对话动作确认无效")
    if action["allowAdvance"]:
        if not set(action_names).issubset(action) or action["advanceTemplateCertified"] is not True or action["optionCoverageConfirmed"] is not True:
            raise ValueError("推进标定缺少明确确认")
        action["advanceTemplatePath"] = _local_absolute(action["advanceTemplatePath"], must_exist=True)
        action["advanceRegion"], action["possibleOptionsRegion"] = _region(action["advanceRegion"]), _region(action["possibleOptionsRegion"])
        if not all(_covers(action["advanceRegion"], value) for value in
                   (candidate["message"], candidate["options"], action["possibleOptionsRegion"])) or not _covers(action["possibleOptionsRegion"], candidate["options"]):
            raise ValueError("推进标定覆盖范围无效")
    elif set(action) != {"allowSelect", "allowAdvance"}:
        raise ValueError("未启用推进时不能包含推进标定")
    exit_names = ("enabled", "templatePath", "mainTemplateCertified", "region", "possibleDialogueRegion", "coverageConfirmed", "requiredFrames")
    exit_config = _fields(config["dialogueExitAuthorization"], exit_names, ("enabled",))
    if type(exit_config["enabled"]) is not bool:
        raise ValueError("对话退出配置无效")
    if exit_config["enabled"]:
        if (not set(exit_names).issubset(exit_config) or exit_config["mainTemplateCertified"] is not True
                or exit_config["coverageConfirmed"] is not True or type(exit_config["requiredFrames"]) is not int
                or not 2 <= exit_config["requiredFrames"] <= 10):
            raise ValueError("退出标定缺少明确确认")
        exit_config["templatePath"] = _local_absolute(exit_config["templatePath"], must_exist=True)
        exit_config["region"], exit_config["possibleDialogueRegion"] = _region(exit_config["region"]), _region(exit_config["possibleDialogueRegion"])
        if (not _covers(exit_config["region"], exit_config["possibleDialogueRegion"])
                or not all(_covers(exit_config["possibleDialogueRegion"], value) for value in
                           (candidate["message"], candidate["options"]))):
            raise ValueError("退出标定覆盖范围无效")
    elif set(exit_config) != {"enabled"}:
        raise ValueError("未启用退出时不能包含退出标定")
    return {"playerEvidence": evidence, "dialogueCandidate": candidate,
            "dialogueActionAuthorization": action, "dialogueExitAuthorization": exit_config}


def _init_host(args: argparse.Namespace) -> int:
    config, token_file = args.config.expanduser(), args.token_file.expanduser()
    if (not config.is_absolute() or not config.parent.is_dir()
            or not token_file.is_absolute() or not token_file.is_file()
            or not 1024 <= args.port <= 65535):
        print("宿主配置创建失败：需要已有目录、绝对路径令牌文件及有效端口。", file=sys.stderr)
        return 2
    data = {
        "externalAccessConfig": {"httpApiEnabled": True, "webSocketEnabled": True,
                                 "agentControlEnabled": True, "mcpEnabled": False, "port": args.port},
        "autoPickConfig": {"enabled": False},
        "autoSkipConfig": {"enabled": False},
        "autoFishingConfig": {"enabled": False},
        "autoEatConfig": {"enabled": False},
        "quickTeleportConfig": {"enabled": False},
    }
    try:
        evidence = None
        if args.player_evidence is not None:
            fragment = _fields(_read_fragment(args.player_evidence), ("playerEvidence",), ("playerEvidence",))
            evidence = _evidence(fragment["playerEvidence"])
        calibration = _calibration(_read_fragment(args.dialogue_calibration)) if args.dialogue_calibration else None
        if calibration is not None:
            if evidence is not None and evidence != calibration["playerEvidence"]:
                raise ValueError("玩家身份与对话标定不一致")
            data["externalAccessConfig"].update(calibration)
        elif evidence is not None:
            data["externalAccessConfig"]["playerEvidence"] = evidence
    except (OSError, UnicodeError, ValueError, TypeError):
        print("宿主配置创建失败：配置片段无效或包含不允许的字段。", file=sys.stderr)
        return 2
    created = False
    try:
        with config.open("x", encoding="utf-8", newline="\n") as handle:
            created = True
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        print("宿主配置创建失败：目标文件已存在，不会覆盖。", file=sys.stderr)
        return 2
    except OSError:
        if created:
            try:
                config.unlink()
            except OSError:
                pass
        print("宿主配置创建失败：无法写入配置文件。", file=sys.stderr)
        return 1
    print("无界面宿主配置已创建；启动时请提供同一 --token-file。")
    return 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _loopback_origin(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.path not in {"", "/"} or parts.query or parts.fragment
                or not ipaddress.ip_address(parts.hostname).is_loopback):
            raise ValueError
        _ = parts.port
    except ValueError:
        raise ValueError("宿主地址必须是无凭据的本机回环地址") from None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _host_request(opener, origin: str, token: str, path: str, *, body=None, timeout: float):
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(origin + path, data=payload, headers=headers,
                                     method="GET" if payload is None else "POST")
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("宿主未确认请求")
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("宿主响应过大")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("宿主响应无效")
    return data


def _open_process(pid: int):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        raise OSError("无法打开宿主进程同步句柄")
    return kernel32, handle


def _stop_host(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        print("宿主停止失败：该操作需要 Windows 进程同步。", file=sys.stderr)
        return 2
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        print("宿主停止失败：等待时间必须在 0–120 秒内。", file=sys.stderr)
        return 2
    try:
        origin = _loopback_origin(args.base_url)
        token = (args.token_file.expanduser().read_text(encoding="utf-8").strip()
                 if args.token_file else os.environ.get("BETTERGI_AGENT_TOKEN", "").strip())
        if not token or "\n" in token or "\r" in token or len(token) > 4096:
            raise ValueError("缺少有效的访问令牌")
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"宿主停止失败：{exc if isinstance(exc, ValueError) else '无法读取令牌文件'}", file=sys.stderr)
        return 2

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    deadline = time.monotonic() + args.timeout
    handle = None
    drained = False
    try:
        status = _host_request(opener, origin, token, "/api/v1/agent/headless/status",
                               timeout=min(5.0, args.timeout))
        pid, instance = status.get("processId"), status.get("instanceId")
        if (status.get("headless") is not True or status.get("state") != "ready"
                or type(pid) is not int or not 0 < pid <= 0xFFFFFFFF
                or not isinstance(instance, str) or not instance or len(instance) > 256):
            raise ValueError("无界面宿主身份未确认")
        kernel32, handle = _open_process(pid)
        if kernel32.WaitForSingleObject(handle, 0) != 258:
            raise ValueError("宿主进程已退出或状态未知")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        receipt = _host_request(opener, origin, token, "/api/v1/agent/headless/shutdown",
                                body={"expectedProcessId": pid, "expectedInstanceId": instance},
                                timeout=remaining)
        if (receipt.get("status") != "drained" or type(receipt.get("processId")) is not int
                or receipt.get("processId") != pid or receipt.get("instanceId") != instance
                or receipt.get("liveStopped") is not True
                or receipt.get("runtimeStopped") is not True or receipt.get("shutdownScheduled") is not True):
            raise ValueError("宿主关闭回执不完整")
        drained = True
        remaining = deadline - time.monotonic()
        if remaining <= 0 or kernel32.WaitForSingleObject(handle, max(1, int(remaining * 1000))) != 0:
            raise TimeoutError
        print("无界面宿主已安全关闭并退出。")
        return 0
    except (OSError, ValueError, TimeoutError, urllib.error.URLError) as exc:
        message = "清理完成但退出未确认" if drained else "关闭未确认"
        if isinstance(exc, urllib.error.HTTPError):
            message += f"（HTTP {exc.code}）"
        print(f"宿主停止失败：{message}。", file=sys.stderr)
        return 1
    finally:
        if handle:
            kernel32.CloseHandle(handle)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    arguments = list(args.arguments)
    if arguments and arguments[0] == "--":
        arguments.pop(0)
    if args.command == "host":
        return _host(arguments)
    if args.command == "game":
        return _game(arguments)
    if args.command == "tunnel":
        return _tunnel(arguments)
    if args.command == "profile":
        return _profile(arguments)
    script = COMMANDS[args.command]
    if args.command == "journey":
        arguments.insert(0, "--journey")
    try:
        return subprocess.run([sys.executable, str(script), *arguments], check=False).returncode
    except OSError as exc:
        print(f"子命令启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
