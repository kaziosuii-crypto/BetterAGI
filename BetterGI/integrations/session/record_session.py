#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一场 BetterGI 实况录制为「视频 + 单一可读会话文件」。

录制器只做观察与保存：连接 BetterGI 实时 WebSocket，接收画面、指导、解说、
动作状态，然后

* 把画面按固定帧率写成 ``gameplay.mp4``（WebSocket 帧或窗口直录）；
* 把思考、解说、动作、VTuber 参数按同一时间轴写进 ``session.jsonl``；
* 生成眨眼、呼吸、轻微摆动等基础姿态，后期渲染不需要再猜。

它不修改游戏内存、不抓包、不执行游戏内脚本，只消费 BetterGI 已经公开的
实时数据。

用法::

    python record_session.py --token-file E:/BetterAGI/tools/bettergi_token.txt
    python record_session.py --duration 600 --start --stop-on-exit
    python record_session.py --capture window --window-title 原神

依赖：``websockets``（``pip install websockets``）与 ``ffmpeg``。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import random
import secrets
import shutil
import tempfile
import uuid
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import parse, request

from session_format import (
    SessionWriter,
    iso_to_epoch_ms,
    new_session_id,
    zlib_crc,
)

try:  # websockets 是唯一第三方依赖；缺失时给出可执行的提示。
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect as ws_connect
except ImportError:  # pragma: no cover - 仅用于友好报错
    print("缺少依赖 websockets，请先执行：pip install websockets", file=sys.stderr)
    raise

DEFAULT_WS_URL = "ws://127.0.0.1:30648/ws/v1/agent/live"
DEFAULT_HTTP_BASE = "http://127.0.0.1:30648"
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
RECV_TIMEOUT = 0.5
POSE_INTERVAL_MS = 1000
PLAYER_SPEECH_MAX_BYTES = 256 * 1024


def _ipc_json(value: Any) -> bytes:
    """用于签名和文件交换的确定性 UTF-8 JSON。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _ipc_signature(secret: str, value: Dict[str, Any]) -> str:
    """以会话能力密钥验证请求及回执，密钥不会写入文件。"""
    return hmac.new(secret.encode("utf-8"), _ipc_json(value), hashlib.sha256).hexdigest()


def _ipc_atomic_write(path: Path, data: bytes, *, temporary_directory: Optional[Path] = None) -> None:
    """先写临时文件再原子发布，避免消费者读到半条 JSON。"""
    temporary = (temporary_directory or path.parent) / (path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_player_speech_endpoint(endpoint: Path | str, secret: str) -> Dict[str, Any]:
    """验证并读取当前会话描述；生成异步结果前保存 session_id。"""
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError("speech IPC 密钥至少需要 32 个字符")
    descriptor_envelope = json.loads(Path(endpoint).read_text(encoding="utf-8"))
    descriptor = descriptor_envelope["body"]
    signature = descriptor_envelope["signature"]
    if (not isinstance(descriptor, dict) or descriptor.get("version") != 2 or
        not isinstance(signature, str) or not hmac.compare_digest(signature, _ipc_signature(secret, descriptor))):
        raise ValueError("speech IPC 描述文件身份校验失败")
    return descriptor


def read_player_speech_heartbeat(endpoint: Path | str, secret: str) -> Dict[str, Any]:
    """读取经认证的录制器心跳，并绑定当前描述。"""
    descriptor = read_player_speech_endpoint(endpoint, secret)
    path = Path(descriptor["directory"]) / "recorder.heartbeat"
    with path.open("rb") as handle:
        raw = handle.read(PLAYER_SPEECH_MAX_BYTES + 1)
    if len(raw) > PLAYER_SPEECH_MAX_BYTES:
        raise ValueError("speech 心跳过大")
    envelope = json.loads(raw)
    body, signature = envelope["body"], envelope["signature"]
    if (not isinstance(body, dict) or not isinstance(signature, str)
            or not hmac.compare_digest(signature, _ipc_signature(secret, body))
            or any(body.get(key) != descriptor.get(key)
                   for key in ("session_id", "instance", "challenge", "live_session_id",
                               "formal_chain_id", "speech_mode", "orchestrator_pid"))
            or descriptor != read_player_speech_endpoint(endpoint, secret)):
        raise ValueError("speech 心跳身份不匹配")
    return body


def announce_player_speech_producer(endpoint: Path | str, secret: str, *, timeout: float = 10.0) -> Dict[str, Any]:
    """向所属录制会话声明唯一 Player 语音生产者，等待录制线程确认。"""
    descriptor = read_player_speech_endpoint(endpoint, secret)
    session_id, instance, challenge = (descriptor.get(k) for k in ("session_id", "instance", "challenge"))
    if not all(isinstance(value, str) and value for value in (session_id, instance, challenge)):
        raise ValueError("speech 生产者交接缺少会话挑战")
    directory = Path(descriptor["directory"])
    deadline_ns = time.monotonic_ns() + int(timeout * 1_000_000_000)
    body = {"session_id": session_id, "instance": instance, "challenge": challenge,
            "pid": os.getpid(), "deadline_ns": deadline_ns}
    _ipc_atomic_write(directory / "producer.ready", _ipc_json({"body": body,
                      "signature": _ipc_signature(secret, body)}))
    response = directory / "producer.ready-ack"
    while time.monotonic_ns() < deadline_ns:
        try:
            envelope = json.loads(response.read_bytes())
        except FileNotFoundError:
            time.sleep(min(0.02, max(0.0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)))
            continue
        ack, signature = envelope.get("body"), envelope.get("signature")
        if (not isinstance(ack, dict) or not isinstance(signature, str)
                or not hmac.compare_digest(signature, _ipc_signature(secret, ack))
                or ack != {**body, "status": "ready"}):
            raise RuntimeError("speech 生产者就绪回执无效")
        response.unlink(missing_ok=True)
        return ack
    raise TimeoutError("speech 生产者就绪未确认")


def send_player_speech(endpoint: Path | str, secret: str, event: Dict[str, Any], *,
                       expected_session_id: str, expected_instance: str,
                       timeout: float = 5.0) -> Dict[str, Any]:
    """向录制线程提交 player speech，只有验证后的落盘回执才表示成功。

    endpoint 是录制器发布的会话描述文件，secret 须通过受信任的进程环境传递。
    调用方须在生成前读取并保存 session_id 和 instance，并在其他线程或进程调用。
    超时意味着结果未知，不表示未写入；调用方不得自动重试或确认送达。
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout 必须是正的有限数")
    if not all(isinstance(value, str) and value for value in
               (expected_session_id, expected_instance)):
        raise ValueError("必须先绑定录制会话与实例")
    # 跨进程使用同一台机器的单调时钟，期限也覆盖描述读取与请求发布。
    deadline_ns = time.monotonic_ns() + int(timeout * 1_000_000_000)
    descriptor = read_player_speech_endpoint(endpoint, secret)
    if (descriptor.get("session_id") != expected_session_id or
        descriptor.get("instance") != expected_instance):
        raise ValueError("speech 不属于当前录制会话")
    directory = Path(descriptor["directory"])
    request_id = uuid.uuid4().hex
    body = {"id": request_id, "instance": descriptor["instance"],
            "session_id": expected_session_id, "write_deadline_ns": deadline_ns,
            "event": event}
    body_bytes = _ipc_json(body)
    request_hash = hashlib.sha256(body_bytes).hexdigest()
    payload = _ipc_json({"body": body, "signature": _ipc_signature(secret, body)})
    if len(payload) > PLAYER_SPEECH_MAX_BYTES:
        raise ValueError("speech IPC 请求过大")
    request_path = directory / "requests" / (request_id + ".request")
    response_path = directory / (request_id + ".response")
    if time.monotonic_ns() >= deadline_ns:
        raise TimeoutError("speech IPC 请求已过期，写入结果未知；不要自动重试")
    # 临时文件位于稳定的父目录；requests 的原子改名决定是否接收。
    _ipc_atomic_write(request_path, payload, temporary_directory=directory)
    while time.monotonic_ns() < deadline_ns:
        try:
            raw = response_path.read_bytes()
        except FileNotFoundError:
            time.sleep(min(0.02, max(0.0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)))
            continue
        if len(raw) > PLAYER_SPEECH_MAX_BYTES:
            raise RuntimeError("speech IPC 回执过大，结果未知")
        try:
            envelope = json.loads(raw)
            receipt = envelope["body"]
            signature = envelope["signature"]
            valid = (isinstance(receipt, dict) and isinstance(signature, str)
                     and hmac.compare_digest(signature, _ipc_signature(secret, receipt))
                     and receipt.get("id") == request_id
                     and receipt.get("instance") == descriptor["instance"]
                     and receipt.get("session_id") == expected_session_id
                     and receipt.get("request_hash") == request_hash)
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise RuntimeError("speech IPC 回执验证失败，结果未知")
        response_path.unlink(missing_ok=True)
        if receipt.get("status") != "written":
            # Even a failed flush can have appended data; do not infer absence.
            raise RuntimeError("speech IPC 未确认写入，结果未知：" + str(receipt.get("error", "unknown")))
        stored = receipt.get("event")
        identity_fields = ("type", "source", "text", "generated_at_ms",
                           "thought_id", "intent_id", "frame_seq", "prosody",
                           "kind", "emotion", "intensity", "pace", "volume",
                           "pause_before_ms", "interruptible", "thought_source")
        if (not isinstance(event, dict) or not isinstance(stored, dict)
                or any(stored.get(key) != event[key] for key in identity_fields
                       if key in event and event[key] is not None)
                or stored.get("type") != "speech"
                or stored.get("source") != "persistent_player"):
            raise RuntimeError("speech IPC 回执内容不匹配，结果未知")
        return stored
    raise TimeoutError("speech IPC 回执超时，写入结果未知；不要自动重试")


def _bypass_proxy_for_loopback() -> None:
    """本机回环地址不走系统代理。

    录制器只连 127.0.0.1；系统代理（如 127.0.0.1:7897）在部分 Windows
    环境里也会拦截回环请求，导致「连接被拒绝」。显式写入 no_proxy，
    同时 HTTP 请求使用无代理 opener。
    """

    for name in ("no_proxy", "NO_PROXY"):
        existing = os.environ.get(name, "")
        parts = {item.strip() for item in existing.split(",") if item.strip()}
        parts.update({"127.0.0.1", "localhost", "::1"})
        os.environ[name] = ",".join(sorted(parts))


_bypass_proxy_for_loopback()
_DIRECT_OPENER = request.build_opener(request.ProxyHandler({}))


class FrameSink:
    """把 WebSocket JPEG 帧按固定帧率写成 MP4。

    写入线程按 ``fps`` 节拍取最新一帧；没有新帧时重复上一帧，因此视频
    时长与真实时间一致，事件时间轴不会漂移。
    """

    def __init__(self, path: Path, fps: float) -> None:
        self.path = Path(path)
        self.fps = float(fps)
        self.frames_written = 0
        self.error: Optional[str] = None
        self._latest: Optional[bytes] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._stderr = b""

    def start(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("找不到 ffmpeg，无法录制画面")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "image2pipe",
            "-framerate",
            f"{self.fps:g}",
            "-vcodec",
            "mjpeg",
            "-i",
            "-",
            "-an",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            f"{self.fps:g}",
            str(self.path),
        ]
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._thread = threading.Thread(
            target=self._run, name="frame-sink", daemon=True
        )
        self._thread.start()

    def push(self, jpeg: bytes) -> None:
        with self._lock:
            self._latest = jpeg

    def _run(self) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        period = 1.0 / self.fps
        next_tick = time.monotonic()
        while not self._stop.is_set():
            if self._proc.poll() is not None:
                break
            with self._lock:
                jpeg = self._latest
            if jpeg is not None:
                try:
                    self._proc.stdin.write(jpeg)
                    self.frames_written += 1
                except (BrokenPipeError, OSError) as exc:
                    self.error = f"写入 ffmpeg 失败：{exc}"
                    break
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_tick = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._proc is not None:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            if self._proc.stderr is not None:
                try:
                    self._stderr = self._proc.stderr.read() or b""
                except OSError:
                    pass
            if self._proc.returncode not in (0, None) and not self.error:
                tail = self._stderr.decode("utf-8", "replace").strip()[-400:]
                self.error = f"ffmpeg 退出码 {self._proc.returncode}：{tail}"

    @property
    def duration_ms(self) -> int:
        if self.fps <= 0:
            return 0
        return int(self.frames_written / self.fps * 1000)


class AudioSink:
    """用 ffmpeg dshow 录制系统/虚拟声卡的游戏原音。"""

    def __init__(self, path: Path, device: str) -> None:
        self.path = Path(path)
        self.device = device
        self.error: Optional[str] = None
        self._proc: Optional[subprocess.Popen[bytes]] = None
        # 仅界定 Popen 调用时间；dshow 首采样时间及设备延迟未知。
        self.start_unix_ms: Optional[int] = None
        self.start_window_ms: Optional[tuple[int, int]] = None

    def start(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("找不到 ffmpeg，无法录制游戏音频")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "dshow", "-i", "audio=" + self.device,
            "-ac", "2", "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
            str(self.path),
        ]
        # 以墙钟与 session.t0_unix_ms 对齐；边界仅描述进程启动调用，
        # 不能表示设备何时就绪、采样何时开始或声卡缓冲延迟。
        before_ms = int(time.time() * 1000)
        self._proc = subprocess.Popen(
            command, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        after_ms = int(time.time() * 1000)
        self.start_window_ms = (before_ms, after_ms)
        self.start_unix_ms = (before_ms + after_ms) // 2
        time.sleep(1.0)
        if self._proc.poll() is not None:
            tail = b""
            if self._proc.stderr is not None:
                tail = self._proc.stderr.read() or b""
            self.error = (
                "音频录制启动失败（设备 {0!r}）：{1}".format(
                    self.device, tail.decode("utf-8", "replace").strip()[-400:]
                )
            )
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            if self._proc.stderr is not None:
                self._proc.stderr.close()
            self._proc = None
            raise RuntimeError(self.error)

    def stop(self) -> int:
        """停止采集并探测文件实际时长；异常退出不能冒充正常录制。"""
        if self._proc is None:
            return 0
        proc = self._proc
        exited_early = proc.poll() is not None
        try:
            if not exited_early and proc.stdin is not None:
                proc.stdin.write(b"q")
                proc.stdin.flush()
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        if exited_early or proc.returncode != 0:
            tail = b""
            if proc.stderr is not None:
                try:
                    tail = proc.stderr.read() or b""
                except OSError:
                    pass
            self.error = (f"音频录制意外退出（退出码 {proc.returncode}）："
                          + tail.decode("utf-8", "replace").strip()[-400:])
        for stream in (proc.stdin, proc.stderr):
            if stream is not None:
                stream.close()
        self._proc = None

        # 墙钟运行时间包括启动和收尾，不能当作实际媒体时长。
        ffprobe = shutil.which("ffprobe")
        if ffprobe and self.path.is_file():
            try:
                result = subprocess.run(
                    [ffprobe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(self.path)],
                    capture_output=True, text=True, timeout=30, check=True,
                )
                seconds = float(result.stdout.strip())
                if math.isfinite(seconds) and seconds > 0:
                    return round(seconds * 1000)
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        self.error = (self.error + "；" if self.error else "") + "无法探测游戏原音实际时长"
        return 0


class WindowSink:
    """用 ffmpeg gdigrab 直接录制某个窗口，画面更流畅。"""

    def __init__(self, path: Path, fps: float, title: str) -> None:
        self.path = Path(path)
        self.fps = float(fps)
        self.title = title
        self.error: Optional[str] = None
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._start_monotonic = 0.0

    def start(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("找不到 ffmpeg，无法录制窗口")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "gdigrab",
            "-framerate",
            f"{self.fps:g}",
            "-i",
            f"title={self.title}",
            "-an",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            f"{self.fps:g}",
            str(self.path),
        ]
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._start_monotonic = time.monotonic()
        time.sleep(1.0)
        if self._proc.poll() is not None:
            tail = b""
            if self._proc.stderr is not None:
                tail = self._proc.stderr.read() or b""
            self.error = (
                f"窗口录制启动失败（标题 {self.title!r}）："
                + tail.decode("utf-8", "replace").strip()[-400:]
            )
            raise RuntimeError(self.error)

    @property
    def duration_ms(self) -> int:
        if not self._start_monotonic:
            return 0
        return int((time.monotonic() - self._start_monotonic) * 1000)

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.write(b"q")
                self._proc.stdin.flush()
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._proc.returncode not in (0, None) and not self.error:
            tail = b""
            if self._proc.stderr is not None:
                try:
                    tail = self._proc.stderr.read() or b""
                except OSError:
                    pass
            self.error = (
                f"ffmpeg 退出码 {self._proc.returncode}："
                + tail.decode("utf-8", "replace").strip()[-400:]
            )


class SessionRecorder:
    def __init__(self, args: argparse.Namespace) -> None:
        self._speech_mode = getattr(args, "speech_mode", "legacy")
        if (self._speech_mode == "player-exclusive"
                and not getattr(args, "exclusive_producer_required", False)):
            raise ValueError("player-exclusive 需要单玩家编排及就绪交接")
        self._formal_chain_id = getattr(args, "formal_chain_id", "")
        self._formal_chain_guard = getattr(args, "formal_chain_guard", None)
        if (self._speech_mode == "player-exclusive" and
                (not isinstance(self._formal_chain_id, str)
                 or len(self._formal_chain_id) != 32
                 or any(c not in "0123456789abcdef" for c in self._formal_chain_id)
                 or not callable(self._formal_chain_guard))):
            raise ValueError("player-exclusive 需要正式录制链能力")
        self.args = args
        self.token = args.token
        self.url = self._build_url(args.url, args.token)
        self._ws_headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        self.reconnect_ms = max(500, int(args.reconnect_ms))
        self.out_root = Path(args.out)
        self.out_root.mkdir(parents=True, exist_ok=True)

        self.writer: Optional[SessionWriter] = None
        self.t0_ms: Optional[int] = None
        self.session_dir: Optional[Path] = None
        self.sink: Optional[FrameSink | WindowSink] = None
        self.audio: Optional[AudioSink] = None

        self.hello: Dict[str, Any] = {}
        # 录制器不能把另一个 Live 会话接到既有 writer 上。
        self._live_session_id: Optional[str] = None
        self._live_session_changed = False
        # 有效 Live hello 到达前不得发布 speech 能力。
        self._speech_can_publish = False
        self.stopped = False
        self.stop_reason = ""
        self.last_frame_t = 0
        self.frame_count = 0
        self.last_keys: tuple[str, ...] = ()
        self.last_guidance = ""
        self.last_pose_t = -POSE_INTERVAL_MS
        self.next_blink_t = 2000
        self.last_event_t = 0
        self.reconnects = 0
        self.errors = 0
        self.speeches = 0
        self.thoughts = 0
        self.actions = 0
        self._rng = random.Random(20261027)
        self._pending: list[tuple[int, str, Dict[str, Any]]] = []
        self._connected_at = int(time.time() * 1000)
        self._speech_endpoint = Path(args.player_speech_endpoint).resolve() if getattr(args, "player_speech_endpoint", "") else None
        self._speech_secret = getattr(args, "player_speech_secret", "") or os.environ.get("BETTERGI_PLAYER_SPEECH_SECRET", "")
        if self._speech_endpoint and len(self._speech_secret) < 32:
            raise ValueError("启用 speech IPC 需要至少 32 字符的 BETTERGI_PLAYER_SPEECH_SECRET")
        if self._speech_mode == "player-exclusive" and self._speech_endpoint is None:
            raise ValueError("player-exclusive 需要 speech IPC 端点")
        self.speech_producer_ready = threading.Event()
        self.speech_producer_pid: Optional[int] = None
        self.exclusive_chain_failed = threading.Event()
        self._formal_chain_lock = threading.Lock()
        self._expected_speech_producer_pid: Optional[int] = None
        self._speech_heartbeat_last_ns = 0
        self._speech_heartbeat_sequence = 0
        self._speech_descriptor: Optional[Dict[str, Any]] = None
        self._speech_dir: Optional[Path] = None
        self._speech_seen: set[str] = set()
        self._speech_thoughts: set[str] = set()
        self._writer_thread_id: Optional[int] = None

    # -- 连接与生命周期 ---------------------------------------------------

    @staticmethod
    def _build_url(url: str, token: str) -> str:
        parts = parse.urlsplit(url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError("录制 WebSocket 地址无效或包含用户信息")
        query = [(key, value) for key, value in parse.parse_qsl(parts.query, keep_blank_values=True)
                 if key.lower() not in {"token", "access_token"}]
        return parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parse.urlencode(query), ""))

    def _http(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> None:
        base = self.args.http_base.rstrip("/")
        data = (
            json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        req = request.Request(
            base + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        with _DIRECT_OPENER.open(req, timeout=10) as response:
            response.read()

    def start_live(self) -> None:
        try:
            payload = {"fps": 15, "quality": 80, "maxWidth": 1280}
            if getattr(self.args, "strict_safety", False):
                payload["requireStrictSafety"] = True
                payload["requireControlLease"] = True
            self._http("POST", "/api/v1/agent/live/start", payload)
            print("已请求启动 BetterGI 实时会话", flush=True)
        except Exception as exc:  # noqa: BLE001 - strict admission cannot fall back to an unverified session
            if getattr(self.args, "strict_safety", False):
                raise RuntimeError("严格安全模式启动未确认，拒绝继续录制") from exc
            print(
                f"启动实时会话失败（可稍后手动启动）：{exc}",
                file=sys.stderr,
                flush=True,
            )

    def stop_live(self) -> None:
        try:
            self._http("POST", "/api/v1/agent/live/stop")
        except Exception as exc:  # noqa: BLE001
            print(f"停止实时会话失败：{exc}", file=sys.stderr, flush=True)

    def run(self) -> int:
        if self.args.start:
            self.start_live()

        try:
            if self.args.capture == "window":
                self._start_window_capture()
            self._connect_loop()
        except KeyboardInterrupt:
            self.stop_reason = "用户中断"
        finally:
            self._finish()

        if self.args.stop_on_exit and not self._live_session_changed:
            self.stop_live()
        return 0

    def _start_window_capture(self) -> None:
        self.t0_ms = int(time.time() * 1000)
        self.session_dir = self._make_session_dir()
        self._open_writer()
        sink = WindowSink(
            self.session_dir / "gameplay.mp4", self.args.fps, self.args.window_title
        )
        try:
            sink.start()
        except Exception as exc:  # noqa: BLE001
            print(f"窗口录制不可用，本次不录画面：{exc}", file=sys.stderr, flush=True)
            self.sink = None
            return
        self.sink = sink
        self._emit(
            0,
            "video",
            {
                "path": "gameplay.mp4",
                "width": 1920,
                "height": 1080,
                "fps": self.args.fps,
            },
        )
        print(f"已开始窗口录制：{self.args.window_title}", flush=True)

    def _connect_loop(self) -> None:
        try:
            with ws_connect(
                self.url,
                additional_headers=self._ws_headers,
                max_size=MAX_MESSAGE_BYTES,
                open_timeout=10,
                close_timeout=5,
                ping_interval=20,
            ) as socket:
                print("已连接 BetterGI 实时会话", flush=True)
                self._session_loop(socket)
        except (ConnectionClosed, OSError) as exc:
            if self.stopped:
                return
            # sessionId 可被复用，断线后无法证明仍是原场；撤销并停止。
            self._speech_can_publish = False
            self._live_session_changed = True
            self.stopped = True
            self.stop_reason = "实时连接中断，无法确认 Live 场次，停止录制"
            self._close_speech_endpoint(reject_reason=self.stop_reason)
            print(f"{self.stop_reason}：{exc}", file=sys.stderr, flush=True)

    def _session_loop(self, socket: Any) -> None:
        # 收到有效 hello 前不消费实时消息，也不发布 speech 能力。
        verified_hello = self._speech_can_publish
        while not self.stopped:
            if verified_hello:
                self._drain_player_speech()
            try:
                raw = socket.recv(timeout=RECV_TIMEOUT)
            except TimeoutError:
                if verified_hello:
                    self._tick()
                elif (self.args.duration and time.time() * 1000 - self._connected_at
                      >= self.args.duration * 1000):
                    self.stopped = True
                    self.stop_reason = "达到录制时长"
                continue
            except ConnectionClosed:
                raise
            if isinstance(raw, bytes):
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                if not verified_hello:
                    if message.get("type") != "hello":
                        continue
                    if not isinstance(message.get("sessionId"), str) or not message["sessionId"]:
                        self.stopped = True
                        self._live_session_changed = True
                        self.stop_reason = "hello 缺少 Live 会话身份，拒绝继续录制"
                        print(self.stop_reason, file=sys.stderr, flush=True)
                        return
                    verified_hello = True
                try:
                    self._handle(message)
                except Exception as exc:  # noqa: BLE001 - 单条坏消息不能中断整场录制
                    if self._speech_endpoint and self.writer and self._speech_descriptor is None:
                        raise RuntimeError("speech IPC 初始化失败") from exc
                    self.errors += 1
                    print(f"处理实时消息失败：{exc!r}", file=sys.stderr, flush=True)

    def _tick(self) -> None:
        now = int(time.time() * 1000)
        if self.writer is None and now - self._connected_at >= 5000:
            self._ensure_writer(self._connected_at)
        self._housekeeping()

    def _housekeeping(self) -> None:
        """按真实时间补齐姿态/眨眼，并检查录制时长。

        实时帧通常每 50–100ms 到达一次，主循环很少进入接收超时分支，
        因此这里必须由每条消息驱动，不能只在 ``_tick`` 里执行。
        """
        if self.writer is None or self.t0_ms is None:
            return
        now = int(time.time() * 1000)
        if self.args.duration and now - self.t0_ms >= self.args.duration * 1000:
            self.stopped = True
            self.stop_reason = "达到录制时长"
            return
        t = max(0, now - self.t0_ms)
        if t - self.last_pose_t >= POSE_INTERVAL_MS:
            self.last_pose_t = t
            self._emit_pose(t)
        if t >= self.next_blink_t:
            self._emit_blink(t)
            self.next_blink_t = t + self._rng.randint(3500, 5500)

    # -- 消息处理 ---------------------------------------------------------

    def _handle(self, message: Dict[str, Any]) -> None:
        kind = str(message.get("type") or "")
        if kind == "hello":
            live_id = message.get("sessionId")
            if isinstance(live_id, str) and live_id:
                if self._live_session_id is not None and live_id != self._live_session_id:
                    # 排队请求可能来自新场，无法证明归属，不能写进旧 writer。
                    self._live_session_changed = True
                    self.stopped = True
                    self.stop_reason = "Live 会话已变更，拒绝跨场录制"
                    self._close_speech_endpoint(reject_reason=self.stop_reason)
                    print(self.stop_reason, file=sys.stderr, flush=True)
                    return
                self._live_session_id = live_id
                self._speech_can_publish = True
            self.hello = message
            if (self.writer is not None and self._speech_descriptor is None
                    and self._live_session_id is not None):
                self._publish_speech_endpoint()
            return
        if kind == "live.stopped":
            # stop-on-exit 不能停止其后可能已启动的下一场。
            self._live_session_changed = True
            self.stopped = True
            self.stop_reason = "BetterGI 实时会话已停止"
            return
        if kind == "frame":
            self._on_frame(message)
        elif kind == "commentary":
            self._on_commentary(message)
        elif kind == "guidance":
            self._on_guidance(message)
        elif kind == "action_state":
            self._on_action_state(message)
        elif kind == "action":
            self._on_action(message)
        elif kind == "action_attempt":
            attempt = message.get("executionAttempt")
            if isinstance(attempt, dict):
                self._emit(self._message_time(message), "action", {
                    "source": "input_attempt", "executionAttempt": attempt,
                })
        elif kind == "input_evidence":
            evidence = message.get("inputEvidence")
            if isinstance(evidence, dict):
                fields = {"source": "input_evidence", "inputEvidence": evidence}
                for key in ("seq", "intentId", "motorGeneration", "actionGroupId", "attemptId"):
                    if message.get(key) is not None:
                        fields[key] = message[key]
                self._emit(self._message_time(message), "action", fields)
        elif kind == "error":
            self._on_error(message)
        self._housekeeping()

    def _message_time(self, message: Dict[str, Any]) -> int:
        ts = iso_to_epoch_ms(message.get("timestampUtc"))
        if ts is None:
            ts = int(time.time() * 1000)
        self._ensure_writer(ts)
        return max(0, ts - (self.t0_ms or ts))

    def _ensure_writer(self, ts_ms: int) -> None:
        if self.t0_ms is None:
            self.t0_ms = int(ts_ms)
            self.session_dir = self._make_session_dir()
            self._open_writer()
        if self.writer is not None:
            self._flush_pending()

    def _on_frame(self, message: Dict[str, Any]) -> None:
        ts = iso_to_epoch_ms(message.get("timestampUtc")) or int(time.time() * 1000)
        self._ensure_writer(ts)
        t = max(0, ts - (self.t0_ms or ts))
        self.last_frame_t = max(self.last_frame_t, t)
        self.frame_count += 1
        if self.args.capture != "frames":
            return
        jpeg_b64 = message.get("jpeg")
        if not isinstance(jpeg_b64, str) or not jpeg_b64:
            return
        if self.sink is None:
            sink = FrameSink(self.session_dir / "gameplay.mp4", self.args.fps)  # type: ignore[union-attr]
            try:
                sink.start()
            except Exception as exc:  # noqa: BLE001
                print(f"画面录制不可用：{exc}", file=sys.stderr, flush=True)
                return
            self.sink = sink
            self._emit(
                t,
                "video",
                {
                    "path": "gameplay.mp4",
                    "width": int(message.get("width") or 1280),
                    "height": int(message.get("height") or 720),
                    "fps": self.args.fps,
                },
            )
        try:
            jpeg = base64.b64decode(jpeg_b64, validate=True)
        except (ValueError, TypeError):
            return
        self.sink.push(jpeg)  # type: ignore[union-attr]

    def bind_expected_speech_producer(self, pid: int) -> None:
        """由正式编排器一次性绑定实际启动的唯一玩家进程。"""
        if self._speech_mode != "player-exclusive" or type(pid) is not int or pid <= 0:
            raise ValueError("正式语音生产者身份无效")
        with self._formal_chain_lock:
            if self._expected_speech_producer_pid is not None:
                raise RuntimeError("正式语音生产者已经绑定")
            self._expected_speech_producer_pid = pid

    def _formal_chain_valid(self, pid: int) -> bool:
        if self._speech_mode != "player-exclusive" or self.exclusive_chain_failed.is_set():
            return False
        try:
            return self._formal_chain_guard is not None and self._formal_chain_guard(pid) is True
        except Exception:
            return False

    def _invalidate_formal_chain(self) -> None:
        self.exclusive_chain_failed.set()
        self.stopped = True
        self.stop_reason = "正式 Player 语音链已失效"
        if self._speech_dir is not None:
            try:
                (self._speech_dir / "recorder.heartbeat").unlink(missing_ok=True)
            except OSError:
                pass

    def _publish_speech_endpoint(self) -> None:
        """发布当前会话的带签名描述；已有路径一律拒绝覆盖。"""
        if self._speech_endpoint is None:
            return
        assert self.session_dir is not None and self.writer is not None
        if not self._speech_can_publish or not self._live_session_id:
            raise RuntimeError("Live 会话身份未确认，不能发布 speech 能力")
        directory = Path(tempfile.mkdtemp(prefix=".speech-ipc-", dir=self.session_dir)).resolve()
        descriptor = {"version": 2, "session_id": self.writer.session_id,
                      "instance": secrets.token_hex(16), "directory": str(directory),
                      "challenge": secrets.token_hex(32), "live_session_id": self._live_session_id,
                      "speech_mode": self._speech_mode, "formal_chain_id": self._formal_chain_id,
                      "orchestrator_pid": os.getpid()}
        payload = _ipc_json({"body": descriptor,
                             "signature": _ipc_signature(self._speech_secret, descriptor)})
        created = False
        try:
            (directory / "requests").mkdir()
            with self._speech_endpoint.open("xb") as handle:
                created = True
                handle.write(payload)
                handle.flush()
        except Exception:
            if created:
                self._speech_endpoint.unlink(missing_ok=True)
            shutil.rmtree(directory, ignore_errors=True)
            raise
        self._speech_dir = directory
        self._speech_descriptor = descriptor

    def _accept_speech_producer(self) -> None:
        """由录制线程确认绑定本会话和挑战的单一生产者。"""
        if (self.speech_producer_ready.is_set() or self._speech_dir is None
                or self._speech_descriptor is None or self.writer is None):
            return
        with self._formal_chain_lock:
            expected_pid = self._expected_speech_producer_pid
        if expected_pid is None:
            return
        if not self._formal_chain_valid(expected_pid):
            self._invalidate_formal_chain()
            return
        path = self._speech_dir / "producer.ready"
        try:
            raw = path.read_bytes()
            if len(raw) > PLAYER_SPEECH_MAX_BYTES:
                raise ValueError("speech 生产者声明过大")
            envelope = json.loads(raw)
            body, signature = envelope["body"], envelope["signature"]
            if (not isinstance(body, dict) or not isinstance(signature, str)
                    or not hmac.compare_digest(signature, _ipc_signature(self._speech_secret, body))
                    or body.get("session_id") != self.writer.session_id
                    or body.get("instance") != self._speech_descriptor["instance"]
                    or body.get("challenge") != self._speech_descriptor["challenge"]
                    or type(body.get("pid")) is not int or body["pid"] != expected_pid
                    or type(body.get("deadline_ns")) is not int
                    or body["deadline_ns"] <= time.monotonic_ns()):
                raise ValueError("speech 生产者声明无效")
            if not self._formal_chain_valid(expected_pid):
                self._invalidate_formal_chain()
                return
            ack = {**body, "status": "ready"}
            _ipc_atomic_write(self._speech_dir / "producer.ready-ack",
                              _ipc_json({"body": ack, "signature": _ipc_signature(self._speech_secret, ack)}))
            self.speech_producer_pid = body["pid"]
            self.speech_producer_ready.set()
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError) as exc:
            print(f"speech 生产者就绪未确认：{exc}", file=sys.stderr, flush=True)

    def _publish_speech_heartbeat(self) -> None:
        """录制线程以单调时间证明当前 Live 会话仍在服务生产者。"""
        descriptor = self._speech_descriptor
        if (descriptor is None or self._speech_dir is None or not self.speech_producer_ready.is_set()
                or self.stopped):
            return
        if (not self._speech_can_publish or self._live_session_id != descriptor.get("live_session_id")
                or self.speech_producer_pid is None):
            self._invalidate_formal_chain()
            return
        with self._formal_chain_lock:
            expected_pid = self._expected_speech_producer_pid
        if (expected_pid != self.speech_producer_pid or self.writer is None
                or self.writer.session_id != descriptor.get("session_id")
                or descriptor.get("formal_chain_id") != self._formal_chain_id
                or descriptor.get("speech_mode") != self._speech_mode
                or not self._formal_chain_valid(self.speech_producer_pid)):
            self._invalidate_formal_chain()
            return
        now = time.monotonic_ns()
        if now - self._speech_heartbeat_last_ns < 250_000_000:
            return
        self._speech_heartbeat_sequence += 1
        body = {key: descriptor[key] for key in ("session_id", "instance", "challenge", "live_session_id",
                                                  "formal_chain_id", "speech_mode", "orchestrator_pid")}
        body.update(pid=self.speech_producer_pid, monotonic_ns=now, sequence=self._speech_heartbeat_sequence,
                    orchestrator="run_episode", player_mode="full_active", producer="persistent_player",
                    orchestrator_director_configured=False, legacy_formal_speech_excluded=True)
        _ipc_atomic_write(self._speech_dir / "recorder.heartbeat",
                          _ipc_json({"body": body, "signature": _ipc_signature(self._speech_secret, body)}))
        self._speech_heartbeat_last_ns = now

    def _drain_player_speech(self, *, request_dir: Optional[Path] = None,
                             finishing: bool = False, reject_reason: str = "") -> None:
        """只在录制线程消费请求，保持 session.jsonl 单写者。"""
        if self._speech_dir is None or self._speech_descriptor is None:
            return
        if not finishing and self._speech_mode == "player-exclusive":
            self._accept_speech_producer()
            self._publish_speech_heartbeat()
        paths = sorted((request_dir or self._speech_dir / "requests").glob("*.request"))
        for path in paths if finishing else paths[:64]:
            request_id = path.stem
            if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
                path.unlink(missing_ok=True)
                continue
            try:
                with path.open("rb") as handle:
                    raw = handle.read(PLAYER_SPEECH_MAX_BYTES + 1)
                if len(raw) > PLAYER_SPEECH_MAX_BYTES:
                    continue
                envelope = json.loads(raw)
                body, signature = envelope["body"], envelope["signature"]
                if not isinstance(body, dict) or not isinstance(signature, str):
                    continue
                if (body.get("id") != request_id or
                    body.get("instance") != self._speech_descriptor["instance"] or
                    not hmac.compare_digest(signature, _ipc_signature(self._speech_secret, body))):
                    continue
                request_hash = hashlib.sha256(_ipc_json(body)).hexdigest()
                receipt: Dict[str, Any] = {
                    "id": request_id, "instance": self._speech_descriptor["instance"],
                    "session_id": body.get("session_id"), "request_hash": request_hash,
                }
                try:
                    if request_id in self._speech_seen:
                        raise ValueError("重复的 speech IPC 请求")
                    self._speech_seen.add(request_id)
                    if reject_reason:
                        raise ValueError(reject_reason)
                    if self.writer is None or body.get("session_id") != self.writer.session_id:
                        raise ValueError("speech 不属于当前录制会话")
                    write_deadline_ns = body.get("write_deadline_ns")
                    if (isinstance(write_deadline_ns, bool) or not isinstance(write_deadline_ns, int)
                            or write_deadline_ns <= 0 or time.monotonic_ns() >= write_deadline_ns):
                        raise ValueError("speech IPC 写入期限已过或不合法")
                    event = body.get("event")
                    thought_id = event.get("thought_id") if isinstance(event, dict) else None
                    if not isinstance(thought_id, str) or not thought_id.strip():
                        raise ValueError("speech IPC 必须包含已保存的 thought_id")
                    if thought_id in self._speech_thoughts:
                        raise ValueError("该思考的 speech 已提交；禁止重复投递")
                    # Reserve before writing: a failed flush may already have
                    # appended the event, so even a new request id cannot retry it.
                    self._speech_thoughts.add(thought_id)
                    result = self.record_player_speech(
                        event, expected_session_id=body.get("session_id"),
                        _allow_stopped=finishing, _write_deadline_ns=write_deadline_ns,
                    )
                    receipt.update(status="written", event=result)
                except Exception as exc:  # noqa: BLE001 - 拒绝或写盘失败绝不能返回成功
                    receipt.update(status="error", error=str(exc))
                response = _ipc_json({"body": receipt,
                                      "signature": _ipc_signature(self._speech_secret, receipt)})
                _ipc_atomic_write(self._speech_dir / (request_id + ".response"), response)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                print(f"speech IPC 请求处理失败：{exc}", file=sys.stderr, flush=True)
            finally:
                path.unlink(missing_ok=True)

    def _close_speech_endpoint(self, *, reject_reason: str = "") -> None:
        """撤销入口并封存已接收请求，再由单写者处理；回执目录保留。"""
        if self._speech_endpoint is None or self._speech_descriptor is None:
            return
        assert self._speech_dir is not None
        try:
            (self._speech_dir / "recorder.heartbeat").unlink(missing_ok=True)
        except OSError as exc:
            print(f"撤销 speech 心跳失败：{exc}", file=sys.stderr, flush=True)
        try:
            envelope = json.loads(self._speech_endpoint.read_text(encoding="utf-8"))
            if envelope.get("body") == self._speech_descriptor:
                self._speech_endpoint.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError) as exc:
            print(f"撤销 speech IPC 描述失败：{exc}", file=sys.stderr, flush=True)
        # 客户端在父目录写临时文件，只有原子发布进 requests 才算接收。
        # 改名构成关闭边界：此前发布的全部处理，此后发布必然失败。
        requests = self._speech_dir / "requests"
        closed_requests = self._speech_dir / "closed-requests"
        try:
            requests.rename(closed_requests)
        except OSError as exc:
            # 无法建立边界时不能冒险处理正在变化的队列。
            print(f"封存 speech IPC 队列失败，未处理待收请求：{exc}", file=sys.stderr, flush=True)
        else:
            self._drain_player_speech(request_dir=closed_requests, finishing=True,
                                      reject_reason=reject_reason)
        # 已完成的回执允许客户端读取；目录留在该 session 中，避免关闭时丢回执。
        self._speech_descriptor = None

    def record_player_speech(
        self, event: Dict[str, Any], *, expected_session_id: str,
        _allow_stopped: bool = False, _write_deadline_ns: Optional[int] = None,
    ) -> Dict[str, Any]:
        """显式落盘 player 的 speech；须在录制器所属线程调用，不发送网络消息。

        event 使用 type/source/text/generated_at_ms 和可选关联、prosody 字段。
        必须已有录制会话；不缓存、不创建会话。调用方须传入生成请求所属的
        录制 session_id，避免异步旧结果投递到新场次。返回的只是落盘事件，
        不代表播放。
        """
        if self._writer_thread_id is not None and threading.get_ident() != self._writer_thread_id:
            raise RuntimeError("speech 必须由录制器写入线程处理")
        if (self._speech_mode == "player-exclusive"
                and not self.speech_producer_ready.is_set()):
            raise RuntimeError("speech 生产者尚未就绪")
        if self.writer is None or self.t0_ms is None or (self.stopped and not _allow_stopped):
            raise RuntimeError("录制会话未开始或已停止")
        if self.writer.t0_unix_ms != self.t0_ms:
            raise RuntimeError("录制器与会话文件的起点不一致")
        if expected_session_id != self.writer.session_id:
            raise ValueError("speech 不属于当前录制会话")
        if not isinstance(event, dict) or event.get("type") != "speech":
            raise ValueError("必须提供 speech 事件")
        if event.get("source") != "persistent_player":
            raise ValueError("speech source 必须是 persistent_player")
        timestamp = event.get("generated_at_ms")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
            raise ValueError("generated_at_ms 必须是正整数毫秒")
        if timestamp < self.writer.t0_unix_ms:
            raise ValueError("speech 早于当前录制会话")
        if timestamp > int(time.time() * 1000):
            raise ValueError("speech 时间不能晚于当前时间")
        text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("解说文本不能为空")

        prosody = event.get("prosody")
        if prosody is not None and not isinstance(prosody, dict):
            raise ValueError("prosody 必须是对象")
        # 同时接受扁平字段和嵌套提示；冲突时拒绝，保留原始 prosody 对象。
        hints = dict(event)
        if prosody is not None:
            for name in ("intensity", "pace", "volume", "pause_before_ms", "interruptible"):
                if name in prosody:
                    if name in event and event[name] != prosody[name]:
                        raise ValueError(f"prosody.{name} 与顶层字段冲突")
                    hints[name] = prosody[name]
        # 对可选提示严格校验，避免 writer 的旧式容错悄悄改变传入事件。
        for name in ("kind", "emotion", "thought_source"):
            value = event.get(name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} 必须是非空字符串")
        intensity = hints.get("intensity")
        if intensity is not None and (
            isinstance(intensity, bool) or not isinstance(intensity, (int, float))
            or not math.isfinite(intensity) or not 0 <= intensity <= 1
        ):
            raise ValueError("intensity 必须在 0..1 之间")
        for name, allowed in (("pace", ("slow", "normal", "fast")),
                              ("volume", ("soft", "normal", "loud"))):
            if hints.get(name) is not None and hints[name] not in allowed:
                raise ValueError(f"{name} 不合法")
        pause = hints.get("pause_before_ms")
        if pause is not None and (
            isinstance(pause, bool) or not isinstance(pause, int) or not 0 <= pause <= 2000
        ):
            raise ValueError("pause_before_ms 必须是 0..2000 的整数")
        if hints.get("interruptible") is not None and not isinstance(hints["interruptible"], bool):
            raise ValueError("interruptible 必须是布尔值")

        t = timestamp - self.writer.t0_unix_ms
        fields = {name: hints[name] for name in (
            "kind", "emotion", "frame_seq", "thought_id", "intent_id", "intensity",
            "pace", "volume", "pause_before_ms", "interruptible", "thought_source", "prosody",
        ) if hints.get(name) is not None}
        if _write_deadline_ns is None:
            stored = self.writer.emit_speech(
                t, text, source="persistent_player", generated_at_ms=timestamp, **fields
            )
        else:
            # emit_speech 还会计算口型等内容；在其真正进入 _write 时检查期限。
            # 只有录制线程使用 writer，临时包装不会改变其他线程的写入顺序。
            original_write = self.writer._write

            def write_before_deadline(payload: Dict[str, Any]) -> None:
                if time.monotonic_ns() >= _write_deadline_ns:
                    raise ValueError("speech IPC 写入期限已过")
                original_write(payload)

            self.writer._write = write_before_deadline
            try:
                stored = self.writer.emit_speech(
                    t, text, source="persistent_player", generated_at_ms=timestamp, **fields
                )
            finally:
                self.writer._write = original_write
        self.speeches += 1
        self.last_event_t = max(self.last_event_t, t + stored["dur"])
        return stored

    def _on_commentary(self, message: Dict[str, Any]) -> None:
        text = str(message.get("text") or "").strip()
        if not text:
            return
        kind = str(message.get("kind") or "reaction")
        meta = message.get("meta") if isinstance(message.get("meta"), dict) else None
        if isinstance(meta, dict) and meta.get("internal"):
            kind = "thought"
        if kind != "thought" and self._speech_mode == "player-exclusive":
            return
        t = self._message_time(message)
        emotion = str(message.get("emotion") or "calm")
        frame_seq = message.get("frameSeq")
        if kind == "thought":
            self._emit(
                t,
                "thought",
                {
                    "text": text[:4000],
                    **({"frame": int(frame_seq)} if isinstance(frame_seq, int) else {}),
                },
            )
            self.thoughts += 1
        else:
            if self.writer is None:
                return
            event = self.writer.emit_speech(
                t,
                text,
                kind=kind,
                emotion=emotion,
                frame_seq=frame_seq if isinstance(frame_seq, int) else None,
                seed=zlib_crc(text, t),
                meta=meta,
            )
            self.speeches += 1
            self.last_event_t = max(self.last_event_t, t + int(event.get("dur", 0)))

    def _on_guidance(self, message: Dict[str, Any]) -> None:
        text = str(message.get("text") or "").strip()
        if not text or text == self.last_guidance:
            return
        self.last_guidance = text
        t = self._message_time(message)
        self._emit(
            t,
            "guidance",
            {"text": text[:2000], "source": message.get("source") or "agent"},
        )

    def _on_action_state(self, message: Dict[str, Any]) -> None:
        keys = message.get("keys")
        if not isinstance(keys, list):
            return
        normalized = tuple(sorted(str(key) for key in keys))
        if normalized == self.last_keys:
            return
        self.last_keys = normalized
        t = self._message_time(message)
        self._emit(t, "action", {"keys": list(normalized), "source": "state"})
        self.actions += 1

    def _on_action(self, message: Dict[str, Any]) -> None:
        """服务端回显的动作块（含鼠标位移与模型思考）。"""
        block = message.get("block") if isinstance(message.get("block"), dict) else None
        if block is None:
            return
        t = self._message_time(message)
        chunks = block.get("chunks") if isinstance(block.get("chunks"), list) else []
        keys: list[str] = []
        dx = dy = scroll = 0
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                continue
            for key in chunk.get("keys") or []:
                if str(key) not in keys:
                    keys.append(str(key))
            if index == 0:
                dx = int(chunk.get("dx") or 0)
                dy = int(chunk.get("dy") or 0)
                scroll = int(chunk.get("scroll") or 0)
        absolute = (
            block.get("absolute") if isinstance(block.get("absolute"), dict) else None
        )
        fields: Dict[str, Any] = {"keys": keys, "source": "block", "status": "accepted"}
        for name in ("intentId", "motorGeneration", "actionGroupId", "attemptId", "seq"):
            value = message.get(name) if message.get(name) is not None else block.get(name)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                fields[name] = value
        if dx or dy:
            fields["mouse"] = {"dx": dx, "dy": dy}
        if scroll:
            fields["scroll"] = scroll
        if (
            absolute
            and isinstance(absolute.get("x"), int)
            and isinstance(absolute.get("y"), int)
        ):
            fields["cursor"] = {"x": absolute["x"], "y": absolute["y"]}
        thought = block.get("thought") or message.get("thought")
        if isinstance(thought, str) and thought.strip():
            fields["thought"] = thought.strip()[:2000]
        self._emit(t, "action", fields)
        self.actions += 1

    def _on_error(self, message: Dict[str, Any]) -> None:
        t = self._message_time(message)
        self.errors += 1
        self._emit(
            t,
            "error",
            {
                "code": str(message.get("code") or "ERROR"),
                "message": str(message.get("message") or "")[:2000],
            },
        )

    # -- 写入辅助 ---------------------------------------------------------

    def _make_session_dir(self) -> Path:
        name = (
            self.args.name or str(self.hello.get("sessionId") or "") or new_session_id()
        )
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)[:80]
        directory = self.out_root / safe
        if directory.exists():
            directory = self.out_root / f"{safe}-{int(time.time())}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _open_writer(self) -> None:
        assert self.session_dir is not None and self.t0_ms is not None
        source: Dict[str, Any] = {
            "kind": "bettergi-live",
            "ws": self._redact(self.args.url),
            "live_session": self.hello.get("sessionId"),
            "capture": self.args.capture,
        }
        self._writer_thread_id = threading.get_ident()
        self.writer = SessionWriter(
            self.session_dir,
            session_id=self.session_dir.name,
            t0_unix_ms=self.t0_ms,
            source=source,
            video={
                "path": "gameplay.mp4",
                "fps": self.args.fps,
                "width": int(self.hello.get("width") or 1280),
                "height": int(self.hello.get("height") or 720),
            },
            agent={"kind": "director+lumine", "name": self.args.agent_name},
        )
        print(f"会话目录：{self.session_dir}", flush=True)
        if not self.stopped and self._speech_can_publish:
            self._publish_speech_endpoint()
        self._start_audio()

    def _start_audio(self) -> None:
        """按需启动系统声卡录音；失败只警告，不影响事件与画面录制。"""
        device = str(getattr(self.args, "audio_device", "") or "").strip()
        if not device or self.audio is not None or self.session_dir is None:
            return
        sink = AudioSink(self.session_dir / "audio" / "game_audio.m4a", device)
        try:
            sink.start()
        except Exception as exc:  # noqa: BLE001
            print(f"游戏原音录制不可用：{exc}", file=sys.stderr, flush=True)
            return
        self.audio = sink
        assert self.t0_ms is not None and sink.start_unix_ms is not None
        assert sink.start_window_ms is not None
        start_offset = sink.start_unix_ms - self.t0_ms
        self._emit(
            max(0, start_offset),
            "audio",
            {
                "path": "audio/game_audio.m4a",
                "device": device,
                "source": "dshow",
                # 即使会话起点晚于启动估计，也保留有符号偏移供渲染裁头。
                "start_offset_ms": start_offset,
                "process_start_unix_ms": sink.start_unix_ms,
                "process_start_window_unix_ms": list(sink.start_window_ms),
                "process_start_uncertainty_ms": max(
                    abs(bound - sink.start_unix_ms) for bound in sink.start_window_ms
                ) + 1,  # 墙钟毫秒取整；不含设备及首采样延迟
                "timing_basis": "popen_call_midpoint",
                "first_sample_offset_ms": None,
            },
        )
        print(f"已开始录制游戏原音：{device}", flush=True)

    @staticmethod
    def _redact(url: str) -> str:
        parts = parse.urlsplit(url)
        return parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    def _emit(self, t: int, event_type: str, fields: Dict[str, Any]) -> None:
        if self.writer is None:
            self._pending.append((t, event_type, fields))
            return
        self.writer.emit(t, event_type, **fields)
        self.last_event_t = max(self.last_event_t, t)

    def _flush_pending(self) -> None:
        pending, self._pending = self._pending, []
        for t, kind, payload in pending:
            if kind == "commentary":
                self._on_commentary(payload)
            elif kind == "guidance":
                self._on_guidance(payload)
            elif kind == "action_state":
                self._on_action_state(payload)
            elif kind == "action_block":
                self._emit(max(0, t), "action", payload)
            elif kind in ("action", "error", "video"):
                self._emit(max(0, t), kind, payload)

    def _emit_pose(self, t: int) -> None:
        seconds = t / 1000.0
        self._emit(
            t,
            "avatar",
            {
                "op": "pose",
                "params": {
                    "ParamAngleX": round(
                        1.8 * math.sin(2 * math.pi * 0.07 * seconds), 2
                    ),
                    "ParamAngleY": round(
                        1.2 * math.sin(2 * math.pi * 0.05 * seconds + 1.1), 2
                    ),
                    "ParamAngleZ": round(
                        0.8 * math.sin(2 * math.pi * 0.09 * seconds + 0.4), 2
                    ),
                    "ParamBodyAngleX": round(
                        1.2 * math.sin(2 * math.pi * 0.06 * seconds), 2
                    ),
                    "ParamBodyAngleZ": round(
                        0.6 * math.sin(2 * math.pi * 0.08 * seconds + 2.0), 2
                    ),
                    "ParamBreath": round(
                        0.5 + 0.5 * math.sin(2 * math.pi * 0.22 * seconds), 3
                    ),
                    "ParamEyeBallX": round(
                        0.25 * math.sin(2 * math.pi * 0.11 * seconds + 0.7), 3
                    ),
                    "ParamEyeBallY": round(
                        0.12 * math.sin(2 * math.pi * 0.13 * seconds + 1.9), 3
                    ),
                },
            },
        )

    def _emit_blink(self, t: int) -> None:
        self._emit(t, "avatar", {"op": "blink", "dur": 120})

    # -- 收尾 -------------------------------------------------------------

    def _finish(self) -> None:
        self.stopped = True
        if self.writer is None and self._pending:
            self._ensure_writer(self._connected_at)
        self._close_speech_endpoint()
        if self.sink is not None:
            self.sink.stop()
            if self.sink.error:
                print(f"画面录制警告：{self.sink.error}", file=sys.stderr, flush=True)
        duration = max(self.last_event_t, self.last_frame_t)
        if self.sink is not None:
            duration = max(duration, self.sink.duration_ms)
        audio_ms = 0
        if self.audio is not None:
            audio_ms = self.audio.stop()
            if audio_ms and self.audio.start_unix_ms is not None and self.t0_ms is not None:
                duration = max(duration, self.audio.start_unix_ms - self.t0_ms + audio_ms)
            if self.audio.error:
                print(f"游戏原音录制警告：{self.audio.error}", file=sys.stderr, flush=True)
        if self.writer is not None:
            counts = {
                "frames": self.frame_count,
                "speeches": self.speeches,
                "thoughts": self.thoughts,
                "actions": self.actions,
                "errors": self.errors,
                "reconnects": self.reconnects,
                "game_audio": self.audio is not None,
                "game_audio_ms": audio_ms,
                "stop_reason": self.stop_reason or "用户停止",
            }
            self.writer.close(duration_ms=duration, counts=counts)
            print(
                f"会话已保存：{self.session_dir}（{duration / 1000:.1f}s，"
                f"{self.speeches} 条解说，{self.thoughts} 条思考，{self.actions} 个动作）",
                flush=True,
            )
        else:
            print("没有收到任何画面或事件，未生成会话文件", file=sys.stderr, flush=True)


def _read_token(args: argparse.Namespace) -> str:
    if args.token:
        return str(args.token).strip()
    if args.token_file:
        return Path(args.token_file).read_text(encoding="utf-8").strip()
    env = os.environ.get("BETTERGI_LIVE_TOKEN") or os.environ.get(
        "BETTERGI_AGENT_TOKEN"
    )
    if env:
        return env.strip()
    raise SystemExit("缺少令牌：用 --token、--token-file 或 BETTERGI_LIVE_TOKEN 提供")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="录制 BetterGI 实况为视频 + 单一可读会话文件",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url", default=DEFAULT_WS_URL, help="BetterGI 实时 WebSocket 地址"
    )
    parser.add_argument(
        "--http-base", default=DEFAULT_HTTP_BASE, help="BetterGI HTTP 服务地址"
    )
    parser.add_argument("--token", default="", help="访问令牌（不建议写在命令历史里）")
    parser.add_argument("--token-file", default="", help="从文件读取访问令牌")
    parser.add_argument("--out", default="sessions", help="会话根目录")
    parser.add_argument(
        "--name", default="", help="会话子目录名；默认使用 BetterGI 会话 id"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="录制秒数；0 = 直到直播停止或 Ctrl+C",
    )
    parser.add_argument("--fps", type=float, default=15.0, help="视频帧率")
    parser.add_argument(
        "--capture",
        choices=["frames", "window", "none"],
        default="frames",
        help="frames = 用实时帧合成视频；window = ffmpeg 直录窗口；none = 只记事件",
    )
    parser.add_argument(
        "--window-title", default="原神", help="window 模式下的窗口标题"
    )
    parser.add_argument(
        "--audio-device",
        default="",
        help=(
            "ffmpeg dshow 音频设备名（如 “立体声混音 (Realtek(R) Audio)”）；"
            "留空则不录游戏原音，渲染时可再用 --game-audio 指定"
        ),
    )
    parser.add_argument("--start", action="store_true", help="先调用 HTTP 启动实时会话")
    parser.add_argument("--strict-safety", action="store_true", help="启动时要求 BetterGI 严格安全门")
    parser.add_argument(
        "--stop-on-exit", action="store_true", help="录制结束时停止实时会话"
    )
    parser.add_argument(
        "--reconnect-ms", type=int, default=2000, help="断线重连初始间隔"
    )
    parser.add_argument(
        "--agent-name", default="director", help="写入元信息的智能体名称"
    )
    parser.add_argument("--player-speech-endpoint", default="", help="显式启用本机 speech IPC，指定会话描述文件路径")
    parser.add_argument("--speech-mode", choices=["legacy", "player-exclusive"],
                        default="legacy", help="player-exclusive 排除旧 commentary 解说，保留 thought")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.speech_mode == "player-exclusive":
        print("player-exclusive 暂不可用：没有已验证的正式 player。", file=sys.stderr)
        return 2
    args.token = _read_token(args)
    recorder = SessionRecorder(args)
    return recorder.run()


if __name__ == "__main__":
    raise SystemExit(main())
