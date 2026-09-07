import asyncio
from types import SimpleNamespace

import pytest

import server
from core.telegram_bot_runner import TelegramBotRunner, _safe_public_url
from core.telegram_bot_store import TelegramBotStore


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.message_id = 7
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeContextBot:
    async def send_chat_action(self, **kwargs):
        return None


def fake_update(chat_id, text="", first_name="测试用户"):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(first_name=first_name),
        message=FakeMessage(text),
    )


def test_remote_start_only_returns_pairing_id_and_never_claims_owner(tmp_path):
    store = TelegramBotStore(tmp_path)
    runner = TelegramBotRunner(store, lambda *args: None, lambda task_id: None)
    update = fake_update(123456)

    asyncio.run(runner._cmd_start(update, None))

    assert store.get("owner_chat_id") == ""
    assert store.has_access_policy() is False
    assert store.is_allowed(123456) is False
    assert "123456" in update.message.replies[0]
    assert "不会创建任何下载任务" in update.message.replies[0]
    assert store.adopt_owner(123456) is False


def test_unauthorized_chat_cannot_submit_download(tmp_path):
    store = TelegramBotStore(tmp_path)
    store.update_config({"whitelist": ["111"]})
    called = []
    runner = TelegramBotRunner(
        store,
        lambda *args: called.append(args),
        lambda task_id: None,
    )
    update = fake_update(222, "https://example.com/video")

    asyncio.run(runner._on_text(update, SimpleNamespace(bot=FakeContextBot())))

    assert called == []
    assert update.message.replies == ["当前未对你开放使用权限。"]


def test_per_chat_pending_quota_blocks_third_task(tmp_path):
    store = TelegramBotStore(tmp_path)
    store.add_mapping("task-1", "111")
    store.add_mapping("task-2", "111")
    runner = TelegramBotRunner(store, lambda *args: None, lambda task_id: None)

    allowed, reason = runner._reserve_submission("111")

    assert allowed is False
    assert "两个任务" in reason


def test_per_chat_rate_limit_is_independent(tmp_path):
    store = TelegramBotStore(tmp_path)
    runner = TelegramBotRunner(store, lambda *args: None, lambda task_id: None)

    for _ in range(6):
        allowed, _ = runner._reserve_submission("111")
        assert allowed is True
        runner._release_submission("111")

    allowed, reason = runner._reserve_submission("111")
    other_allowed, _ = runner._reserve_submission("222")

    assert allowed is False
    assert "一分钟" in reason
    assert other_allowed is True
    runner._release_submission("222")


def test_download_rejection_message_is_safe_for_allowed_chat(tmp_path):
    store = TelegramBotStore(tmp_path)
    store.update_config({"whitelist": ["111"]})

    class Rejected(Exception):
        user_message = "这个链接未通过安全检查"

    def reject(*args):
        raise Rejected("private diagnostic")

    runner = TelegramBotRunner(store, reject, lambda task_id: None)
    update = fake_update(111, "https://example.com/video")

    asyncio.run(runner._on_text(update, SimpleNamespace(bot=FakeContextBot())))

    assert update.message.replies == [Rejected.user_message]
    logs = store.get_logs()
    assert logs[-1]["message"] == "创建任务失败"


def test_store_validates_chat_ids_and_loopback_large_file_api(tmp_path):
    store = TelegramBotStore(tmp_path)

    cfg = store.update_config({"whitelist": ["123", "bad", "-456", "123"]})
    assert cfg["whitelist"] == ["123", "-456"]

    with pytest.raises(ValueError, match="Telegram Chat ID"):
        store.update_config({"owner_chat_id": "not-a-chat"})
    with pytest.raises(ValueError, match="只能使用本机"):
        store.update_config({"local_api_url": "https://attacker.example"})

    cfg = store.update_config({"local_api_url": "http://127.0.0.1:8081/"})
    assert cfg["local_api_url"] == "http://127.0.0.1:8081"


def test_translation_credentials_never_return_to_browser(tmp_path):
    store = TelegramBotStore(tmp_path)
    cfg = store.update_config({
        "token": "123456789:TEST_ONLY_PLACEHOLDER_TOKEN_ABC",
        "translation": {
            "api_key": "sk-sensitive-value",
            "secret_id": "secret-id-value",
            "secret_key": "secret-key-value",
        }
    })

    translation = cfg["translation"]
    assert cfg["token"] == ""
    assert cfg["token_configured"] is True
    assert translation["api_key"] == ""
    assert translation["secret_id"] == ""
    assert translation["secret_key"] == ""
    assert translation["api_key_configured"] is True
    assert "sk-sensitive-value" not in str(cfg)


def test_task_failure_reply_does_not_expose_internal_error(tmp_path):
    store = TelegramBotStore(tmp_path)
    store.update_config({"token": "123456789:TEST_ONLY_PLACEHOLDER_TOKEN_ABC"})
    store.add_mapping("task-1", "111")
    runner = TelegramBotRunner(
        store,
        lambda *args: None,
        lambda task_id: {
            "status": "failed",
            "error": (
                "C:\\private\\output.mp4 "
                "123456789:TEST_ONLY_PLACEHOLDER_TOKEN_ABC"
            ),
        },
    )
    messages = []

    async def capture(chat_id, text):
        messages.append(text)

    runner._safe_reply = capture
    asyncio.run(runner._check_task("task-1", store.get_mapping("task-1")))

    assert "C:\\private" not in messages[0]
    assert "abcdefghijklmnopqrstuvwxyz" not in messages[0]
    assert "本机影迹的任务列表" in messages[0]


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini",
    "http://localhost/admin",
    "http://127.0.0.1:5001/api/tasks",
    "http://[::1]/",
    "http://192.168.1.20/video",
    "http://user:password@example.com/video",
    "http://2130706433/admin",
])
def test_download_entry_rejects_local_or_credentialed_urls(url):
    with pytest.raises(server.DownloadRequestRejected):
        server._validated_download_url(url)


def test_download_entry_allows_and_normalizes_public_url():
    assert (
        server._validated_download_url(
            "https://www.douyin.com/jingxuan?modal_id=123456"
        )
        == "https://www.douyin.com/video/123456"
    )


def test_local_session_rejects_non_loopback_remote(monkeypatch):
    monkeypatch.setattr(server, "_SESSION_TOKEN", "test-session-token")
    client = server.app.test_client()
    client.get("/")

    response = client.get(
        "/api/ffmpeg-status",
        headers={"Origin": "http://localhost", "Host": "localhost"},
        environ_overrides={"REMOTE_ADDR": "192.168.1.20"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "Invalid local host"


def test_public_url_removes_signatures_but_keeps_resource_identity():
    safe = _safe_public_url(
        "https://example.com/watch?v=abc&token=secret&X-Amz-Signature=signed#fragment"
    )

    assert safe == "https://example.com/watch?v=abc"
    assert "secret" not in safe
    assert "signed" not in safe


def test_caption_is_bounded_to_telegram_media_limit(tmp_path):
    store = TelegramBotStore(tmp_path)
    runner = TelegramBotRunner(store, lambda *args: None, lambda task_id: None)
    info = {
        "url": "https://example.com/watch?v=abc",
        "caption": {"title": "标题", "description": "内容" * 1000, "uploader": "author"},
    }

    caption = runner._build_caption("task-1", info)

    assert len(caption) <= 1000
    assert "https://example.com/watch?v=abc" in caption
    assert "#task-1" in caption


def test_large_file_fallback_does_not_expose_absolute_path(tmp_path):
    store = TelegramBotStore(tmp_path / "data")
    store.update_config({"max_direct_send_mb": 0})
    media = tmp_path / "private" / "example.mp4"
    media.parent.mkdir()
    media.write_bytes(b"video")
    store.add_mapping("task-1", "111", url="https://example.com/watch?v=abc")
    runner = TelegramBotRunner(store, lambda *args: None, lambda task_id: None)
    messages = []

    async def capture(chat_id, text):
        messages.append(text)

    runner._safe_reply = capture
    asyncio.run(runner._deliver("111", str(media), "task-1"))

    assert str(media) not in messages[0]
    assert "example.mp4" in messages[0]
    assert "TelegramBot 下载目录" in messages[0]
