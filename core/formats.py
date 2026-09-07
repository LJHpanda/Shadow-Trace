"""画质 / 音视频轨结构化解析（P0-1）。

把 yt-dlp ``--dump-json`` 输出的 ``formats`` 列表，拆成三类结构化数据：
- ``video``    : 纯视频流（vcodec != none 且 acodec == none）
- ``audio``    : 纯音频流
- ``combined`` : 音视频同轨（直接可用，无需合并）

前端据此渲染「画质 + 音轨」双选择器；选纯视频轨时与最佳/指定音轨
以 ``video_id+audio_id`` 形式交给 yt-dlp 原生合并。

设计参考 media-downloader 的 MediaVariant 思路：一个可下载选项 = 视频资源 (+ 音频资源 可选)，
分离轨通过 ``requiresMerge``（此处即 ``+`` 合并语法）完成配对。
"""
from typing import Any, Dict, List, Tuple


def _codec(v: Any) -> str:
    return (v or "none").lower()


def classify_formats(info: Dict[str, Any]) -> Tuple[List[Dict], List[Dict], List[Dict], str]:
    """解析 yt-dlp dump-json 的 formats。

    返回 ``(video, audio, combined, best_audio_id)``。
    - ``best_audio_id``：音轨里码率最高者，作为「自动配对最佳音轨」默认。
      若没有独立音轨但有复合轨，则退回复合轨首条 id（同样含音）。
    """
    raw = (info or {}).get("formats") or []
    video: List[Dict] = []
    audio: List[Dict] = []
    combined: List[Dict] = []

    for f in raw:
        fid = f.get("format_id")
        if not fid:
            continue
        vcodec = _codec(f.get("vcodec"))
        acodec = _codec(f.get("acodec"))
        is_video = vcodec not in ("none", "", "null")
        is_audio = acodec not in ("none", "", "null")

        entry = {
            "id": str(fid),
            "ext": f.get("ext") or "",
            "filesize": f.get("filesize") or f.get("filesize_approx") or 0,
        }
        if is_video and is_audio:
            entry.update({
                "height": f.get("height") or 0,
                "width": f.get("width") or 0,
                "note": (f.get("format_note") or "").strip(),
            })
            combined.append(entry)
        elif is_video:
            entry.update({
                "height": f.get("height") or 0,
                "width": f.get("width") or 0,
                "fps": round(f.get("fps") or 0, 2),
                "vcodec": vcodec,
                "tbr": f.get("tbr") or 0,
                "dynamic_range": f.get("dynamic_range") or "",
                "note": (f.get("format_note") or "").strip(),
            })
            video.append(entry)
        elif is_audio:
            entry.update({
                "abr": f.get("abr") or 0,
                "acodec": acodec,
                "language": f.get("language") or "",
                "note": (f.get("format_note") or "").strip(),
            })
            audio.append(entry)

    # 视频按分辨率 → 码率降序；音轨按码率降序
    video.sort(key=lambda x: (x.get("height") or 0, x.get("tbr") or 0), reverse=True)
    audio.sort(key=lambda x: x.get("abr") or 0, reverse=True)

    best_audio_id = audio[0]["id"] if audio else (combined[0]["id"] if combined else "")
    return video, audio, combined, best_audio_id


def build_format_selector(format_id: str) -> str:
    """把前端传来的复合格式串归一化（占位/防御性处理）。

    yt-dlp 原生支持 ``video_id+audio_id`` 合并语法；本函数仅做基本清洗：
    - 空串 → 空（走默认最佳，已含音轨）
    - 含 ``+`` → 视为合并选择（后端据此强制校验音轨）
    直接返回原串即可，保留给未来扩展（如括号分组、回退语法）。
    """
    return (format_id or "").strip()
