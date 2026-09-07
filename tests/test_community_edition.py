from pathlib import Path

import server


ROOT = Path(__file__).resolve().parent.parent


def test_commercial_activation_implementation_is_absent():
    removed = [
        ROOT / "core" / "licensing.py",
        ROOT / "scripts" / "license_tool.py",
        ROOT / "legal" / "END_USER_LICENSE_AGREEMENT.txt",
        ROOT / "legal" / "LICENSE_PUBLIC_KEY.txt",
    ]

    assert all(not path.exists() for path in removed)

    server = (ROOT / "server.py").read_text(encoding="utf-8")
    frontend = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    for phrase in ("/api/license", "LICENSE_REQUIRED", "YINGJI_LICENSE_ENFORCEMENT"):
        assert phrase not in server
    for phrase in ("/license/activate", "订单授权码", "激活本机授权"):
        assert phrase not in frontend


def test_runtime_exposes_no_commercial_license_api_or_gate():
    routes = {rule.rule for rule in server.app.url_map.iter_rules()}

    assert not any(route.startswith("/api/license") for route in routes)
    assert not hasattr(server, "LICENSE_ENFORCEMENT")
    assert not hasattr(server, "LICENSE_FILE")


def test_internal_archives_and_static_prototypes_are_absent():
    removed_dirs = [
        ROOT / "docs" / "archive",
        ROOT / "static" / "legacy",
        ROOT / "static" / "historical-yingji",
    ]

    assert all(not any(path.rglob("*")) for path in removed_dirs)


def test_public_copy_identifies_the_gpl_community_edition():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert "全部社区版功能可直接使用，无设备激活机制" in readme
    assert "GPL-3.0-or-later" in readme
    assert "社区版" in html
    assert "单设备授权" not in html
