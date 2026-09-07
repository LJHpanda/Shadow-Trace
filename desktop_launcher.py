"""Windows Portable desktop entry point for YingJi."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from core.runtime_paths import resolve_runtime_paths


APP_VERSION = "v2.7.0"
ERROR_ALREADY_EXISTS = 183


def _configure_ca_bundle():
    """Use the bundled CA roots in both the desktop server and worker mode."""
    try:
        import certifi

        ca_file = Path(certifi.where()).resolve()
        if ca_file.is_file():
            os.environ["SSL_CERT_FILE"] = str(ca_file)
            os.environ["REQUESTS_CA_BUNDLE"] = str(ca_file)
            return ca_file
    except (ImportError, OSError):
        pass
    return None


def _run_yt_dlp_worker(arguments):
    """Run the bundled yt-dlp CLI inside a child YingJi process."""
    _restore_worker_standard_streams()
    from core.network_policy import install_yt_dlp_network_guard

    install_yt_dlp_network_guard()
    from yt_dlp import main as yt_dlp_main

    try:
        result = yt_dlp_main(arguments)
        return int(result or 0)
    except SystemExit as exc:
        return int(exc.code or 0)


def _restore_worker_standard_streams():
    """Restore redirected pipes in the windowed PyInstaller worker process."""
    for name, descriptor in (("stdout", 1), ("stderr", 2)):
        if getattr(sys, name, None) is not None:
            continue
        try:
            stream = os.fdopen(
                os.dup(descriptor),
                "w",
                buffering=1,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            stream = open(os.devnull, "w", encoding="utf-8")
        setattr(sys, name, stream)


def _configure_output(paths):
    """Configure stdout/stderr/stdin for the frozen desktop app.

    Default: redirect to a log file so no console window appears.
    Set YINGJI_SHOW_CONSOLE=1 to keep the legacy preview console for debugging.
    """
    if os.name != "nt":
        return False

    show_console = os.environ.get("YINGJI_SHOW_CONSOLE") == "1"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p

    if show_console:
        created = False
        if not kernel32.GetConsoleWindow():
            if not kernel32.AllocConsole():
                raise ctypes.WinError(ctypes.get_last_error())
            created = True
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleTitleW("影迹 Portable 调试版")
        if created or sys.stdout is None:
            sys.stdout = open(
                "CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace"
            )
        if created or sys.stderr is None:
            sys.stderr = open(
                "CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace"
            )
        if created or sys.stdin is None:
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        return created

    # No console window: redirect stdout/stderr to a log file so failures are
    # still diagnosable, and ensure stdin is not None for libraries that read it.
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        log_stream = open(
            paths.data_dir / "launcher.log",
            "a",
            buffering=1,
            encoding="utf-8",
            errors="replace",
        )
        sys.stdout = log_stream
        sys.stderr = log_stream
    except OSError:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = sys.stdout
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8", errors="replace")
    return False


class WindowsSingleInstance:
    def __init__(self, app_dir: Path):
        self.handle = None
        self.already_running = False
        if os.name != "nt":
            return
        identity = hashlib.sha256(str(app_dir).lower().encode("utf-8")).hexdigest()[:16]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        self._kernel32 = kernel32
        self.handle = kernel32.CreateMutexW(
            None, False, f"Local\\YingJiPortable-{identity}"
        )
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    def close(self):
        if self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def _read_instance(instance_file: Path):
    try:
        payload = json.loads(instance_file.read_text(encoding="utf-8"))
        port = int(payload["port"])
        if not 1 <= port <= 65535:
            return None
        return payload
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _health_url(port):
    return f"http://127.0.0.1:{port}/healthz"


def _wait_for_health(port, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(_health_url(port), timeout=1.0) as response:
                if response.status == 200:
                    payload = json.loads(response.read().decode("utf-8"))
                    if payload.get("ok"):
                        return True
        except Exception:
            time.sleep(0.15)
    return False


def _open_existing_instance(instance_file: Path):
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        payload = _read_instance(instance_file)
        if payload and _wait_for_health(payload["port"], timeout=0.5):
            if os.environ.get("YINGJI_NO_BROWSER") != "1":
                webbrowser.open(f"http://127.0.0.1:{payload['port']}/")
            return True
        time.sleep(0.2)
    return False


def _write_instance(instance_file: Path, port: int):
    payload = {
        "pid": os.getpid(),
        "port": port,
        "version": APP_VERSION,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    temp_file = instance_file.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temp_file, instance_file)


def _remove_own_instance(instance_file: Path):
    payload = _read_instance(instance_file)
    if payload and payload.get("pid") == os.getpid():
        try:
            instance_file.unlink()
        except FileNotFoundError:
            pass


def _validate_runtime(server_module):
    missing = []
    if not server_module._yt_dlp_available():
        missing.append("yt-dlp")
    if not server_module.FFMPEG_PATH or not Path(server_module.FFMPEG_PATH).is_file():
        missing.append("ffmpeg.exe")
    ffprobe = server_module._ffprobe_path()
    if not ffprobe or not Path(ffprobe).is_file():
        missing.append("ffprobe.exe")
    if missing:
        raise RuntimeError("Portable 运行时不完整：" + "、".join(missing))


def _close_waitress_server(httpd):
    """Close the listener, keep-alive channels, and worker dispatcher."""
    try:
        httpd.close()
    except Exception:
        pass
    httpd.task_dispatcher.shutdown(cancel_pending=True, timeout=10)
    for channel in list(getattr(httpd, "_map", {}).values()):
        try:
            channel.close()
        except Exception:
            pass


def run_desktop():
    paths = resolve_runtime_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.downloads_dir.mkdir(parents=True, exist_ok=True)
    instance_file = paths.data_dir / "instance.json"

    guard = WindowsSingleInstance(paths.app_dir)
    if guard.already_running:
        opened = _open_existing_instance(instance_file)
        if not opened:
            print("影迹已在运行，但暂时无法打开现有页面。请稍后再试。")
        guard.close()
        return 0 if opened else 2

    _configure_output(paths)
    os.environ["YINGJI_SESSION_TOKEN"] = secrets.token_urlsafe(32)
    server = None
    httpd = None
    try:
        import server
        from waitress.server import create_server

        _validate_runtime(server)
        server.prepare_runtime()
        httpd = create_server(server.app, host="127.0.0.1", port=0, threads=8)
        port = int(httpd.effective_port)
        _write_instance(instance_file, port)
        stop_once = threading.Event()

        def request_shutdown():
            if stop_once.is_set():
                return
            stop_once.set()
            server.save_runtime_state()
            _close_waitress_server(httpd)

        server.register_shutdown_callback(request_shutdown)
        worker = threading.Thread(target=httpd.run, name="yingji-http", daemon=False)
        worker.start()
        if not _wait_for_health(port):
            raise RuntimeError("本地服务启动后未能通过健康检查")

        url = f"http://127.0.0.1:{port}/"
        print(f"影迹 {APP_VERSION} 已启动：{url}")
        print(f"数据目录：{paths.data_dir}")
        print(f"下载目录：{paths.downloads_dir}")
        if os.environ.get("YINGJI_NO_BROWSER") != "1":
            webbrowser.open(url)
        worker.join()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"影迹启动失败：{exc}", file=sys.stderr)
        return 1
    finally:
        if httpd is not None and server is not None:
            try:
                server.save_runtime_state()
            except Exception:
                pass
            try:
                _close_waitress_server(httpd)
            except Exception:
                pass
        _remove_own_instance(instance_file)
        guard.close()


def main():
    _configure_ca_bundle()
    if len(sys.argv) >= 2 and sys.argv[1] == "--yt-dlp-worker":
        return _run_yt_dlp_worker(sys.argv[2:])
    return run_desktop()


if __name__ == "__main__":
    raise SystemExit(main())
