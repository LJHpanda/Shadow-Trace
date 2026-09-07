from pathlib import Path

from scripts.public_release_check import check_public_source


ROOT = Path(__file__).resolve().parent.parent


def test_current_source_snapshot_passes_public_policy():
    result = check_public_source(ROOT)

    assert result["ok"] is True, result


def test_windows_setup_uses_project_virtual_environment():
    setup = (ROOT / "setup.bat").read_text(encoding="utf-8")
    start = (ROOT / "start.bat").read_text(encoding="utf-8")

    assert "-m venv .venv" in setup
    assert ".venv\\Scripts\\python.exe" in setup
    assert ".venv\\Scripts\\python.exe" in start
    assert "YINGJI_PID_FILE" in start


def test_stop_script_never_kills_processes_by_global_image_name():
    stop = (ROOT / "stop.bat").read_text(encoding="utf-8").lower()

    assert "/im yt-dlp.exe" not in stop
    assert "/im ffmpeg.exe" not in stop
    assert 'commandline -notmatch \'server\\.py\'' in stop
    assert "executablepath" in stop
    assert "taskkill /f /t /pid" in stop


def test_private_repository_is_not_documented_as_directly_publishable():
    publishing = (ROOT / "docs" / "PUBLISHING.md").read_text(encoding="utf-8")

    assert "不要直接公开当前私有仓库" in publishing
    assert "新的公开仓库" in publishing
    assert "无 Git 历史" in publishing
