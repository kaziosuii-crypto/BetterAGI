from __future__ import annotations

import io

import pytest

import record


class FakeStdin:
    def __init__(self):
        self.buf = io.BytesIO()
        self.closed = False

    def write(self, data):
        if self.closed:
            raise BrokenPipeError("closed")
        return self.buf.write(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, cmd, stdin=None):
        self.cmd = cmd
        self.stdin = FakeStdin()
        self.killed = False
        self._code = 0

    def wait(self, timeout=None):
        return self._code

    def kill(self):
        self.killed = True
        self._code = -9


class FakeIndex:
    def __init__(self):
        self.headers = []
        self.ends = []
        self.saves = 0

    def segment_header(self, file, start_ms, frame_no=0):
        self.headers.append((file, start_ms, frame_no))

    def segment_end(self, file, end_ms, frames=None):
        self.ends.append((file, end_ms, frames))

    def save(self):
        self.saves += 1


def test_build_cmd_has_segment_and_csv():
    cmd = record.build_ffmpeg_cmd(fps=10, out_pattern="seg_%03d.mp4", segment_ms=600000,
                                  list_path="segments.csv", size=(1280, 736))
    assert cmd[0] == "ffmpeg" and "-f" in cmd and "image2pipe" in cmd
    assert cmd[cmd.index("-segment_time") + 1] == "600.000"
    assert cmd[cmd.index("-segment_list_type") + 1] == "csv"
    assert cmd[cmd.index("-vf") + 1] == "scale=1280:736"
    assert cmd[-1] == "seg_%03d.mp4"


def test_parse_segment_list_sorts_and_tolerates_junk():
    text = "seg_001.mp4,600.000000,1200.000000\nseg_000.mp4,0.000000,600.000000\n\nbad,xx,yy\n"
    rows = record.parse_segment_list(text)
    assert [r["file"] for r in rows] == ["seg_000.mp4", "seg_001.mp4"]
    assert rows[1]["start_s"] == 600.0


def test_writer_feeds_frames_and_backfills_headers(tmp_path):
    index = FakeIndex()
    writer = record.SegmentWriter(tmp_path, fps=10, segment_ms=600000, index=index,
                                  start_ms=5000, popen_factory=FakeProc)
    started = writer.start()
    assert started["ok"] is True
    assert writer.write(b"\xff\xd8frame1") and writer.write(b"\xff\xd8frame2")
    assert writer.stats()["frames"] == 2

    (tmp_path / "segments").mkdir(exist_ok=True)
    (tmp_path / "segments.csv").write_text(
        "seg_000.mp4,0.000000,600.000000\nseg_001.mp4,600.000000,900.000000\n", encoding="utf-8")
    done = writer.stop()

    assert done["ok"] is True and done["frames"] == 2
    assert index.headers == [("seg_000.mp4", 5000, 0), ("seg_001.mp4", 605000, 6000)]
    # 分片段帧数按 ffmpeg 切点推算（600s*10fps、300s*10fps），不是写入帧数：
    # 末段未收尾时两者会差 1-2 帧，索引里以切点为准。
    assert index.ends[0] == ("seg_000.mp4", 605000, 6000)
    assert index.saves == 1


def test_writer_without_list_falls_back_to_file_names(tmp_path):
    (tmp_path / "segments").mkdir()
    (tmp_path / "segments" / "seg_000.mp4").write_bytes(b"x")
    (tmp_path / "segments" / "seg_001.mp4").write_bytes(b"x")
    writer = record.SegmentWriter(tmp_path, index=None, popen_factory=FakeProc)
    writer.start()
    done = writer.stop()
    assert [r["file"] for r in done["segments"]] == ["seg_000.mp4", "seg_001.mp4"]


def test_write_after_stop_is_false_and_broken_pipe_is_recorded(tmp_path):
    writer = record.SegmentWriter(tmp_path, popen_factory=FakeProc)
    writer.start()
    writer.stop()
    assert writer.write(b"x") is False

    broken = record.SegmentWriter(tmp_path, popen_factory=FakeProc)
    broken.start()
    broken.proc.stdin.close()
    assert broken.write(b"x") is False
    assert "BrokenPipe" in broken.last_error


def test_missing_ffmpeg_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(record.shutil, "which", lambda name: None)
    monkeypatch.delenv(record.FFMPEG_ENV, raising=False)
    writer = record.SegmentWriter(tmp_path)
    with pytest.raises(record.RecordError) as info:
        writer.start()
    assert "ffmpeg" in str(info.value)