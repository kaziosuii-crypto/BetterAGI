"""WebRTC 快系统：aiortc 客户端，把游戏画面当视频流持续喂给 Qwen-Omni。

为什么不用浏览器：官方 WebRTC 信令端点不返回 CORS 头，浏览器内 fetch(offer) 直接
TypeError: Failed to fetch。官方客户端就是 aiortc，所以这里用 Python 原生客户端。

协议要点（来自官方 demo，见 _diag/qw_webrtc_demo.txt）：
- pc.addTrack(audio) 是硬要求：offer 里必须有 m=audio，否则 400 remote_sdp_failed。
- pc.createDataChannel("oai-events") 只是触发 SDP 协商；服务端用固定名 "txt" 的
  DataChannel 推送事件，收到 txt 首条消息后才发 session.update。
- WebRTC 只支持 server_vad / semantic_vad，不支持 manual。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Dict, List, Optional

DEFAULT_ENDPOINT = "https://cn-beijing.maas.qianwenaiapi.com"
DEFAULT_MODEL = "qwen3.8-omni-flash-realtime"

SYSTEM_INSTRUCTIONS = (
    "你是一个游戏操作助手。你正在通过实时视频看着一个游戏窗口（Windows）。"
    "回答要极简：只描述当前画面上确定可见的内容（界面类型、可见文字、可交互项），"
    "不要推测看不到的东西，不要输出与画面无关的解释。"
)


def _silent_audio_track():
    from aiortc.mediastreams import AudioStreamTrack
    import av
    import fractions
    import numpy as np

    class SilentAudio(AudioStreamTrack):
        kind = "audio"

        def __init__(self, sample_rate: int = 48000, samples: int = 960):
            super().__init__()
            self._rate = sample_rate
            self._samples = samples
            self._pts = 0

        async def recv(self):
            await asyncio.sleep(self._samples / self._rate)
            arr = np.zeros((self._samples, 1), dtype=np.int16)
            frame = av.AudioFrame.from_ndarray(arr, format="s16", layout="mono")
            frame.sample_rate = self._rate
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, self._rate)
            self._pts += self._samples
            return frame

    return SilentAudio()


class LatestFrameVideoTrack:
    """永远发送"最新一帧"的视频轨：没有新帧就重发上一帧。"""

    @staticmethod
    def build(fps: int = 5):
        from aiortc import VideoStreamTrack
        import av
        import fractions

        class _Track(VideoStreamTrack):
            kind = "video"

            def __init__(self):
                super().__init__()
                self._fps = max(1, int(fps))
                self._frame: Optional["av.VideoFrame"] = None
                self._lock = threading.Lock()
                self._start = time.time()
                self._count = 0
                self.pushed = 0

            def push_rgb(self, arr) -> None:
                import numpy as np

                if arr is None:
                    return
                a = np.asarray(arr)
                if a.ndim != 3 or a.shape[2] != 3:
                    return
                a = np.ascontiguousarray(a.astype("uint8"))
                frame = av.VideoFrame.from_ndarray(a, format="rgb24")
                with self._lock:
                    self._frame = frame
                    self.pushed += 1

            def has_frame(self) -> bool:
                with self._lock:
                    return self._frame is not None

            async def recv(self):
                import av

                while True:
                    with self._lock:
                        src = self._frame
                    if src is not None:
                        break
                    await asyncio.sleep(0.05)
                out = av.VideoFrame.from_ndarray(
                    src.to_ndarray(format="rgb24"), format="rgb24"
                )
                pts = int((time.time() - self._start) * 90000)
                out.pts = pts
                out.time_base = fractions.Fraction(1, 90000)
                self._count += 1
                wait = 1.0 / self._fps
                await asyncio.sleep(wait)
                return out

        return _Track()


class WebRTCFastSystem:
    def __init__(
        self,
        api_key: str,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        fps: int = 5,
        instructions: str = SYSTEM_INSTRUCTIONS,
        voice: str = "Tina",
        on_event=None,
    ):
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.fps = fps
        self.instructions = instructions
        self.voice = voice
        self.on_event = on_event
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._pc = None
        self._video_track = None
        self._audio_track = None
        self._txt = None
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._events: List[Dict[str, Any]] = []
        self._resp_lock = threading.Lock()
        self._resp_text: List[str] = []
        self._resp_done = threading.Event()
        self._sdp_answer: Optional[str] = None
        self._connected_at: Optional[float] = None
        self._session_updated = False
        self._closed = False

    # ---------------- 线程/事件循环 ----------------
    def start(self, timeout: float = 25.0) -> Dict[str, Any]:
        if self._thread and self._thread.is_alive():
            return {"ok": True, "already": True}
        self._thread = threading.Thread(target=self._run_loop, name="webrtc-fast", daemon=True)
        self._thread.start()
        ok = self._ready.wait(timeout)
        if not ok:
            return {"ok": False, "error": self._error or "connect timeout"}
        if self._error:
            return {"ok": False, "error": self._error}
        return {
            "ok": True,
            "model": self.model,
            "sdp_answer_len": len(self._sdp_answer or ""),
            "session_updated": self._session_updated,
            "connect_ms": int((self._connected_at or time.time()) * 1000),
        }

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
            self._loop.run_forever()
        except Exception as exc:  # pragma: no cover
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            try:
                self._loop.run_until_complete(self._shutdown())
            except Exception:
                pass
            self._loop.close()

    async def _connect(self) -> None:
        import aiohttp
        from aiortc import RTCPeerConnection, RTCConfiguration, RTCSessionDescription

        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))

        self._video_track = LatestFrameVideoTrack.build(fps=self.fps)
        self._audio_track = _silent_audio_track()
        self._pc.addTrack(self._audio_track)
        self._pc.addTrack(self._video_track)
        self._pc.createDataChannel("oai-events")

        @self._pc.on("datachannel")
        def _on_dc(channel):
            if channel.label == "txt":
                self._txt = channel

                @channel.on("message")
                def _on_msg(msg):
                    self._handle_event(msg)

        @self._pc.on("track")
        def _on_track(track):
            if track.kind == "audio":
                asyncio.ensure_future(self._drain(track))
            elif track.kind == "video":
                asyncio.ensure_future(self._drain(track))

        @self._pc.on("connectionstatechange")
        async def _on_state():
            if self._pc.connectionState in ("failed", "closed"):
                self._error = self._error or f"connection {self._pc.connectionState}"

        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        url = f"{self.endpoint}/api/v1/webrtc/realtime?model={self.model}"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                data=offer.sdp.encode("utf-8"),
                headers={
                    "Content-Type": "application/sdp",
                    "Authorization": f"Bearer {self.api_key}",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    self._error = f"SDP {resp.status}: {body[:300]}"
                    self._ready.set()
                    return
                self._sdp_answer = body
        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=self._sdp_answer, type="answer")
        )
        # 等 txt 通道 + 首个 session.created
        deadline = time.time() + 15
        while time.time() < deadline and self._txt is None:
            await asyncio.sleep(0.05)
        self._connected_at = time.time()
        self._ready.set()

    async def _drain(self, track) -> None:
        try:
            while True:
                await track.recv()
        except Exception:
            return

    def _handle_event(self, msg) -> None:
        try:
            evt = json.loads(msg)
        except Exception:
            return
        if not isinstance(evt, dict):
            return
        etype = evt.get("type", "")
        self._events.append({"t": time.time(), "type": etype})
        if len(self._events) > 400:
            del self._events[:200]
        if etype == "session.created" and not self._session_updated:
            self._session_updated = True
            self._send_session_update()
        elif etype in ("response.text.delta", "response.audio_transcript.delta"):
            d = evt.get("delta") or ""
            if d:
                with self._resp_lock:
                    self._resp_text.append(d)
        elif etype in ("response.done", "response.text.done", "response.audio_transcript.done"):
            self._resp_done.set()
        if self.on_event:
            try:
                self.on_event(evt)
            except Exception:
                pass

    def _post(self, fn) -> None:
        """在事件循环线程里执行（aiortc 的 DataChannel.send 需要当前 loop）。"""
        if self._loop is None:
            raise RuntimeError("event loop not started")
        self._loop.call_soon_threadsafe(fn)

    def _send_text(self, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        channel = self._txt

        def _do() -> None:
            if channel is not None and channel.readyState == "open":
                channel.send(data)

        self._post(_do)

    def _send_session_update(self) -> None:
        if self._txt is None or self._txt.readyState != "open":
            return
        payload = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": self.voice,
                "audio": {
                    "input": {"format": {"type": "pcm", "sample_rate": 16000}},
                    "output": {"format": {"type": "pcm", "sample_rate": 24000}},
                },
                "instructions": self.instructions,
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "silence_duration_ms": 800,
                },
                "max_tokens": 1024,
                "temperature": 0.6,
            },
        }
        self._send_text(payload)

    # ---------------- 同步接口 ----------------
    def push_frame(self, rgb) -> None:
        if self._video_track is not None:
            self._video_track.push_rgb(rgb)

    def has_frame(self) -> bool:
        return bool(self._video_track and self._video_track.has_frame())

    def events(self) -> List[Dict[str, Any]]:
        return list(self._events)

    def ask(
        self,
        text: str,
        timeout: float = 25.0,
        audio: bool = False,
        image_b64: Optional[str] = None,
        image_mime: str = "image/jpeg",
    ) -> Dict[str, Any]:
        if self._txt is None:
            return {"ok": False, "error": "not connected"}
        if not self.has_frame() and not image_b64:
            return {"ok": False, "error": "no video frame pushed yet"}
        with self._resp_lock:
            self._resp_text = []
        self._resp_done.clear()
        t0 = time.time()
        content = [{"type": "input_text", "text": text}]
        if image_b64:
            content.insert(
                0,
                {
                    "type": "input_image",
                    "image_url": f"data:{image_mime};base64,{image_b64}",
                },
            )
        item = {
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": content},
        }
        try:
            self._send_text(item)
            self._send_text(
                {"type": "response.create", "response": {"modalities": ["text"]}}
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not self._resp_done.wait(timeout):
            return {
                "ok": False,
                "error": "response timeout",
                "elapsed_ms": int((time.time() - t0) * 1000),
                "events": [e["type"] for e in self._events[-12:]],
            }
        with self._resp_lock:
            answer = "".join(self._resp_text).strip()
        return {
            "ok": bool(answer),
            "text": answer,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    async def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._pc is not None:
                await self._pc.close()
        except Exception:
            pass

    def stop(self) -> None:
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(5)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=3)
