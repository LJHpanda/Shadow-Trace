import io
import inspect
import json
import os
import sys
import tempfile
import threading
import types
from pathlib import Path

_ISOLATED_DATA = Path(tempfile.gettempdir()) / "yingji-portable-runtime-tests"
os.environ.setdefault("YINGJI_DATA_DIR", str(_ISOLATED_DATA))
os.environ.setdefault("YINGJI_DOWNLOADS_DIR", str(_ISOLATED_DATA / "downloads"))

import server
import desktop_launcher
from core.runtime_paths import resolve_runtime_paths


def test_portable_paths_separate_resources_and_user_data(monkeypatch, tmp_path):
    app_dir = tmp_path / "影迹 Portable With Spaces"
    monkeypatch.setenv("YINGJI_PORTABLE", "1")
    monkeypatch.setenv("YINGJI_APP_DIR", str(app_dir))
    monkeypatch.delenv("YINGJI_DATA_DIR", raising=False)
    monkeypatch.delenv("YINGJI_DOWNLOADS_DIR", raising=False)
    monkeypatch.delenv("YINGJI_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("YINGJI_COOKIES_FILE", raising=False)

    paths = resolve_runtime_paths()

    assert paths.portable is True
    assert paths.app_dir == app_dir.resolve()
    assert paths.data_dir == (app_dir / "Data").resolve()
    assert paths.downloads_dir == (app_dir / "Downloads").resolve()
    assert paths.runtime_dir == (app_dir / "Runtime").resolve()
    assert paths.cookies_file == (app_dir / "Data" / "cookies.txt").resolve()
    assert paths.static_dir.name == "static"


def test_development_paths_remain_compatible(monkeypatch, tmp_path):
    data_dir = tmp_path / "isolated data"
    downloads_dir = tmp_path / "custom downloads"
    monkeypatch.delenv("YINGJI_PORTABLE", raising=False)
    monkeypatch.setenv("YINGJI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("YINGJI_DOWNLOADS_DIR", str(downloads_dir))

    paths = resolve_runtime_paths()

    assert paths.portable is False
    assert paths.data_dir == data_dir.resolve()
    assert paths.downloads_dir == downloads_dir.resolve()


def test_desktop_launcher_configures_bundled_ca_bundle(monkeypatch, tmp_path):
    ca_file = tmp_path / "cacert.pem"
    ca_file.write_text("test-ca", encoding="utf-8")
    fake_certifi = types.SimpleNamespace(where=lambda: str(ca_file))
    monkeypatch.setitem(sys.modules, "certifi", fake_certifi)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    configured = desktop_launcher._configure_ca_bundle()

    assert configured == ca_file.resolve()
    assert os.environ["SSL_CERT_FILE"] == str(ca_file.resolve())
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(ca_file.resolve())


def test_yt_dlp_command_has_no_managed_environment_hardcode():
    source = inspect.getsource(server._yt_dlp_command)
    assert "sys.executable" in source


def test_safe_download_error_codes_do_not_expose_details():
    assert server._safe_download_error_code(
        "ERROR: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    ) == "TLS_CERTIFICATE"
    assert server._safe_download_error_code("fresh cookies are needed") == "AUTH_REQUIRED"
    assert server._safe_download_error_code("connection timed out") == "NETWORK_ERROR"
    assert server._safe_download_error_code("signed-url=https://secret.example") == "DOWNLOAD_FAILED"


def test_directory_picker_is_visible_serialized_and_bounded(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = "C:\\Portable Downloads\r\n"

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    response = server.app.test_client().post("/api/choose-dir")

    assert response.status_code == 200
    assert response.get_json()["path"] == "C:\\Portable Downloads"
    assert captured["arguments"][0] == "powershell.exe"
    script = captured["arguments"][-1]
    assert "$owner.Show()" in script
    assert "$owner.Activate()" in script
    assert captured["kwargs"]["timeout"] == 90

    assert server._DIRECTORY_PICKER_LOCK.acquire(blocking=False)
    try:
        busy = server.app.test_client().post("/api/choose-dir")
        assert busy.status_code == 409
        assert busy.get_json()["error"] == "目录选择窗口已经打开"
    finally:
        server._DIRECTORY_PICKER_LOCK.release()


def test_portable_session_cookie_allows_same_origin(monkeypatch):
    monkeypatch.setattr(server, "_SESSION_TOKEN", "test-session-token")
    client = server.app.test_client()

    root = client.get("/")
    assert root.status_code == 200
    assert "yingji_session=" in root.headers.get("Set-Cookie", "")

    response = client.get(
        "/api/ffmpeg-status",
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 200


def test_portable_session_rejects_cross_site_source(monkeypatch):
    monkeypatch.setattr(server, "_SESSION_TOKEN", "test-session-token")
    client = server.app.test_client()
    client.get("/")

    response = client.post(
        "/api/app/quit",
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403
    assert response.get_json()["error"] == "Invalid request source"


def test_health_check_does_not_require_session(monkeypatch):
    monkeypatch.setattr(server, "_SESSION_TOKEN", "test-session-token")
    response = server.app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["version"].startswith("v2.7")


def test_portable_worker_dispatch_stays_before_selected_format(monkeypatch):
    monkeypatch.setattr(
        server,
        "_yt_dlp_command",
        lambda: ["YingJi.exe", "--yt-dlp-worker"],
    )

    command = server._yt_dlp_download_prefix("137+140")

    assert command == ["YingJi.exe", "--yt-dlp-worker", "-f", "137+140"]


def test_windowed_launcher_defers_output_setup_until_single_instance_owner():
    spec = (Path(__file__).parents[1] / "YingJi.spec").read_text(encoding="utf-8")
    source = inspect.getsource(desktop_launcher.run_desktop)

    assert "console=False" in spec
    assert source.index("if guard.already_running:") < source.index("_configure_output(paths)")


def test_installed_frozen_paths_use_user_writable_locations(monkeypatch, tmp_path):
    install_dir = tmp_path / "Programs" / "YingJi"
    local_app_data = tmp_path / "LocalAppData"
    user_home = tmp_path / "User"
    executable = install_dir / "YingJi.exe"
    runtime = install_dir / "Runtime"

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "_MEIPASS", str(runtime), raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("YINGJI_PORTABLE", raising=False)
    monkeypatch.delenv("YINGJI_DATA_DIR", raising=False)
    monkeypatch.delenv("YINGJI_DOWNLOADS_DIR", raising=False)
    monkeypatch.delenv("YINGJI_COOKIES_FILE", raising=False)

    paths = resolve_runtime_paths()

    assert paths.portable is False
    assert paths.app_dir == install_dir.resolve()
    assert paths.resource_dir == runtime.resolve()
    assert paths.data_dir == (local_app_data / "YingJi" / "Data").resolve()
    assert paths.downloads_dir == (user_home / "Downloads" / "YingJi").resolve()
    assert paths.cookies_file == (
        local_app_data / "YingJi" / "Data" / "cookies.txt"
    ).resolve()


def test_frozen_portable_marker_keeps_data_beside_executable(monkeypatch, tmp_path):
    app_dir = tmp_path / "YingJi Portable"
    runtime = app_dir / "Runtime"
    app_dir.mkdir(parents=True)
    (app_dir / "portable.flag").write_text("portable\n", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app_dir / "YingJi.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(runtime), raising=False)
    monkeypatch.delenv("YINGJI_PORTABLE", raising=False)
    monkeypatch.delenv("YINGJI_DATA_DIR", raising=False)
    monkeypatch.delenv("YINGJI_DOWNLOADS_DIR", raising=False)

    paths = resolve_runtime_paths()

    assert paths.portable is True
    assert paths.data_dir == (app_dir / "Data").resolve()
    assert paths.downloads_dir == (app_dir / "Downloads").resolve()


def test_runtime_cookie_is_under_data_in_portable_mode(monkeypatch, tmp_path):
    app_dir = tmp_path / ("长路径 " * 12)
    monkeypatch.setenv("YINGJI_PORTABLE", "1")
    monkeypatch.setenv("YINGJI_APP_DIR", str(app_dir))
    monkeypatch.delenv("YINGJI_COOKIES_FILE", raising=False)
    assert resolve_runtime_paths().cookies_file == Path(app_dir, "Data", "cookies.txt").resolve()


def test_excel_batch_template_round_trip():
    client = server.app.test_client()
    template = client.get("/api/import/excel/template")
    assert template.status_code == 200
    assert template.data.startswith(b"PK")

    imported = client.post(
        "/api/import/excel",
        data={"file": (io.BytesIO(template.data), "template.xlsx")},
        content_type="multipart/form-data",
    )
    assert imported.status_code == 200
    assert imported.get_json()["count"] == 2


def test_desktop_shutdown_closes_waitress_channels_and_workers():
    class Channel:
        closed = False

        def close(self):
            self.closed = True

    class Dispatcher:
        args = None

        def shutdown(self, **kwargs):
            self.args = kwargs

    class Httpd(Channel):
        def __init__(self):
            self._map = {1: Channel(), 2: Channel()}
            self.task_dispatcher = Dispatcher()

    httpd = Httpd()
    desktop_launcher._close_waitress_server(httpd)

    assert httpd.closed is True
    assert all(channel.closed for channel in httpd._map.values())
    assert httpd.task_dispatcher.args == {
        "cancel_pending": True,
        "timeout": 10,
    }


def test_quit_response_avoids_wsgi_hop_by_hop_header():
    requested = threading.Event()
    server.register_shutdown_callback(requested.set)
    response = server.app.test_client().post("/api/app/quit")
    assert response.status_code == 200
    assert "Connection" not in response.headers
    assert requested.wait(timeout=3)


def test_concurrent_task_and_group_saves_are_serialized(monkeypatch, tmp_path):
    tasks_file = tmp_path / "tasks.json"
    groups_file = tmp_path / "groups.json"
    monkeypatch.setattr(server, "TASKS_FILE", tasks_file)
    monkeypatch.setattr(server, "GROUPS_FILE", groups_file)
    manager = server.TaskManager()
    manager.tasks = {
        "one": {
            "id": "one",
            "status": "complete",
            "output_name": "one.mp4",
            "_proc": None,
        }
    }
    manager.groups = {
        "group": {"id": "group", "name": "并发保存验证", "parent_id": ""}
    }

    task_results = []

    def save_many():
        for _ in range(10):
            task_results.append(manager._save())
            manager._save_groups()

    workers = [threading.Thread(target=save_many) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert all(task_results)
    assert json.loads(tasks_file.read_text(encoding="utf-8"))["one"]["id"] == "one"
    assert json.loads(groups_file.read_text(encoding="utf-8"))["group"]["id"] == "group"
