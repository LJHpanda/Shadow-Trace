"""UI-P0-03 完成状态真实性 — 后端契约测试。

覆盖：
- /api/tasks/<id>/confirm 的状态流转 / 400 / 404 / 持久化 / 回滚
- completion_basis 派生（verified / user_confirmed / legacy_unknown）
- /api/tasks 输出字段（completion_basis / confirmed_at / verify_ok / has_audio）
- 统计聚合 completion_basis 构成且 KPI 口径不变
- 服务重启时 verifying 任务降级为 incomplete

数据隔离：模块导入前把 YINGJI_DATA_DIR 指向独立临时目录，
绝不触碰真实 tasks.json / downloads。
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# --- 隔离数据目录必须在导入 server 前生效 ---
_ISOLATED = Path(tempfile.mkdtemp(prefix="yingji_test_completion_"))
os.environ["YINGJI_DATA_DIR"] = str(_ISOLATED)
os.environ["YINGJI_DOWNLOADS_DIR"] = str(_ISOLATED / "downloads")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

server = importlib.import_module("server")


def _mk_task(tid, status, verify=None, basis=None, file_size=1024, **extra):
    t = {
        "id": tid, "url": f"https://example.com/{tid}", "output_name": f"{tid}.mp4",
        "status": status, "progress": 100 if status == "complete" else 50,
        "speed": "", "eta": "", "size": "", "fragments": "",
        "file_size": file_size, "created_at": "2026-07-29T10:00:00",
        "completed_at": "2026-07-29T10:30:00" if status == "complete" else "",
    }
    if verify is not None:
        t["verify"] = verify
    if basis is not None:
        t["completion_basis"] = basis
    t.update(extra)
    return t


@pytest.fixture()
def client():
    server.app.config["TESTING"] = True
    with server.task_manager._lock:
        server.task_manager.tasks = {}
    yield server.app.test_client()
    with server.task_manager._lock:
        server.task_manager.tasks = {}


def _put(*tasks):
    with server.task_manager._lock:
        for t in tasks:
            server.task_manager.tasks[t["id"]] = t


# ---------- confirm 接口 ----------

def test_confirm_complete_sets_user_confirmed(client):
    _put(_mk_task("t1", "incomplete", verify={"ok": False, "reason": "no_duration"}))
    r = client.post("/api/tasks/t1/confirm", json={"result": "complete"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["status"] == "complete"
    assert body["completion_basis"] == "user_confirmed"
    assert body["confirmed_at"]
    t = server.task_manager.tasks["t1"]
    assert t["completion_basis"] == "user_confirmed"
    assert t["confirmed_at"]
    # 自动检查证据必须原样保留，不被人工确认覆盖
    assert t["verify"] == {"ok": False, "reason": "no_duration"}
    assert "人工确认保留" in t["confirmation_note"]


def test_confirm_failed_sets_error(client):
    _put(_mk_task("t2", "incomplete"))
    r = client.post("/api/tasks/t2/confirm", json={"result": "failed"})
    assert r.status_code == 200
    t = server.task_manager.tasks["t2"]
    assert t["status"] == "error"
    assert t["confirmed_at"]
    assert t.get("completion_basis") is None


def test_confirm_rejects_complete_task_with_400(client):
    _put(_mk_task("t3", "complete", verify={"ok": True}))
    r = client.post("/api/tasks/t3/confirm", json={"result": "complete"})
    assert r.status_code == 400


def test_confirm_rejects_downloading_task_with_400(client):
    _put(_mk_task("t4", "downloading"))
    r = client.post("/api/tasks/t4/confirm", json={"result": "complete"})
    assert r.status_code == 400


def test_confirm_missing_task_returns_404(client):
    r = client.post("/api/tasks/nope/confirm", json={"result": "complete"})
    assert r.status_code == 404


def test_confirm_invalid_result_returns_400(client):
    _put(_mk_task("t5", "incomplete"))
    r = client.post("/api/tasks/t5/confirm", json={"result": "banana"})
    assert r.status_code == 400


def test_confirm_empty_body_defaults_to_complete(client):
    """旧请求体兼容：无 body 时默认 result=complete。"""
    _put(_mk_task("t6", "incomplete"))
    r = client.post("/api/tasks/t6/confirm")
    assert r.status_code == 200
    assert server.task_manager.tasks["t6"]["status"] == "complete"


def test_confirm_persists_to_disk(client):
    _put(_mk_task("t7", "incomplete"))
    r = client.post("/api/tasks/t7/confirm", json={"result": "complete"})
    assert r.status_code == 200
    on_disk = json.loads(Path(server.TASKS_FILE).read_text(encoding="utf-8"))
    assert on_disk["t7"]["completion_basis"] == "user_confirmed"
    assert on_disk["t7"]["status"] == "complete"


def test_confirm_save_failure_rolls_back_and_returns_500(client, monkeypatch):
    _put(_mk_task("t8", "incomplete", error="残留"))
    monkeypatch.setattr(server.task_manager, "_save", lambda: False)
    r = client.post("/api/tasks/t8/confirm", json={"result": "complete"})
    assert r.status_code == 500
    t = server.task_manager.tasks["t8"]
    assert t["status"] == "incomplete"          # 内存已回滚
    assert t.get("completion_basis") is None
    assert t.get("confirmed_at") is None


# ---------- completion_basis 派生 ----------

def test_basis_verified_for_new_tasks_and_passing_verify():
    assert server._completion_basis(
        _mk_task("a", "complete", basis="verified")) == "verified"
    assert server._completion_basis(
        _mk_task("b", "complete", verify={"ok": True})) == "verified"


def test_basis_derivation_for_legacy_tasks():
    # 有 verify 但未通过 → 只可能经旧版人工确认进入完成态
    assert server._completion_basis(
        _mk_task("c", "complete", verify={"ok": False, "reason": "x"})) == "user_confirmed"
    # 无 verify 记录 → 历史未知
    assert server._completion_basis(_mk_task("d", "complete")) == "legacy_unknown"
    # 非 complete 一律 None
    assert server._completion_basis(_mk_task("e", "incomplete")) is None
    assert server._completion_basis(_mk_task("f", "error")) is None


# ---------- API 输出字段 ----------

def test_tasks_api_exposes_completion_fields(client):
    _put(
        _mk_task("v1", "complete", verify={"ok": True, "has_audio": True}, basis="verified"),
        _mk_task("v2", "complete", basis="user_confirmed",
                 confirmed_at="2026-07-29T11:00:00", confirmation_note="note"),
        _mk_task("v3", "complete"),
    )
    r = client.get("/api/tasks")
    assert r.status_code == 200
    by_id = {t["id"]: t for t in r.get_json()}
    assert by_id["v1"]["completion_basis"] == "verified"
    assert by_id["v1"]["verify_ok"] is True
    assert by_id["v1"]["has_audio"] is True
    assert by_id["v2"]["completion_basis"] == "user_confirmed"
    assert by_id["v2"]["confirmed_at"] == "2026-07-29T11:00:00"
    assert by_id["v3"]["completion_basis"] == "legacy_unknown"
    for f in ("completion_basis", "confirmed_at", "verify_ok", "has_audio"):
        assert f in by_id["v3"]


# ---------- 统计口径 ----------

def test_stats_completion_basis_breakdown_and_kpi_unchanged(client):
    _put(
        _mk_task("s1", "complete", verify={"ok": True}, basis="verified", file_size=100),
        _mk_task("s2", "complete", basis="user_confirmed", file_size=200),
        _mk_task("s3", "complete", file_size=0),  # legacy，无大小：不计 KPI 但计依据构成
        _mk_task("s4", "incomplete", file_size=999),
    )
    agg = server._stats_aggregate()
    assert agg["completion_basis"] == {
        "verified": 1, "user_confirmed": 1, "legacy_unknown": 1}
    # KPI 口径不变：仍按「complete 且有 file_size」统计
    assert agg["kpi"]["total"]["count"] == 2
    assert agg["kpi"]["total"]["bytes"] == 300


def test_stats_report_states_saved_caliber(client):
    _put(_mk_task("r1", "complete", basis="user_confirmed", file_size=100))
    r = client.get("/api/stats/report?range=all")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "已保存文件" in html
    assert "不代表全部通过自动检查" in html


# ---------- 重启恢复 ----------

def test_verifying_task_becomes_incomplete_on_reload(client):
    Path(server.TASKS_FILE).write_text(json.dumps({
        "z1": _mk_task("z1", "verifying"),
    }, ensure_ascii=False), encoding="utf-8")
    tm = server.TaskManager()
    assert tm.tasks["z1"]["status"] == "incomplete"
    assert "文件检查未完成" in tm.tasks["z1"]["error"]


# ---------- 周期统计回归 ----------

def _week_iso(offset_days=0):
    from datetime import date, timedelta
    return (date.today() - timedelta(days=offset_days)).isoformat() + "T10:00:00"


def test_report_basis_counts_follow_range_selection(client):
    """周报只有本周任务时，完成依据数量必须与周报文件数量对应，不混入历史任务。"""
    _put(
        # 本周：1 个自动检查通过
        _mk_task("w1", "complete", verify={"ok": True}, basis="verified",
                 file_size=100, completed_at=_week_iso(0)),
        # 历史（120 天前）：1 个 legacy_unknown + 1 个旧人工确认
        _mk_task("w2", "complete", file_size=200, completed_at=_week_iso(120)),
        _mk_task("w3", "complete", verify={"ok": False, "reason": "no_duration"},
                 file_size=300, completed_at=_week_iso(120)),
    )
    html = client.get("/api/stats/report?range=week").get_data(as_text=True)
    assert "自动检查通过 1 个" in html
    # 历史任务不得混入本周构成
    assert "历史记录" not in html
    assert "人工确认保留 1 个" not in html
    # range=all 时三类才应齐全
    html_all = client.get("/api/stats/report?range=all").get_data(as_text=True)
    assert "自动检查通过 1 个" in html_all
    assert "人工确认保留 1 个" in html_all
    assert "历史记录（无检查数据）1 个" in html_all


def test_report_basis_counts_sum_matches_file_count(client):
    """构成数量之和 = 当前区间「N 个文件」的 N（同一数据集）。"""
    _put(
        _mk_task("c1", "complete", verify={"ok": True}, basis="verified",
                 file_size=10, completed_at=_week_iso(0)),
        _mk_task("c2", "complete", basis="user_confirmed",
                 file_size=20, completed_at=_week_iso(0)),
    )
    html = client.get("/api/stats/report?range=week").get_data(as_text=True)
    assert "2 个文件" in html
    assert "自动检查通过 1 个" in html
    assert "人工确认保留 1 个" in html


_FORBIDDEN_IN_UI = ("no_duration", "unreadable", "ffprobe", "Traceback",
                    "WinError", "C:\\", "_", ".exe")


def test_verify_reason_is_fully_user_language(client):
    """API 面向 UI 的 verify_reason 完全用户语言化，原始 verify.reason 保留。"""
    raw_reasons = [
        "no_duration",
        "container_unreadable",
        "ffprobe_error: [WinError 2] C:\\tools\\ffprobe.exe not found",
        "duration_too_short (0.30s)",
        "bitrate_too_low (136 bps)",
        "some_future_internal_code_42",
    ]
    tasks = [
        _mk_task(f"vr{i}", "incomplete", verify={"ok": False, "reason": r})
        for i, r in enumerate(raw_reasons)
    ]
    _put(*tasks)
    out = {t["id"]: t for t in client.get("/api/tasks").get_json()}
    for i, raw in enumerate(raw_reasons):
        ui = out[f"vr{i}"]["verify_reason"]
        assert ui, f"vr{i} 应有用户可读原因"
        for bad in _FORBIDDEN_IN_UI:
            assert bad not in ui, f"UI 原因泄漏内部信息 {bad!r}: {ui!r}"
        # 不含任何 ASCII 字母/反斜杠/括号参数（纯中文文案）
        assert not any(ch.isascii() and ch.isalpha() for ch in ui), ui
        # 证据不被破坏：内存中的原始 verify.reason 原样保留
        assert server.task_manager.tasks[f"vr{i}"]["verify"]["reason"] == raw


def test_confirmation_note_contains_no_internal_codes(client):
    _put(_mk_task("cn1", "incomplete",
                  verify={"ok": False, "reason": "ffprobe_error: Traceback xx C:\\a\\b.mp4"}))
    r = client.post("/api/tasks/cn1/confirm", json={"result": "complete"})
    assert r.status_code == 200
    note = server.task_manager.tasks["cn1"]["confirmation_note"]
    assert note
    assert not any(ch.isascii() and ch.isalpha() for ch in note.replace("（", "").replace("）", "")), note
    # 原始证据仍在
    assert "Traceback" in server.task_manager.tasks["cn1"]["verify"]["reason"]


def test_ui_verify_reason_mapping_unit():
    f = server._ui_verify_reason
    assert f("") == ""
    assert f("no_duration") == "无法读取视频时长"
    assert f("container_unreadable") == "文件无法正常读取，可能已损坏"
    assert f("duration_too_short (0.30s)") == "视频时长异常过短"
    assert f("ffprobe_error: boom") == "文件检查过程出现异常"
    assert f("totally_unknown") == "文件检查未通过"
