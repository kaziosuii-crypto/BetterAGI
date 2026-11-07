"""录播分片：ffmpeg image2pipe 直收画面，10 分钟一片，每片首帧对齐时间轴。

设计 02 §6 / 03 C.14 的要求：
- 一条一直跟录的连续流，切成 ``seg_000.mp4`` …（默认 600s 一片）；
- 每片要有 sidecar 记录**这一片第一帧**的 ``t_ms``（相对本场开始的单调毫秒）与帧号；
- 分片名与 session_index 里的名字同一个（``seg_%03d.mp4``），剪辑时能对上。

实现取舍：**不自己数帧猜时间**。让 ffmpeg 写 ``-segment_list_type csv``（每行
``文件名,起始秒,结束秒``），停止时按真实切点回填 sidecar——猜 ``帧数/fps`` 在掉帧时
会和画面错位，而剪辑认的是画面。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_FPS = 10.0
DEFAULT_SEGMENT_MS = 600000
SEGMENT_STEM = "seg_%03d.mp4"
LIST_NAME = "segments.csv"
FFMPEG_ENV = "BETTERAGI_FFMPEG"


class RecordError(RuntimeError):
    """录播起不来：要么没 ffmpeg，要么管道写不进去。"""


def ffmpeg_path() -> str:
    import os

    explicit = (os.environ.get(FFMPEG_ENV) or "").strip()
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if not found:
        raise RecordError(
            "找不到 ffmpeg：装一个并加进 PATH，或用环境变量 %s 指定绝对路径" % FFMPEG_ENV
        )
    return found


def build_ffmpeg_cmd(
    *,
    fps: float = DEFAULT_FPS,
    out_pattern: str,
    segment_ms: int = DEFAULT_SEGMENT_MS,
    list_path: Optional[str] = None,
    size: Optional[Sequence[int]] = None,
    crf: int = 23,
    exe: str = "ffmpeg",
) -> List[str]:
    """拼命令行（单独可测：不启动进程）。"""

    rate = max(0.1, float(fps))
    cmd = [
        exe,
        "-hide_banner",
        "-loglevel", "warning",
        "-f", "image2pipe",
        "-framerate", "%g" % rate,
        "-vcodec", "mjpeg",
        "-i", "-",
    ]
    if size:
        cmd += ["-vf", "scale=%d:%d" % (int(size[0]), int(size[1]))]
    cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p",
        "-g", str(max(2, int(round(rate * 2)))),
        "-f", "segment",
        "-segment_time", "%.3f" % (max(1, int(segment_ms)) / 1000.0),
    ]
    if list_path:
        cmd += ["-segment_list", str(list_path), "-segment_list_type", "csv"]
    cmd += ["-reset_timestamps", "1", str(out_pattern)]
    return cmd


def parse_segment_list(text: str) -> List[Dict[str, Any]]:
    """解析 ffmpeg 的 csv：``seg_000.mp4,0.000000,600.000000``（秒）。"""

    rows: List[Dict[str, Any]] = []
    for raw in str(text or "").splitlines():
        parts = [cell.strip() for cell in raw.strip().split(",")]
        if len(parts) < 2 or not parts[0]:
            continue
        try:
            start_s = float(parts[1])
        except ValueError:
            continue
        end_s = None
        if len(parts) >= 3:
            try:
                end_s = float(parts[2])
            except ValueError:
                end_s = None
        rows.append({"file": parts[0], "start_s": start_s, "end_s": end_s})
    rows.sort(key=lambda row: row["start_s"])
    return rows


class SegmentWriter:
    """把 JPEG 帧按顺序喂给 ffmpeg，落成 10 分钟分片并回填 sidecar。"""

    def __init__(
        self,
        root: Any,
        *,
        fps: float = DEFAULT_FPS,
        segment_ms: int = DEFAULT_SEGMENT_MS,
        size: Optional[Sequence[int]] = None,
        index: Any = None,
        start_ms: int = 0,
        crf: int = 23,
        popen_factory: Any = None,
    ) -> None:
        self.root = Path(root)
        self.fps = max(0.1, float(fps))
        self.segment_ms = max(1, int(segment_ms))
        self.size = list(size) if size else None
        self.index = index
        self.start_ms = int(start_ms)
        self.crf = int(crf)
        self._popen_factory = popen_factory
        self.proc: Any = None
        self.list_path = self.root / LIST_NAME
        self.dir = self.root / "segments"
        self.frames = 0
        self.bytes = 0
        self.started_at = 0.0
        self.last_error = ""
        self.segments: List[Dict[str, Any]] = []

    # ----------------------------------------------------------- 生命周期
    def start(self) -> Dict[str, Any]:
        if self.proc is not None:
            return {"ok": True, "already": True}
        self.dir.mkdir(parents=True, exist_ok=True)
        pattern = str(self.dir / SEGMENT_STEM)
        if self._popen_factory is None:
            exe = ffmpeg_path()
        else:
            exe = "ffmpeg"
        cmd = build_ffmpeg_cmd(
            fps=self.fps,
            out_pattern=pattern,
            segment_ms=self.segment_ms,
            list_path=str(self.list_path),
            size=self.size,
            crf=self.crf,
            exe=exe,
        )
        factory = self._popen_factory or subprocess.Popen
        try:
            self.proc = factory(cmd, stdin=subprocess.PIPE)
        except Exception as error:  # noqa: BLE001
            self.last_error = "%s: %s" % (type(error).__name__, error)
            raise RecordError("起不了 ffmpeg：%s" % self.last_error) from error
        self.started_at = time.monotonic()
        return {"ok": True, "cmd": cmd, "pattern": pattern}

    def write(self, jpeg: Any) -> bool:
        """喂一帧；进程死了不抛（录播不能把直播带崩），只记 last_error 并返回 False。"""

        if self.proc is None or getattr(self.proc, "stdin", None) is None:
            return False
        data = bytes(jpeg or b"")
        if not data:
            return False
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except Exception as error:  # noqa: BLE001 - 断管只停录播
            self.last_error = "%s: %s" % (type(error).__name__, error)
            return False
        self.frames += 1
        self.bytes += len(data)
        return True

    def stop(self, timeout: float = 10.0) -> Dict[str, Any]:
        proc, self.proc = self.proc, None
        if proc is None:
            return {"ok": False, "reason": "not_started"}
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception as error:  # noqa: BLE001
            self.last_error = "%s: %s" % (type(error).__name__, error)
        try:
            code = proc.wait(timeout=timeout)
        except Exception as error:  # noqa: BLE001 - 卡住的 ffmpeg 直接杀
            self.last_error = "%s: %s" % (type(error).__name__, error)
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            code = -1
        self.segments = self._read_list()
        headers = self._write_headers(self.segments)
        return {
            "ok": code == 0 and bool(self.segments),
            "code": code,
            "frames": self.frames,
            "bytes": self.bytes,
            "segments": self.segments,
            "headers": headers,
            "error": self.last_error,
        }

    # ----------------------------------------------------------- 收尾细节
    def _read_list(self) -> List[Dict[str, Any]]:
        try:
            return parse_segment_list(self.list_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as error:  # noqa: BLE001 - 没清单就退回按文件名数片
            self.last_error = self.last_error or "%s: %s" % (type(error).__name__, error)
            files = sorted(p.name for p in self.dir.glob("seg_*.mp4"))
            return [{"file": name, "start_s": i * self.segment_ms / 1000.0, "end_s": None}
                    for i, name in enumerate(files)]

    def _write_headers(self, rows: Sequence[Dict[str, Any]]) -> int:
        if self.index is None:
            return 0
        written = 0
        for row in rows:
            start_ms = self.start_ms + int(round(float(row["start_s"]) * 1000))
            frame_no = int(round(float(row["start_s"]) * self.fps))
            try:
                self.index.segment_header(row["file"], start_ms, frame_no)
                if row.get("end_s") is not None:
                    end_s = float(row["end_s"])
                    count = max(1, int(round((end_s - float(row["start_s"])) * self.fps)))
                    self.index.segment_end(row["file"],
                                           self.start_ms + int(round(end_s * 1000)),
                                           count)
                written += 1
            except Exception as error:  # noqa: BLE001 - sidecar 写不进去要留痕但不炸
                self.last_error = "%s: %s" % (type(error).__name__, error)
        try:
            save = getattr(self.index, "save", None)
            if callable(save):
                save()
        except Exception as error:  # noqa: BLE001
            self.last_error = "%s: %s" % (type(error).__name__, error)
        return written

    def stats(self) -> Dict[str, Any]:
        return {
            "running": self.proc is not None,
            "frames": self.frames,
            "bytes": self.bytes,
            "segments": len(self.segments),
            "dir": str(self.dir),
            "error": self.last_error,
        }