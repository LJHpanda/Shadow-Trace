from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_root_license_is_full_gpl_v3_text():
    root_license = (ROOT / "LICENSE").read_bytes()
    bundled_gpl = (ROOT / "legal" / "COPYING.GPLv3.txt").read_bytes()

    assert root_license == bundled_gpl
    assert b"GNU GENERAL PUBLIC LICENSE" in root_license
    assert b"Version 3, 29 June 2007" in root_license
    assert len(root_license) > 30_000


def test_readme_declares_project_license_identifier():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "GPL-3.0-or-later" in readme
    assert "[`LICENSE`](LICENSE)" in readme
