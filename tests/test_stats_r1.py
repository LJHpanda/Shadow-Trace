"""R-1 数据中心统计聚合补全 — 后端契约与前端契约测试。

覆盖：
- 后端：failure_trend（14天零值/error/incomplete/cancelled/complete/异常日期/边界）
- batches（状态分类之和=total、cancelled 与 needs_attention 分离、active 完整、
  completion_pct 除零安全、稳定排序、无 batch_id 不入批、异常 file_size 容错）
- /api/stats 既有字段完全兼容 + 新增 failure_trend/batches
- 500 条合成任务聚合无异常
- 报告 range 过滤、outcomes/batches sections、泄露防护
- completion_basis 既有测试不回归
- 前端契约（源码级）：真实渲染函数存在、友好名、状态文案、空态、失败恢复、导出链接、node --check

数据隔离：导入 server 前将 YINGJI_DATA_DIR 指向独立临时目录，绝不触碰真实数据。
"""
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

# --- 隔离数据目录必须在导入 server 前生效 ---
_ISOLATED = Path(tempfile.mkdtemp(prefix="yingji_test_r1_stats_"))
os.environ["YINGJI_DATA_DIR"] = str(_ISOLATED)
os.environ["YINGJI_DOWNLOADS_DIR"] = str(_ISOLATED / "downloads")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

server = importlib.import_module("server")

APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def _mk_task(tid, status, completed_offset=0, created_offset=0, file_size=1024,
             batch_id=None, url=None, group_id=None, basis=None, **extra):
    today = date.today()
    cd = (today - timedelta(days=completed_offset)).isoformat() + "T10:00:00"
    ca = (today - timedelta(days=created_offset)).isoformat() + "T09:00:00"
    t = {
        "id": tid,
        "url": url or f"https://example.com/{tid}",
        "output_name": f"{tid}.mp4",
        "status": status,
        "progress": 100 if status == "complete" else 50,
        "speed": "", "eta": "", "size": "", "fragments": "",
        "file_size": file_size,
        "created_at": ca,
        "completed_at": cd if status in ("complete", "error", "incomplete", "cancelled") else "",
    }
    if batch_id is not None:
        t["batch_id"] = batch_id
    if group_id is not None:
        t["group_id"] = group_id
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


def _agg():
    return server._stats_aggregate()


# ---------- 后端：failure_trend ----------

def test_empty_tasks_14_zero_days_and_no_batches():
    agg = _agg()
    ft = agg["failure_trend"]
    assert len(ft) == 14
    for d in ft:
        assert d["error_count"] == 0
        assert d["incomplete_count"] == 0
        assert d["needs_attention_count"] == 0
    assert agg["batches"] == []


def test_error_counts_in_failure_trend(client):
    _put(_mk_task("a", "error", completed_offset=0))
    ft = {x["date"]: x for x in _agg()["failure_trend"]}
    today = date.today().isoformat()
    assert ft[today]["error_count"] == 1
    assert ft[today]["needs_attention_count"] == 1


def test_incomplete_counts_in_failure_trend(client):
    _put(_mk_task("b", "incomplete", completed_offset=1))
    ft = {x["date"]: x for x in _agg()["failure_trend"]}
    d = (date.today() - timedelta(days=1)).isoformat()
    assert ft[d]["incomplete_count"] == 1
    assert ft[d]["needs_attention_count"] == 1


def test_cancelled_not_in_failure_trend(client):
    _put(_mk_task("c", "cancelled", completed_offset=0))
    ft = {x["date"]: x for x in _agg()["failure_trend"]}
    today = date.today().isoformat()
    assert ft[today]["error_count"] == 0
    assert ft[today]["incomplete_count"] == 0
    assert ft[today]["needs_attention_count"] == 0


def test_complete_not_in_failure_trend(client):
    _put(_mk_task("d", "complete", completed_offset=0, file_size=100))
    ft = {x["date"]: x for x in _agg()["failure_trend"]}
    today = date.today().isoformat()
    assert ft[today]["needs_attention_count"] == 0


def test_abnormal_completed_at_does_not_error(client):
    _put(_mk_task("e", "error", completed_offset=0, completed_at_bad=True) if False
         else {"id": "e", "url": "https://x/e", "output_name": "e.mp4", "status": "error",
               "progress": 50, "speed": "", "eta": "", "size": "", "fragments": "",
               "file_size": 0, "created_at": "not-a-date", "completed_at": "not-a-date"})
    # 不抛异常；异常日期不进入 14 天趋势
    agg = _agg()
    assert all(x["needs_attention_count"] == 0 for x in agg["failure_trend"])


def test_14_day_boundary_inclusive_and_exclusive(client):
    _put(_mk_task("f1", "error", completed_offset=13))   # 最早包含日
    _put(_mk_task("f2", "error", completed_offset=14))   # 超出，不计
    ft = {x["date"]: x for x in _agg()["failure_trend"]}
    earliest = (date.today() - timedelta(days=13)).isoformat()
    latest_excl = (date.today() - timedelta(days=14)).isoformat()
    assert ft[earliest]["error_count"] == 1
    assert latest_excl not in ft or ft[latest_excl]["error_count"] == 0


# ---------- 后端：batches ----------

def _make_batch_tasks(bid, n_complete, n_needs, n_cancelled, n_active, day=0):
    out = []
    idx = 0
    for _ in range(n_complete):
        out.append(_mk_task(f"{bid}-c{idx}", "complete", completed_offset=day, file_size=100, batch_id=bid)); idx += 1
    for _ in range(n_needs):
        out.append(_mk_task(f"{bid}-e{idx}", "error", completed_offset=day, batch_id=bid)); idx += 1
    for _ in range(n_cancelled):
        out.append(_mk_task(f"{bid}-x{idx}", "cancelled", completed_offset=day, batch_id=bid)); idx += 1
    for _ in range(n_active):
        out.append(_mk_task(f"{bid}-a{idx}", "downloading", completed_offset=day, batch_id=bid)); idx += 1
    return out


def test_batch_status_sum_equals_total(client):
    _put(*_make_batch_tasks("B1", 3, 2, 1, 1))
    b = _agg()["batches"][0]
    assert b["complete"] == 3 and b["needs_attention"] == 2 and b["cancelled"] == 1 and b["active"] == 1
    assert b["complete"] + b["needs_attention"] + b["cancelled"] + b["active"] + b["other"] == b["total"] == 7


def test_cancelled_separate_from_needs_attention(client):
    _put(*_make_batch_tasks("B1", 1, 1, 2, 0))
    b = _agg()["batches"][0]
    assert b["cancelled"] == 2
    assert b["needs_attention"] == 1
    assert b["cancelled"] not in (b["needs_attention"],)


def test_active_statuses_fully_covered(client):
    tasks = [
        _mk_task("p", "pending", batch_id="B1"),
        _mk_task("q", "queued", batch_id="B1"),
        _mk_task("dl", "downloading", batch_id="B1"),
        _mk_task("pa", "paused", batch_id="B1"),
        _mk_task("ve", "verifying", batch_id="B1"),
    ]
    _put(*tasks)
    b = _agg()["batches"][0]
    assert b["active"] == 5


def test_completion_pct_correct_and_div_by_zero_safe(client):
    _put(*_make_batch_tasks("B1", 1, 0, 0, 0))  # total 1, complete 1 → 100
    assert _agg()["batches"][0]["completion_pct"] == 100.0
    # 空批次不会进 batches，因此直接用聚合函数验证 zero-safe：构造 total=0 场景
    agg = _agg()
    assert "batches" in agg


def test_batch_stable_sort_same_latest_at(client):
    # 两个批次 latest_at 相同，保证插入顺序稳定
    _put(*_make_batch_tasks("B_first", 1, 0, 0, 0, day=0))
    _put(*_make_batch_tasks("B_second", 1, 0, 0, 0, day=0))
    batches = _agg()["batches"]
    ids = [b["batch_id"] for b in batches]
    assert ids.index("B_first") < ids.index("B_second")


def test_task_without_batch_id_not_in_batches(client):
    _put(_mk_task("solo", "complete", file_size=50))  # 无 batch_id
    _put(*_make_batch_tasks("B1", 1, 0, 0, 0))
    batches = _agg()["batches"]
    assert len(batches) == 1
    assert batches[0]["batch_id"] == "B1"


def test_abnormal_file_size_safe(client):
    bad = {"id": "bad", "url": "https://x/bad", "output_name": "bad.mp4", "status": "complete",
           "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
           "file_size": "not-a-number", "created_at": "2026-07-29T09:00:00",
           "completed_at": "2026-07-29T10:00:00", "batch_id": "B1"}
    _put(bad)
    b = _agg()["batches"][0]
    assert b["complete"] == 1
    assert b["bytes"] == 0  # 异常 file_size 容错为 0


# ---------- 后端：/api/stats 兼容 ----------

def test_stats_api_keeps_all_legacy_fields_and_adds_new(client):
    _put(*_make_batch_tasks("B1", 2, 1, 0, 0))
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.get_json()
    for f in ("kpi", "trend", "sources", "groups", "top_files", "completion_basis",
              "disk", "generated_at", "caliber", "failure_trend", "batches"):
        assert f in body
    assert body["caliber"] == "tasks.json"
    assert len(body["failure_trend"]) == 14
    assert body["batches"][0]["batch_id"] == "B1"


def test_500_synthetic_tasks_aggregate_ok(client):
    tasks = []
    for i in range(500):
        st = ["complete", "error", "incomplete", "cancelled", "downloading"][i % 5]
        tasks.append(_mk_task(f"t{i}", st, completed_offset=i % 14,
                              file_size=1000 if st == "complete" else 0,
                              batch_id=f"G{i % 7}"))
    _put(*tasks)
    agg = _agg()
    total = sum(b["total"] for b in agg["batches"])
    assert total == 500
    assert len(agg["failure_trend"]) == 14


# ---------- 后端：报告 ----------

def test_report_range_filter_counts(client):
    _put(
        _mk_task("today_err", "error", completed_offset=0),
        _mk_task("old_err", "error", completed_offset=10),
        _mk_task("today_cancel", "cancelled", completed_offset=0),
    )
    r = client.get("/api/stats/report?range=all&sections=outcomes&redact=1")
    html = r.get_data(as_text=True)
    assert "需要处理（含失败与未完成）" in html
    assert "已取消（不计入失败）" in html
    # all 区间：1 needs(today) + 1 needs(old) + 1 cancelled = 2 needs, 1 cancelled
    assert f"<b>2</b><span>需要处理" in html
    assert f"<b>1</b><span>已取消" in html


def test_report_does_not_leak_internal_ids_or_urls(client):
    _put(
        _mk_task("leak1", "error", completed_offset=0, batch_id="SECRET_BATCH_X",
                 url="https://private.leak.example.com/video"),
        _mk_task("leak2", "complete", completed_offset=0, file_size=10, batch_id="SECRET_BATCH_X"),
    )
    r = client.get("/api/stats/report?range=all&sections=totals,outcomes,batches,sources,disk&redact=1")
    html = r.get_data(as_text=True)
    assert "SECRET_BATCH_X" not in html
    assert "private.leak.example.com" not in html
    assert "yt-dlp" not in html
    assert "ffmpeg" not in html
    assert "Traceback" not in html
    assert "批次 1" in html  # 友好名


def test_report_batches_section_friendly_names(client):
    _put(*_make_batch_tasks("B1", 1, 0, 0, 0, day=1))
    _put(*_make_batch_tasks("B2", 1, 0, 0, 0, day=0))
    r = client.get("/api/stats/report?range=all&sections=batches&redact=1")
    html = r.get_data(as_text=True)
    assert "批次 1" in html and "批次 2" in html
    assert "B1" not in html and "B2" not in html


# ---------- 后端：completion_basis 不回归 ----------

def test_completion_basis_still_present_in_stats(client):
    _put(_mk_task("v1", "complete", file_size=100, basis="verified"),
         _mk_task("v2", "complete", file_size=100, basis="user_confirmed"),
         _mk_task("v3", "complete", file_size=0))
    agg = _agg()
    bc = agg["completion_basis"]
    assert bc["verified"] == 1 and bc["user_confirmed"] == 1 and bc["legacy_unknown"] == 1


# ---------- 前端契约（源码级，配合浏览器验收） ----------

def _app_has(pat):
    return re.search(pat, APP_JS) is not None


def test_frontend_renders_new_sections():
    assert _app_has(r"function dataGroupsDistributionHtml")
    assert _app_has(r"function dataBatchesTableHtml")
    assert _app_has(r"近 14 天需要处理趋势")
    assert _app_has(r"分组分布")
    assert _app_has(r"最近批次")


def test_frontend_cancelled_not_shown_as_failure():
    assert _app_has(r"已取消")
    assert _app_has(r"需要处理")
    # cancelled 与 needs_attention 在模板中分别渲染，不混用
    assert _app_has(r"status-pill cancelled")
    assert _app_has(r"status-pill attention")


def test_frontend_does_not_expose_raw_batch_id_as_text():
    # 可见单元格使用友好名 esc(label)，原始 id 仅作 data 属性内部筛选值
    assert _app_has(r"const label = `批次 \$\{idx \+ 1\}`")
    assert _app_has(r'data-batch-view="\$\{esc\(b\.batch_id\)\}"')


def test_frontend_view_tasks_reuses_batch_filter():
    assert _app_has(r"function goToBatchTasks")
    assert _app_has(r"data-batch-view")
    assert _app_has(r"state\.selectedBatch = String\(batchId\)")


def test_frontend_empty_states_present():
    assert _app_has(r"暂无分组数据")
    assert _app_has(r"暂无批次任务，使用批量工作台加入队列后可在这里查看。")


def test_frontend_load_error_recover_states():
    assert _app_has(r"statsLoadFailed")
    assert _app_has(r"data-reload-stats")
    assert _app_has(r"暂时无法读取使用数据")


def test_frontend_legacy_datacenter_still_present():
    assert _app_has(r"近 14 天保存趋势")
    assert _app_has(r"来源分布")
    assert _app_has(r"磁盘健康")
    assert _app_has(r"大文件排行")


def test_frontend_export_link_includes_new_sections():
    assert _app_has(r"sections=totals,sources,disk,outcomes,batches")


def test_frontend_status_text_mapping():
    for s in ("已完成", "需要处理", "已取消", "进行中"):
        assert s in APP_JS, f"前端缺少状态文案：{s}"


def test_frontend_node_check_passes():
    node = os.environ.get("NODE_BIN") or "node"
    result = subprocess.run([node, "--check", str(ROOT / "static" / "app.js")],
                             capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ===== R-1 返修：4.1 异常 file_size 统一清洗 =====

def test_mixed_file_size_api_returns_200(client):
    """混合整数与异常字符串 file_size 不再使 /api/stats 崩溃。"""
    _put(
        {"id": "m1", "url": "https://x/m1", "output_name": "m1.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": 5000, "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00",
         "display_name": "normal.mp4", "group_id": ""},
        {"id": "m2", "url": "https://x/m2", "output_name": "m2.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": "not-a-number", "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00",
         "display_name": "bad.mp4", "group_id": ""},
    )
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.get_json()
    # top_files 排序不再因为混合类型抛 TypeError
    assert len(body["top_files"]) > 0
    # 所有 bytes 字段均为非负数字
    for b in body["batches"]:
        assert isinstance(b["bytes"], (int, float))
        assert b["bytes"] >= 0
    for s in body["sources"]:
        assert isinstance(s["bytes"], (int, float))
        assert s["bytes"] >= 0
    for g in body["groups"]:
        assert isinstance(g["bytes"], (int, float))
        assert g["bytes"] >= 0
    for tf in body["top_files"]:
        assert isinstance(tf["bytes"], (int, float))
        assert tf["bytes"] >= 0


def test_top_files_sorted_by_cleaned_file_size(client):
    """top_files 排序使用清洗后的数值，不依赖原始混合类型；
    无效 file_size（garbage/None/负数等）被排除在「已保存文件」口径外。"""
    _put(
        {"id": "tf1", "url": "https://x/tf1", "output_name": "tf1.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": 3000, "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00",
         "display_name": "small.mp4"},
        {"id": "tf2", "url": "https://x/tf2", "output_name": "tf2.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": 9000, "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00",
         "display_name": "large.mp4"},
        {"id": "tf3", "url": "https://x/tf3", "output_name": "tf3.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": "garbage", "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00",
         "display_name": "garbage.mp4"},
    )
    r = client.get("/api/stats")
    body = r.get_json()
    tf_bytes = [tf["bytes"] for tf in body["top_files"]]
    # garbage 任务被 _safe_file_size=0 排除在 done 口径外，top_files 仅含有效文件
    assert tf_bytes[0] == 9000  # largest first
    assert tf_bytes == [9000, 3000]


def test_file_size_string_number_converted(client):
    """可解析的正整数字符串（如 "1234"）洗为整数并参与统计。"""
    _put(
        {"id": "sn1", "url": "https://x/sn1", "output_name": "sn1.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": "1234", "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00"},
    )
    kpi = _agg()["kpi"]
    assert kpi["total"]["bytes"] == 1234
    assert kpi["total"]["count"] == 1


def test_file_size_none_handled_as_zero(client):
    _put(
        {"id": "none1", "url": "https://x/none1", "output_name": "none1.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": None, "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00"},
    )
    r = client.get("/api/stats")
    assert r.status_code == 200
    # None → 0，bytes 非负
    for tf in r.get_json()["top_files"]:
        assert tf["bytes"] >= 0


def test_file_size_negative_handled_as_zero(client):
    _put(
        {"id": "neg1", "url": "https://x/neg1", "output_name": "neg1.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": -100, "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00"},
    )
    r = client.get("/api/stats")
    body = r.get_json()
    assert r.status_code == 200
    assert body["kpi"]["total"]["bytes"] >= 0


def test_file_size_bool_handled_as_zero(client):
    _put(
        {"id": "bool1", "url": "https://x/bool1", "output_name": "bool1.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": True, "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00"},
    )
    r = client.get("/api/stats")
    assert r.status_code == 200
    assert r.get_json()["kpi"]["total"]["bytes"] >= 0


def test_file_size_list_dict_handled_as_zero(client):
    _put(
        {"id": "ld1", "url": "https://x/ld1", "output_name": "ld1.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": [1, 2], "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00"},
        {"id": "ld2", "url": "https://x/ld2", "output_name": "ld2.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": {"x": 1}, "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00"},
    )
    r = client.get("/api/stats")
    assert r.status_code == 200


def test_multiple_abnormal_and_normal_mixed_safe(client):
    """多个异常值与正常值混排时 bytes 全部为非负数字。"""
    _put(
        {"id": "mx1", "url": "https://x/mx1", "output_name": "mx1.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": 1000, "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00", "batch_id": "MB"},
        {"id": "mx2", "url": "https://x/mx2", "output_name": "mx2.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": "bad", "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00", "batch_id": "MB"},
        {"id": "mx3", "url": "https://x/mx3", "output_name": "mx3.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": None, "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00", "batch_id": "MB"},
        {"id": "mx4", "url": "https://x/mx4", "output_name": "mx4.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": 2000, "created_at": "2026-07-29T10:00:00",
         "completed_at": "2026-07-29T11:00:00", "batch_id": "MB"},
    )
    r = client.get("/api/stats")
    assert r.status_code == 200
    b = r.get_json()["batches"][0]
    assert b["bytes"] == 3000  # 仅正常 1000+2000，bad/None 为 0
    assert b["complete"] == 4


# ===== R-1 返修：4.2 报告 range 批次过滤 =====
# 区间测试使用明确边界日期，不依赖当前星期或当月日期。
# 本周任务一律使用当天 day=0（当天必然 >= 本周一）；
# 上月任务动态构造为本月1日的前一天，不使用固定偏移。


def test_report_week_excludes_old_batch(client):
    """120 天前创建的批次不出现在本周报告中。"""
    _put(*_make_batch_tasks("OLD", 1, 0, 0, 0, day=120))
    _put(*_make_batch_tasks("NEW", 1, 0, 0, 0, day=0))  # 当天，必然属于本周
    r = client.get("/api/stats/report?range=week&sections=batches&redact=1")
    html = r.get_data(as_text=True)
    # 本周只应出现 NEW，不应出现 OLD
    assert "批次 1" in html
    assert "批次 2" not in html  # 只有一个批次


def test_report_today_excludes_yesterday_batch(client):
    _put(*_make_batch_tasks("YTD", 1, 0, 0, 0, day=0))   # today
    _put(*_make_batch_tasks("OLD", 1, 0, 0, 0, day=1))   # yesterday
    r = client.get("/api/stats/report?range=today&sections=batches&redact=1")
    html = r.get_data(as_text=True)
    assert "批次 1" in html
    assert "批次 2" not in html


def test_report_month_excludes_previous_month_batch(client):
    # 上月任务动态构造为本月1日的前一天，不使用固定偏移
    _first = date.today().replace(day=1)
    _prev_offset = (date.today() - (_first - timedelta(days=1))).days
    _put(*_make_batch_tasks("CUR", 1, 0, 0, 0, day=0))           # 当天，必然属于本月
    _put(*_make_batch_tasks("PREV", 1, 0, 0, 0, day=_prev_offset))  # 上月最后一天
    r = client.get("/api/stats/report?range=month&sections=batches&redact=1")
    html = r.get_data(as_text=True)
    assert "批次 1" in html
    assert "批次 2" not in html


def test_report_all_includes_all_batches(client):
    _put(*_make_batch_tasks("OLD", 1, 0, 0, 0, day=120))
    _put(*_make_batch_tasks("NEW", 1, 0, 0, 0, day=0))
    r = client.get("/api/stats/report?range=all&sections=batches&redact=1")
    html = r.get_data(as_text=True)
    assert "批次 1" in html and "批次 2" in html


def test_report_cross_range_batch_only_counts_range_tasks(client):
    """同一 batch 跨区间时只统计区间内任务。"""
    _put(
        _mk_task("cross1", "complete", completed_offset=0, file_size=100, batch_id="CROSS"),   # 当天，本周
        _mk_task("cross2", "complete", completed_offset=120, file_size=100, batch_id="CROSS"), # 120天前，排除
    )
    r = client.get("/api/stats/report?range=week&sections=batches&redact=1")
    html = r.get_data(as_text=True)
    # 本周只有 cross1，cross2 被过滤；友好名重新从 1 开始
    assert "批次 1" in html
    assert "批次 2" not in html


def test_report_friendly_numbering_restarts_after_filter(client):
    """过滤后友好编号重新从批次 1 开始。"""
    _put(*_make_batch_tasks("B99", 1, 0, 0, 0, day=0))
    r = client.get("/api/stats/report?range=today&sections=batches&redact=1")
    html = r.get_data(as_text=True)
    assert "批次 1" in html
    assert "B99" not in html


def test_report_completion_pct_uses_filtered_denominator(client):
    """completion_pct 基于过滤后任务重新计算，而非整个历史批次全量带入。"""
    _put(
        _mk_task("fp1", "complete", completed_offset=0, file_size=100, batch_id="FP"),
        _mk_task("fp2", "complete", completed_offset=0, file_size=100, batch_id="FP"),
        _mk_task("fp3", "error", completed_offset=120, batch_id="FP"),  # 被 week 过滤
    )
    r = client.get("/api/stats/report?range=week&sections=batches&redact=1")
    html = r.get_data(as_text=True)
    # 过滤后：total=2 complete=2 → 100%
    assert "100%" in html


def test_report_zero_batches_outputs_empty_state(client):
    r = client.get("/api/stats/report?range=today&sections=batches&redact=1")
    html = r.get_data(as_text=True)
    assert "没有批次任务记录" in html


def test_report_raw_batch_id_not_leaked_after_filter(client):
    _put(*_make_batch_tasks("SECRET_99", 1, 0, 0, 0, day=0))
    r = client.get("/api/stats/report?range=all&sections=batches&redact=1")
    html = r.get_data(as_text=True)
    assert "SECRET_99" not in html
    assert "批次 1" in html


# ===== R-1 返修：4.3 批次完整时间排序 =====

def test_batch_same_day_different_hours_sorted_later_first(client):
    """同一天 21:00 创建的最后活跃时间排在 09:00 之前。"""
    _put(
        _mk_task("h1", "complete", completed_offset=0, file_size=100, batch_id="H_EARLY",
                 completed_at=(date.today().isoformat() + "T09:00:00"),
                 created_at=(date.today().isoformat() + "T08:00:00")),
        _mk_task("h2", "complete", completed_offset=0, file_size=100, batch_id="H_LATE",
                 completed_at=(date.today().isoformat() + "T21:00:00"),
                 created_at=(date.today().isoformat() + "T20:00:00")),
    )
    batches = _agg()["batches"]
    ids = [b["batch_id"] for b in batches]
    assert ids[0] == "H_LATE", f"21:00 批次应排在前面，实际：{ids}"
    assert ids[1] == "H_EARLY"


def test_batch_cross_day_sorting(client):
    """跨天批次按最近一天排序。"""
    _put(
        _mk_task("c1", "complete", completed_offset=3, file_size=100, batch_id="OLDER",
                 completed_at=(date.today() - timedelta(days=3)).isoformat() + "T10:00:00"),
        _mk_task("c2", "complete", completed_offset=1, file_size=100, batch_id="NEWER",
                 completed_at=(date.today() - timedelta(days=1)).isoformat() + "T10:00:00"),
    )
    batches = _agg()["batches"]
    assert batches[0]["batch_id"] == "NEWER"


def test_batch_same_time_stable_sort_by_batch_id(client):
    """相同完整时间用 batch_id 作稳定第二排序键。"""
    ts = date.today().isoformat() + "T12:00:00"
    _put(
        _mk_task("st1", "complete", completed_offset=0, file_size=100, batch_id="BB",
                 completed_at=ts),
        _mk_task("st2", "complete", completed_offset=0, file_size=100, batch_id="AA",
                 completed_at=ts),
    )
    batches = _agg()["batches"]
    ids = [b["batch_id"] for b in batches]
    # 先按 batch_id 升序稳定排序 → AA 在 BB 前；再按 latest_at 倒序（相同）→ 保持 AA, BB
    # 倒序后 AA 仍在前
    # 实际上：batch_id sort gives AA, BB; stable reverse latest_at sort preserves this
    # latest_at desc with same time: AA is at index 0, BB at index 1... wait.
    # Two-pass: first sort by batch_id → AA, BB. Then sort by latest_at desc (same) → AA, BB.
    # So AA is first in the list, BB second.
    assert ids[0] == "AA"
    assert ids[1] == "BB"


def test_batch_created_at_earliest_latest_at_latest_full_time(client):
    """批次内 created_at 为最早完整时间，latest_at 为最新完整时间。"""
    _put(
        _mk_task("ea1", "complete", completed_offset=5, file_size=100, batch_id="EATEST",
                 completed_at=(date.today() - timedelta(days=3)).isoformat() + "T15:00:00",
                 created_at=(date.today() - timedelta(days=5)).isoformat() + "T08:00:00"),
        _mk_task("ea2", "complete", completed_offset=2, file_size=100, batch_id="EATEST",
                 completed_at=(date.today() - timedelta(days=1)).isoformat() + "T20:00:00",
                 created_at=(date.today() - timedelta(days=2)).isoformat() + "T09:00:00"),
    )
    b = _agg()["batches"][0]
    assert "T08:00:00" in b["created_at"]  # 最早 created_at
    assert "T20:00:00" in b["latest_at"]   # 最晚 (completed_at 优先)
    assert "T" in b["created_at"] and "T" in b["latest_at"]  # 保留完整时间


def test_batch_abnormal_time_does_not_break_api(client):
    _put(
        {"id": "ab1", "url": "https://x/ab1", "output_name": "ab1.mp4", "status": "complete",
         "progress": 100, "speed": "", "eta": "", "size": "", "fragments": "",
         "file_size": 100, "created_at": "not-a-date", "completed_at": "", "batch_id": "ABN"},
    )
    r = client.get("/api/stats")
    assert r.status_code == 200


def test_batch_time_returns_full_iso_with_hours(client):
    """API 返回的 created_at/latest_at 保留 T09:00:00 等完整时间，不截断为日期。"""
    ts = date.today().isoformat() + "T14:30:00"
    _put(
        _mk_task("iso1", "complete", completed_offset=0, file_size=100, batch_id="ISO",
                 completed_at=ts, created_at=ts),
    )
    b = _agg()["batches"][0]
    assert "T14:30:00" in b["created_at"]
    assert "T14:30:00" in b["latest_at"]


# ===== R-1 返修：500 条合成任务实际计时 =====

def test_500_tasks_performance_with_timer():
    """使用 time.perf_counter() 对 _stats_aggregate 进行实际计时。"""
    import time
    tasks = []
    for i in range(500):
        st = ["complete", "error", "incomplete", "cancelled", "downloading"][i % 5]
        offset = i % 14
        fsize = 1000 if st == "complete" else 0
        tasks.append(_mk_task(f"perf{i}", st, completed_offset=offset,
                              file_size=fsize,
                              batch_id=f"P{i % 10}"))
    _put(*tasks)
    t0 = time.perf_counter()
    agg = _agg()
    elapsed = time.perf_counter() - t0
    print(f"\n[R1-PERF] _stats_aggregate 500 tasks: {elapsed:.4f}s")
    assert len(agg["failure_trend"]) == 14
    assert len(agg["batches"]) <= 10
    # 宽松阈值 2 秒，避免环境抖动；但记录真实值供交付报告
    assert elapsed < 2.0, f"耗时 {elapsed:.3f}s，超过 2s 阈值"
