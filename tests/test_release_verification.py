from pathlib import Path

import pytest

from scripts.verify_release import REQUIRED_FILES, verify_release


def _make_release(root: Path):
    for relative in REQUIRED_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative in {"LICENSE", "legal/COPYING.GPLv3.txt"}:
            path.write_text("GNU GENERAL PUBLIC LICENSE\nVersion 3\n", encoding="utf-8")
        else:
            path.write_bytes(b"release-test")


def test_clean_release_passes(tmp_path):
    release = tmp_path / "YingJi"
    _make_release(release)

    result = verify_release(release)

    assert result["ok"] is True
    assert result["missing_required"] == []
    assert result["forbidden_payloads"] == []
    assert result["invalid_legal_files"] == []


def test_release_rejects_runtime_data_and_media(tmp_path):
    release = tmp_path / "YingJi"
    _make_release(release)
    (release / "Data").mkdir()
    (release / "Data" / "tasks.json").write_text("{}", encoding="utf-8")
    (release / "sample.mp4").write_bytes(b"not-media")

    result = verify_release(release)

    assert result["ok"] is False
    assert "Data/" in result["forbidden_payloads"]
    assert "Data/tasks.json" in result["forbidden_payloads"]
    assert "sample.mp4" in result["forbidden_payloads"]


@pytest.mark.parametrize("relative", ["LICENSE", "legal/COPYING.GPLv3.txt"])
def test_release_rejects_placeholder_gpl_license(tmp_path, relative):
    release = tmp_path / "YingJi"
    _make_release(release)
    (release / relative).write_text(
        "PLACEHOLDER - NOT FOR DISTRIBUTION\n",
        encoding="utf-8",
    )

    result = verify_release(release)

    assert result["ok"] is False
    assert result["invalid_legal_files"] == [relative]


def test_release_documentation_is_valid_utf8():
    project_root = Path(__file__).parents[1]
    documents = [
        project_root / "README.md",
        project_root / "LICENSE",
        project_root / "legal" / "THIRD_PARTY_NOTICES.txt",
        project_root / "legal" / "FFMPEG_SOURCE.txt",
    ]

    decoded = [path.read_text(encoding="utf-8") for path in documents]

    assert "Windows" in decoded[0]
    assert "GNU GENERAL PUBLIC LICENSE" in decoded[1]
    assert "FFmpeg" in decoded[2]
    assert "ffmpeg-8.1.2.tar.xz" in decoded[3]
