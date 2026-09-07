"""任务文件清单与回收站（P0-2 / P0-3）。

原则：
- 回收站失败 → 一律保留原文件（retained），任何平台都不降级为永久删除；
- 永久删除只允许用于白名单临时分片，且必须位于指定任务目录内；
- 所有删除操作返回逐文件结果 trashed / retained / failed / missing。
"""
import logging
import os
import subprocess
from pathlib import Path

try:
    import send2trash as _send2trash
except ImportError:
    _send2trash = None

log = logging.getLogger("m3u8-tool")

# 明确识别的临时文件后缀/特征（永久删除白名单）
TEMP_SUFFIXES = (".part", ".ytdl", ".tmp", ".frag", ".tmp.mp4")
TEMP_MARKERS = (".part-frag", "-frag")

RESULT_TRASHED = "trashed"
RESULT_RETAINED = "retained"
RESULT_MISSING = "missing"


def is_temp_file(path):
    """是否为明确识别的临时分片/中间文件。"""
    name = Path(path).name.lower()
    if name.endswith(TEMP_SUFFIXES):
        return True
    return any(m in name for m in TEMP_MARKERS)


def _is_within(base_dir, path):
    try:
        base = Path(base_dir).resolve()
        p = Path(path).resolve()
        return p == base or base in p.parents
    except Exception:
        return False


def send_to_trash(path):
    """移动单个文件/目录到系统回收站。

    返回 RESULT_TRASHED / RESULT_RETAINED / RESULT_MISSING。
    **任何失败都保留原文件，绝不降级为永久删除。**
    """
    fp = Path(path)
    if not fp.exists():
        return RESULT_MISSING
    if os.name == "nt":
        try:
            esc = str(fp).replace("'", "''")
            op = "DeleteDirectory" if fp.is_dir() else "DeleteFile"
            ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
                  f"[Microsoft.VisualBasic.FileIO.FileSystem]::{op}('{esc}', "
                  "'OnlyErrorDialogs', 'SendToRecycleBin')")
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return RESULT_TRASHED if not fp.exists() else RESULT_RETAINED
        except Exception as e:
            log.warning(f"send_to_trash(PowerShell) failed for {fp}: {e}")
            return RESULT_RETAINED
    # 非 Windows：send2trash，失败保留
    try:
        if _send2trash is not None:
            _send2trash.send2trash(str(fp))
            return RESULT_TRASHED if not fp.exists() else RESULT_RETAINED
    except Exception as e:
        log.warning(f"send_to_trash(send2trash) failed for {fp}: {e}")
    return RESULT_RETAINED


def trash_files(paths, within_dir=None, trash_fn=None):
    """批量移入回收站，返回逐文件结果列表。

    within_dir 提供时，路径解析后必须位于该目录内，否则拒绝（failed）。
    trash_fn 供测试注入。
    """
    trash_fn = trash_fn or send_to_trash
    results = []
    for p in paths:
        entry = {"path": str(p)}
        if within_dir is not None and not _is_within(within_dir, p):
            entry["result"] = "failed"
            entry["reason"] = "outside_output_dir"
            results.append(entry)
            continue
        entry["result"] = trash_fn(p)
        results.append(entry)
    return results


def task_file_manifest(task, default_downloads_dir):
    """返回任务的精确文件清单（绝对路径字符串列表），绝不使用通配。

    优先级：task['files'] > task['file_path'] > 旧任务回退（output_dir/output_name
    以及常见改扩展名的精确候选，逐一存在性检查，不做 glob）。
    """
    out_dir = Path(task.get("output_dir") or default_downloads_dir)
    files = []
    for f in (task.get("files") or []):
        if f:
            files.append(str(f))
    fp = task.get("file_path")
    if fp and fp not in files:
        files.append(str(fp))
    if not files:
        name = task.get("output_name") or ""
        if name:
            stem = Path(name).stem
            # 精确候选：output_name 本身 + 已知容器扩展名（非通配）
            for cand in [name] + [stem + ext for ext in (".mp4", ".mkv", ".webm", ".flv")]:
                p = out_dir / cand
                if p.exists() and str(p) not in files:
                    files.append(str(p))
    return files


def permanent_delete_temp(path, task_dir, unlink_fn=None):
    """永久删除单个**临时文件**。仅当同时满足：
    1) is_temp_file(path)；2) path 位于 task_dir 内。
    否则拒绝并返回 False。
    """
    if not is_temp_file(path):
        log.warning(f"permanent_delete_temp rejected (not temp): {path}")
        return False
    if not _is_within(task_dir, path):
        log.warning(f"permanent_delete_temp rejected (outside task dir): {path}")
        return False
    if unlink_fn is not None:
        return unlink_fn(path)
    try:
        Path(path).unlink(missing_ok=True)
        return not Path(path).exists()
    except Exception as e:
        log.warning(f"permanent_delete_temp failed for {path}: {e}")
        return False


def summarize(results):
    """把逐文件结果汇总为 {trashed, retained, failed, missing, all_ok}。"""
    counts = {"trashed": 0, "retained": 0, "failed": 0, "missing": 0}
    for r in results:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    counts["all_ok"] = (counts["retained"] == 0 and counts["failed"] == 0)
    return counts
