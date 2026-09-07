"""UI-P0-03 完成状态真实性 — 主前端静态契约检查（10 项）。

只做源码字符串级契约校验，不启动浏览器。
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "static"
JS = (MAIN / "app.js").read_text(encoding="utf-8")
CSS = (MAIN / "app.css").read_text(encoding="utf-8")


def test_1_verifying_status_label_present():
    assert 'verifying: "正在检查文件"' in JS


def test_2_confirm_button_only_for_incomplete():
    # 修复旧 bug：确认按钮曾渲染在所有非活动状态上，后端只接受 incomplete
    assert 'task.status === "incomplete") add("confirm"' in JS
    assert '"queued"].includes(task.status)) add("confirm"' not in JS


def test_3_basis_badge_wired_into_row_and_card():
    assert "function basisBadgeHtml(task)" in JS
    assert JS.count("basisBadgeHtml(task)") >= 3  # taskRow + taskCard + 详情


def test_4_basis_labels_cover_three_kinds():
    for label in ("检查通过", "人工确认", "历史记录"):
        assert label in JS


def test_5_css_has_three_basis_badge_styles():
    for cls in ("basis-verified", "basis-user_confirmed", "basis-legacy_unknown"):
        assert f".basis-badge.{cls}" in CSS


def test_6_css_has_verifying_and_light_theme_variants():
    assert ".status.verifying" in CSS
    assert 'html[data-theme="light"] .status.verifying' in CSS
    assert 'html[data-theme="light"] .basis-badge.basis-verified' in CSS


def test_7_detail_has_file_check_section():
    assert "文件检查" in JS
    assert "td-check" in JS
    assert ".td-check" in CSS


def test_8_confirm_modal_uses_accurate_wording():
    assert "确认文件可用" in JS
    assert "人工确认保留" in JS
    assert "标记为完成" not in JS  # 不再暗示等同于系统检查通过


def test_9_no_native_confirm_dialog():
    assert "window.confirm" not in JS
    assert "window.alert" not in JS


def test_10_confirm_modal_supports_escape_and_focus():
    # chooseConfirmResult 弹层：Escape 关闭 + 默认焦点落在取消按钮
    assert '"Escape" && finish("cancel")' in JS
    assert "data-confirm-result=\"cancel\"]', root).focus()" in JS


# ---------- 完成态操作回归 ----------

def test_11_verifying_has_no_destructive_actions():
    # verifying 状态在加入「移动到分组」后立即返回，
    # 绝不落入删除/取消分支，也不触发兜底删除
    assert 'if (task.status === "verifying") return buttons.join("")' in JS
    guard = JS.index('if (task.status === "verifying") return buttons.join("")')
    first_delete = JS.index('add("delete"')
    assert guard < first_delete, "verifying 守卫必须位于所有 delete 按钮分支之前"


def test_12_user_confirmed_tooltip_exact_wording():
    assert "自动检查未通过或未完成，由你人工确认保留。" in JS
    assert "由你人工确认保留，未经过自动检查" not in JS


def test_13_ui_reason_contains_no_internal_codes():
    # UI 渲染只使用后端语言化后的 verify_reason 字段，
    # 前端源码不得出现任何内部原因代码或原始 verify.reason 访问
    assert "task.verify_reason" in JS
    assert "verify.reason" not in JS
    for internal in ("no_duration", "ffprobe", "unreadable", "container_",
                     "duration_too_short", "bitrate_too_low", "no_media_streams"):
        assert internal not in JS, f"前端源码不应包含内部代码 {internal}"
