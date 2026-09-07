"""P0-4 媒体结果验证测试。

用注入 runner 模拟 ffprobe，不调用真实 ffprobe、不下载真实媒体。
"""
import json

from core.media_check import verify_media, reason_text


class FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def make_runner(returncode=0, payload=None):
    stdout = json.dumps(payload) if payload is not None else ""
    def runner(cmd, **kwargs):
        return FakeProc(returncode=returncode, stdout=stdout)
    return runner


def _good_payload(duration=60.0, audio=True):
    streams = [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {"streams": streams, "format": {"duration": str(duration)}}


class TestVerifyMedia:
    def test_big_garbage_file_not_complete(self, tmp_path):
        # P0-4 核心：>1MB 但 ffprobe 读不了 → 不许判完成
        f = tmp_path / "garbage.mp4"
        f.write_bytes(b"\x00" * (2 * 1024 * 1024))  # 2MB 垃圾
        res = verify_media(f, runner=make_runner(returncode=1))
        assert res["ok"] is False
        assert res["reason"] == "container_unreadable"

    def test_no_streams_not_complete(self, tmp_path):
        f = tmp_path / "empty_streams.mp4"
        f.write_bytes(b"x" * (2 * 1024 * 1024))
        res = verify_media(f, runner=make_runner(
            payload={"streams": [], "format": {"duration": "60"}}))
        assert res["ok"] is False
        assert res["reason"] == "no_media_streams"

    def test_audio_only_no_video_fails(self, tmp_path):
        f = tmp_path / "audio_only.mp4"
        f.write_bytes(b"x" * 500_000)
        res = verify_media(f, runner=make_runner(payload={
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
            "format": {"duration": "60"}}))
        assert res["ok"] is False
        assert res["reason"] == "no_video_stream"

    def test_duration_too_short_fails(self, tmp_path):
        f = tmp_path / "short.mp4"
        f.write_bytes(b"x" * 500_000)
        res = verify_media(f, runner=make_runner(payload=_good_payload(duration=0.3)))
        assert res["ok"] is False
        assert res["reason"].startswith("duration_too_short")

    def test_bitrate_too_low_fails(self, tmp_path):
        # 60 秒但只有 1KB → 码率远低于 10kbps
        f = tmp_path / "hollow.mp4"
        f.write_bytes(b"x" * 1024)
        res = verify_media(f, runner=make_runner(payload=_good_payload(duration=60)))
        assert res["ok"] is False
        assert res["reason"].startswith("bitrate_too_low")

    def test_expect_audio_missing_fails(self, tmp_path):
        f = tmp_path / "mute.mp4"
        f.write_bytes(b"x" * 5_000_000)
        res = verify_media(f, expect_audio=True,
                           runner=make_runner(payload=_good_payload(duration=60, audio=False)))
        assert res["ok"] is False
        assert res["reason"] == "no_audio_stream"

    def test_valid_file_passes(self, tmp_path):
        f = tmp_path / "good.mp4"
        f.write_bytes(b"x" * 5_000_000)  # 5MB / 60s ≈ 666kbps
        res = verify_media(f, expect_audio=True,
                           runner=make_runner(payload=_good_payload(duration=60)))
        assert res["ok"] is True
        assert res["has_video"] and res["has_audio"]
        assert res["width"] == 1920 and res["vcodec"] == "h264"

    def test_missing_file(self, tmp_path):
        res = verify_media(tmp_path / "nope.mp4", runner=make_runner())
        assert res["ok"] is False and res["reason"] == "file_not_found"

    def test_empty_file(self, tmp_path):
        f = tmp_path / "zero.mp4"
        f.write_bytes(b"")
        res = verify_media(f, runner=make_runner())
        assert res["ok"] is False and res["reason"] == "empty_file"

    def test_ffprobe_crash_handled(self, tmp_path):
        f = tmp_path / "x.mp4"
        f.write_bytes(b"x" * 100)
        def boom(cmd, **kw):
            raise OSError("ffprobe not found")
        res = verify_media(f, runner=boom)
        assert res["ok"] is False and res["reason"].startswith("ffprobe_error")


class TestReasonText:
    def test_known_reasons_chinese(self):
        assert "容器" in reason_text("container_unreadable")
        assert "时长" in reason_text("duration_too_short (0.30s)")
        assert "码率" in reason_text("bitrate_too_low (136 bps)")
        assert reason_text("") == "未知原因"
