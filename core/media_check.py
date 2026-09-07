"""媒体结果验证（P0-4）。

用 ffprobe 验证下载产物：容器可读、存在有效视频流、时长合理、
（可选）音频流存在。验证不过的文件绝不进入 complete 状态。
"""
import json
import logging
import os
import subprocess

log = logging.getLogger("m3u8-tool")

MIN_DURATION_SEC = 1.0        # 时长下限
MIN_BITRATE_BPS = 10_000      # 码率下限（10kbps，防空壳/截断文件）


def verify_media(path, ffprobe="ffprobe", expect_audio=False,
                 min_duration=MIN_DURATION_SEC, min_bitrate=MIN_BITRATE_BPS,
                 runner=None):
    """验证媒体文件。返回 dict：
    {ok, reason, duration, width, height, vcodec, acodec,
     has_video, has_audio, bitrate_bps}

    runner 供测试注入（签名同 subprocess.run）。
    """
    runner = runner or subprocess.run
    res = {
        "ok": False, "reason": "", "duration": 0.0,
        "width": 0, "height": 0, "vcodec": "", "acodec": "",
        "has_video": False, "has_audio": False, "bitrate_bps": 0,
    }
    if not os.path.exists(path):
        res["reason"] = "file_not_found"
        return res
    size = os.path.getsize(path)
    if size <= 0:
        res["reason"] = "empty_file"
        return res
    try:
        r = runner(
            [ffprobe, "-v", "error",
             "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as e:
        res["reason"] = f"ffprobe_error: {e}"
        return res
    if r.returncode != 0:
        res["reason"] = "container_unreadable"
        return res
    try:
        info = json.loads(r.stdout or "{}")
    except Exception:
        res["reason"] = "ffprobe_bad_output"
        return res

    streams = info.get("streams", []) or []
    fmt = info.get("format", {}) or {}
    for s in streams:
        if s.get("codec_type") == "video":
            res["has_video"] = True
            res["width"] = s.get("width") or res["width"]
            res["height"] = s.get("height") or res["height"]
            res["vcodec"] = res["vcodec"] or s.get("codec_name", "")
        elif s.get("codec_type") == "audio":
            res["has_audio"] = True
            res["acodec"] = res["acodec"] or s.get("codec_name", "")
    try:
        res["duration"] = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        res["duration"] = 0.0
    if res["duration"] > 0:
        res["bitrate_bps"] = int(size * 8 / res["duration"])

    if not res["has_video"] and not res["has_audio"]:
        res["reason"] = "no_media_streams"
        return res
    if not res["has_video"]:
        res["reason"] = "no_video_stream"
        return res
    if res["duration"] < min_duration:
        res["reason"] = f"duration_too_short ({res['duration']:.2f}s)"
        return res
    if res["bitrate_bps"] < min_bitrate:
        res["reason"] = f"bitrate_too_low ({res['bitrate_bps']} bps)"
        return res
    if expect_audio and not res["has_audio"]:
        res["reason"] = "no_audio_stream"
        return res

    res["ok"] = True
    return res


def reason_text(reason):
    """把验证失败原因转成用户可读中文提示。"""
    mapping = {
        "file_not_found": "文件不存在",
        "empty_file": "文件为空",
        "container_unreadable": "文件容器无法读取（可能已损坏或未下载完整）",
        "ffprobe_bad_output": "媒体信息解析失败",
        "no_media_streams": "文件中不含任何音视频流",
        "no_video_stream": "文件中不含视频流",
        "no_audio_stream": "文件中不含音频流",
    }
    for k, v in mapping.items():
        if reason.startswith(k):
            return v + ("" if reason == k else f"（{reason}）")
    if reason.startswith("duration_too_short"):
        return f"视频时长异常过短（{reason}）"
    if reason.startswith("bitrate_too_low"):
        return f"文件码率异常过低，疑似不完整（{reason}）"
    if reason.startswith("ffprobe_error"):
        return f"验证工具执行失败（{reason}）"
    return reason or "未知原因"
