# -*- coding: utf-8 -*-
"""目录可写性探针与 /api/startup-status 真实行为测试。

覆盖：
- 已存在且可写目录返回 True；
- 不存在目录返回 False，且检查后目录仍不存在（不自动创建）；
- 文件路径返回 False，且文件内容不被覆盖（固定探针名 + O_TRUNC 覆盖）；
- 写入失败 / 读取失败均返回 False；
- 探针文件名固定（.yingji_write_probe.tmp）且限定在目标目录内（沙箱 unlink 受限时仅残留至多一个固定文件）；
- 正常与异常检查后均无探针残留；
- /api/startup-status 字段向后兼容并追加 exists / is_directory；
- 接口不读取任务、分组、检测记录或 Cookie 内容。

数据隔离：模块导入前把 YINGJI_DATA_DIR 指向仓库内 .verify_tmp 下的隔离目录，
不触碰真实 tasks.json / downloads / Cookie。
"""
import builtins
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_DATA = ROOT / ".verify_tmp" / "startup_status_data"
_ISOLATED_DATA.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("YINGJI_DATA_DIR", str(_ISOLATED_DATA))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402


# ---------- _probe_download_dir 真实行为 ----------
def test_probe_writable_existing_dir_returns_true(tmp_path):
    result = server._probe_download_dir(tmp_path)
    assert result == {"exists": True, "is_directory": True, "writable": True}
    assert server._check_dir_writable(tmp_path) is True


def test_probe_missing_dir_returns_false_and_does_not_create(tmp_path):
    target = tmp_path / "not_created_by_probe"
    result = server._probe_download_dir(target)
    assert result["exists"] is False
    assert result["is_directory"] is False
    assert result["writable"] is False
    # 状态检查不得改变文件系统：目录检查后仍不存在
    assert not target.exists()


def test_probe_file_path_returns_false_and_keeps_content(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_text("original", encoding="utf-8")
    result = server._probe_download_dir(plain)
    assert result["exists"] is True
    assert result["is_directory"] is False
    assert result["writable"] is False
    # 现有文件不被覆盖
    assert plain.read_text(encoding="utf-8") == "original"


def test_probe_write_failure_returns_false(tmp_path, monkeypatch):
    def deny_open(path, flags, *args, **kwargs):
        raise OSError("write denied")

    monkeypatch.setattr(os, "open", deny_open)
    result = server._probe_download_dir(tmp_path)
    assert result["exists"] is True and result["is_directory"] is True
    assert result["writable"] is False


def test_probe_read_failure_returns_false_and_cleans_up(tmp_path, monkeypatch):
    real_open = builtins.open

    def deny_read(path, *args, **kwargs):
        if ".yingji_write_probe.tmp" in str(path):
            raise OSError("read denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny_read)
    result = server._probe_download_dir(tmp_path)
    assert result["writable"] is False
    # 异常路径下 finally 清理：无探针残留
    assert list(tmp_path.glob(".yingji_write_probe.tmp")) == []


def test_probe_path_is_fixed_and_confined_to_target(tmp_path, monkeypatch):
    captured = []
    real_os_open = os.open

    def spy(path, flags, *args, **kwargs):
        captured.append(Path(path))
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    server._probe_download_dir(tmp_path)
    server._probe_download_dir(tmp_path)
    # 探针文件名固定且限定在目标目录内（沙箱 unlink 受限时仅残留至多一个固定文件）
    assert len(captured) == 2
    for probe in captured:
        assert probe.parent == tmp_path                 # 限定在目标目录内
        assert probe.name == ".yingji_write_probe.tmp"  # 固定名，非随机
    # 跨调用复用同一固定名（确定性，不生成随机临时文件）
    assert captured[0].name == captured[1].name


def test_probe_uses_trunc_creation(tmp_path, monkeypatch):
    flags_seen = []
    real_os_open = os.open

    def spy(path, flags, *args, **kwargs):
        flags_seen.append(flags)
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    server._probe_download_dir(tmp_path)
    # 固定探针名 + O_TRUNC 覆盖：避免每次请求生成新的随机临时文件
    # （O_EXCL 已刻意弃用，否则沙箱 unlink 受限会累积大量残留文件）
    assert flags_seen and (flags_seen[0] & os.O_TRUNC) == os.O_TRUNC


def test_probe_no_residue_after_normal_check(tmp_path):
    server._probe_download_dir(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_probe_does_not_read_other_files(tmp_path, monkeypatch):
    (tmp_path / "tasks.json").write_text("{}", encoding="utf-8")
    (tmp_path / "cookies.txt").write_text("secret", encoding="utf-8")
    opened = []
    real_open = builtins.open
    real_os_open = os.open

    def spy_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    def spy_os_open(path, flags, *args, **kwargs):
        opened.append(str(path))
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    server._probe_download_dir(tmp_path)
    # 只允许打开自己的探针文件，不读取目标目录中的其他文件
    # （运行环境的删除守卫可能在目录外写审计报告，不属于探针行为，过滤掉）
    inside_target = [p for p in opened if str(tmp_path) in p]
    assert inside_target and all(".yingji_write_probe.tmp" in p for p in inside_target)


# ---------- /api/startup-status 接口契约 ----------
def test_startup_status_backward_compatible_fields():
    client = server.app.test_client()
    resp = client.get("/api/startup-status")
    assert resp.status_code == 200
    data = resp.get_json()
    # 原有字段不得破坏
    for key in ("version", "ffmpeg", "yt_dlp", "cookies_loaded",
                "download_dir", "download_dir_writable"):
        assert key in data, f"缺少向后兼容字段 {key}"
    assert data["version"] == "v2.7"
    # 只追加两个细分字段
    assert isinstance(data["download_dir_exists"], bool)
    assert isinstance(data["download_dir_is_directory"], bool)
    assert isinstance(data["download_dir_writable"], bool)


def test_startup_status_does_not_read_task_group_record_or_cookie(monkeypatch):
    opened = []
    real_open = builtins.open

    def spy_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    client = server.app.test_client()
    assert client.get("/api/startup-status").status_code == 200
    forbidden = ("tasks.json", "groups.json", "detect_records", "cookies")
    leaked = [p for p in opened if any(word in p for word in forbidden)]
    assert leaked == [], f"接口不应读取任务/分组/检测记录/Cookie 内容：{leaked}"
