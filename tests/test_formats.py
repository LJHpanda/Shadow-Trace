"""P0-1 画质 / 音视频轨结构化解析测试（离线，不访问真实平台）。

核心逻辑见 core/formats.classify_formats：把 yt-dlp --dump-json 的 formats
拆成 video / audio / combined 三类，供前端「画质 + 音轨」双选择器使用。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.formats import classify_formats, build_format_selector


# 模拟 yt-dlp --dump-json 的 formats 片段（YouTube 风格）
SAMPLE_FORMATS = [
    {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2",
     "height": 360, "width": 640, "tbr": 0.6, "filesize": 10_000_000},
    {"format_id": "22", "ext": "mp4", "vcodec": "avc1.64001F", "acodec": "mp4a.40.2",
     "height": 720, "width": 1280, "tbr": 2.0, "filesize": 40_000_000},
    {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none",
     "height": 1080, "width": 1920, "fps": 30, "tbr": 3.0, "filesize": 60_000_000},
    {"format_id": "248", "ext": "webm", "vcodec": "vp9", "acodec": "none",
     "height": 1080, "width": 1920, "fps": 30, "tbr": 2.5, "filesize": 50_000_000},
    {"format_id": "313", "ext": "webm", "vcodec": "vp9", "acodec": "none",
     "height": 2160, "width": 3840, "fps": 24, "tbr": 13.0, "filesize": 300_000_000},
    {"format_id": "399", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
     "abr": 48, "filesize": 1_200_000},
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
     "abr": 128, "language": "zh", "filesize": 3_200_000},
    {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus",
     "abr": 160, "language": "en", "filesize": 4_000_000},
    # 无效格式（无音视频流）应被跳过
    {"format_id": "0", "ext": "m3u8", "vcodec": "none", "acodec": "none"},
]


def test_classify_separates_three_categories():
    video, audio, combined, best = classify_formats({"formats": SAMPLE_FORMATS})
    vids = {v["id"] for v in video}
    aids = {a["id"] for a in audio}
    cids = {c["id"] for c in combined}
    assert vids == {"137", "248", "313"}
    assert aids == {"399", "140", "251"}
    assert cids == {"18", "22"}


def test_video_sorted_by_height_desc_then_tbr():
    video, _, _, _ = classify_formats({"formats": SAMPLE_FORMATS})
    heights = [v["height"] for v in video]
    assert heights == [2160, 1080, 1080]  # 313, 248, 137
    # 同 1080P：tbr 高者(248=2.5)应先于低者(137=3.0?) 实际 137 tbr=3.0 > 248=2.5
    # 所以排序为 313(2160) > 137(1080,3.0) > 248(1080,2.5)
    assert video[1]["id"] == "137"
    assert video[2]["id"] == "248"


def test_audio_sorted_by_abr_desc_and_best_picked():
    _, audio, _, best = classify_formats({"formats": SAMPLE_FORMATS})
    abrs = [a["abr"] for a in audio]
    assert abrs == [160, 128, 48]  # 251, 140, 399
    assert best == "251"  # 最高码率音轨作为自动配对默认


def test_combined_flagged_and_carries_resolution():
    _, _, combined, _ = classify_formats({"formats": SAMPLE_FORMATS})
    by_id = {c["id"]: c for c in combined}
    assert by_id["22"]["height"] == 720
    assert by_id["18"]["height"] == 360


def test_empty_and_missing_formats():
    assert classify_formats({}) == ([], [], [], "")
    assert classify_formats({"formats": []}) == ([], [], [], "")
    assert classify_formats(None) == ([], [], [], "")


def test_best_audio_falls_back_to_combined_when_no_pure_audio():
    info = {"formats": [
        {"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a",
         "height": 360},
    ]}
    video, audio, combined, best = classify_formats(info)
    assert audio == []
    assert best == "18"  # 无独立音轨 → 退回到复合轨


def test_build_format_selector_passthrough():
    assert build_format_selector("") == ""
    assert build_format_selector("313+251") == "313+251"
    assert build_format_selector("  302 ") == "302"
    # + 视为合并选择（后端据此强制校验音轨）
    assert "+" in build_format_selector("137+140")
