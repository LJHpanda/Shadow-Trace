"""Cookie 应用内管理的端点测试（不触碰真实 Data/cookies.txt）。

通过 monkeypatch server.COOKIES_FILE 到临时文件隔离副作用；
POST/GET/DELETE 不调用 yt-dlp，仅校验文本格式与文件读写。
"""

import sys
import os
import tempfile
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server  # noqa: E402


@pytest.fixture
def client(tmp_path):
    server.COOKIES_FILE = tmp_path / "cookies.txt"
    server._cookie_args_cache = None
    return server.app.test_client()


VALID = "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\txxxx\n"
INVALID_NO_HEADER = "bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\txxxx\n"
INVALID_SHORT = "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\n"


def test_post_valid_then_info_then_delete(client):
    r = client.post("/api/cookies", json={"content": VALID})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["loaded"] is True
    assert body["size_bytes"] > 0

    info = client.get("/api/cookies/info").get_json()
    assert info["loaded"] is True
    assert info["sample_lines"] == 1
    # info 绝不返回内容
    assert "content" not in info

    d = client.delete("/api/cookies")
    assert d.get_json()["loaded"] is False
    assert client.get("/api/cookies/info").get_json()["loaded"] is False


def test_post_missing_header_rejected(client):
    r = client.post("/api/cookies", json={"content": INVALID_NO_HEADER})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_post_too_short_rejected(client):
    r = client.post("/api/cookies", json={"content": INVALID_SHORT})
    assert r.status_code == 400


def test_post_empty_rejected(client):
    r = client.post("/api/cookies", json={"content": ""})
    assert r.status_code == 400


def test_delete_when_absent_ok(client):
    assert client.delete("/api/cookies").get_json()["ok"] is True


def test_test_without_valid_file_returns_400(client):
    # client fixture 的 COOKIES_FILE 指向临时空路径，未写入即无效
    r = client.post("/api/cookies/test")
    assert r.status_code == 400


def test_empty_file_counts_as_not_loaded(client, tmp_path):
    server.COOKIES_FILE = tmp_path / "cookies.txt"
    server.COOKIES_FILE.write_text("")
    info = client.get("/api/cookies/info").get_json()
    assert info["loaded"] is False
    status = client.get("/api/ffmpeg-status").get_json()
    assert status["cookies_loaded"] is False
