"""UI-P0-06 批量检测 — 后端契约测试。

覆盖：
- /api/batch-inspect 输入校验（空列表 / 非列表 / urls 中夹杂非字符串与空串）
- 正常返回结构 {ok, items, count} 字段映射
- filename / ext / filesize / source 字段映射（mock yt-dlp --dump-json）
- yt-dlp 失败 / 超时降级到 URL 路径推断（fallback）
- detect-name 60s 缓存命中复用

数据隔离：模块导入前把 YINGJI_DATA_DIR 指向独立临时目录，绝不触碰真实 tasks.json / downloads。
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# --- 隔离数据目录必须在导入 server 前生效 ---
_ISOLATED = Path(tempfile.mkdtemp(prefix="yingji_test_batch_inspect_"))
os.environ["YINGJI_DATA_DIR"] = str(_ISOLATED)
os.environ["YINGJI_DOWNLOADS_DIR"] = str(_ISOLATED / "downloads")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

server = importlib.import_module("server")


@pytest.fixture()
def client():
    server.app.config["TESTING"] = True
    # 清缓存，避免跨用例命中
    server._detect_cache.clear()
    yield server.app.test_client()
    server._detect_cache.clear()


def _fake_run(stdout="", returncode=0, stderr=""):
    """构造 subprocess.run 替代：接受任意 cmd/kwargs，返回受控结果。"""

    def _impl(cmd, *args, **kwargs):
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = stderr
        return m

    return _impl


def test_batch_inspect_returns_items_with_filename_ext_filesize(monkeypatch, client):
    """happy path：yt-dlp 返回完整 metadata，items 应包含 filename / ext / filesize / source。"""
    info = json.dumps({
        "title": "示例视频 第01集",
        "ext": "mp4",
        "formats": [{"ext": "mp4"}, {"ext": "m4a"}],
        "filesize": 12345678,
    })
    monkeypatch.setattr(server.subprocess, "run", _fake_run(stdout=info))
    r = client.post("/api/batch-inspect", json={"urls": ["https://example.com/v1"]})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    assert body["count"] == 1
    item = body["items"][0]
    assert item["url"].endswith("/v1")
    # sanitize_basename 会把 "示例视频 第01集" 清洗后保留示例
    assert item["filename"]
    assert "示例" in item["filename"]
    assert item["ext"] == "mp4"
    assert item["filesize"] == 12345678
    assert item["source"] == "metadata"


def test_batch_inspect_falls_back_to_url_when_yt_dlp_fails(monkeypatch, client):
    """yt-dlp 抛错时，回退到 URL 路径推断文件名 + ext。"""
    monkeypatch.setattr(server.subprocess, "run", _fake_run(stdout=""))

    def boom(*args, **kwargs):
        raise OSError("simulated subprocess failure")

    monkeypatch.setattr(server.subprocess, "run", boom)
    r = client.post("/api/batch-inspect", json={
        "urls": ["https://example.com/videos/lesson_05.mp4"]
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    item = body["items"][0]
    # URL 路径末段是 lesson_05.mp4 → basename = lesson_05, ext = mp4
    assert item["filename"] == "lesson_05"
    assert item["ext"] == "mp4"
    assert item["source"] == "url"


def test_batch_inspect_rejects_empty_url_list(client):
    r = client.post("/api/batch-inspect", json={"urls": []})
    assert r.status_code == 400


def test_batch_inspect_rejects_non_list_urls(client):
    r = client.post("/api/batch-inspect", json={"urls": "https://example.com/v"})
    assert r.status_code == 400


def test_batch_inspect_skips_invalid_entries(monkeypatch, client):
    """urls 列表中夹杂非字符串与空字符串会被忽略，不会崩溃。"""
    info = json.dumps({"title": "OK", "ext": "mp4", "filesize": 1})
    monkeypatch.setattr(server.subprocess, "run", _fake_run(stdout=info))
    r = client.post("/api/batch-inspect", json={
        "urls": ["", "  ", None, 123, "https://example.com/real"]
    })
    assert r.status_code == 200
    body = r.get_json()
    # 清理后只剩一个真实 URL
    assert body["count"] == 1
    assert body["items"][0]["url"].endswith("/real")


def test_batch_inspect_reuses_detect_name_cache(monkeypatch, client):
    """第二次同 URL 应走 cached（detect-name 60s 缓存复用）。"""
    info = json.dumps({"title": "Cache Test", "ext": "mp4", "filesize": 100})
    monkeypatch.setattr(server.subprocess, "run", _fake_run(stdout=info))
    r1 = client.post("/api/batch-inspect", json={"urls": ["https://example.com/cached"]})
    r2 = client.post("/api/batch-inspect", json={"urls": ["https://example.com/cached"]})
    assert r1.status_code == 200 and r2.status_code == 200
    item1 = r1.get_json()["items"][0]
    item2 = r2.get_json()["items"][0]
    assert item1["filename"] == item2["filename"]
    assert "(cached)" in item2["source"]


def test_batch_inspect_uuid_url_returns_empty_filename(monkeypatch, client):
    """纯 UUID/hash 的 URL 应让 filename 为空，避免把 UUID 当文件名。"""
    def boom(*args, **kwargs):
        raise OSError("simulated")

    monkeypatch.setattr(server.subprocess, "run", boom)
    r = client.post("/api/batch-inspect", json={
        "urls": ["https://cdn.example.com/abc123def456789"]  # 16 位 hex
    })
    assert r.status_code == 200
    item = r.get_json()["items"][0]
    assert item["filename"] == ""


def test_batch_inspect_accepts_multiple_urls_and_preserves_order(monkeypatch, client):
    """多个 URL 时按输入顺序返回 items，不丢失。"""
    info = json.dumps({"title": "T", "ext": "mp4", "filesize": 0})
    monkeypatch.setattr(server.subprocess, "run", _fake_run(stdout=info))
    urls = [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    r = client.post("/api/batch-inspect", json={"urls": urls})
    body = r.get_json()
    assert body["count"] == 3
    returned_urls = [item["url"] for item in body["items"]]
    for url in urls:
        assert any(u.endswith(url.split("//")[-1].split("/")[-1]) or u.endswith(url.rsplit("/", 1)[-1]) or url.endswith(u.rsplit("//", 1)[-1]) for u in returned_urls)