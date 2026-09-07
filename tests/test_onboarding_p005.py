"""UI-P0-05 交付契约测试：首次启动引导 + 能力异常恢复 + 社区版品牌。

约束：
- 仅校验前端源码（static/，不含 legacy/）与 server.py 新增只读接口；
- 不依赖浏览器、不读写真实任务/Cookie/目录；
- 品牌、引导、异常卡片、能力接口均通过字符串/结构契约断言。
"""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "static"
HTML = (MAIN / "index.html").read_text(encoding="utf-8")
JS = (MAIN / "app.js").read_text(encoding="utf-8")
CSS = (MAIN / "app.css").read_text(encoding="utf-8")
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")


# ---------- 1. 品牌：GPL 社区版 ----------
def test_brand_shows_community_edition_in_titlebar_and_sidebar():
    assert '<em>社区版</em>' in HTML
    assert "GPLv3" in HTML
    assert "<span>开源</span>" in HTML


def test_brand_no_longer_shows_pro_or_personal_pro():
    assert "专业版" not in HTML
    assert "个人专业版" not in HTML
    assert "PRO" not in HTML
    assert "个人专业版" not in JS


def test_community_brand_has_no_commercial_activation_entry():
    assert "订单授权码" not in JS
    assert "激活本机授权" not in JS
    assert "/license/activate" not in JS


# ---------- 2. 首次启动三步引导 ----------
def test_onboarding_has_three_named_steps():
    for title in ("认识影迹", "检查使用条件", "开始使用"):
        assert title in JS


def test_onboarding_modal_is_accessible_dialog():
    assert 'role="dialog"' in JS
    assert "aria-modal" in JS
    # 引导弹层使用 onb-modal 容器并带 aria-labelledby
    assert "onb-modal" in JS
    assert "aria-labelledby" in JS


def test_onboarding_uses_versioned_localstorage_marker():
    # 版本化标记，刷新不重复；关闭时写入标记
    assert "yingji_onboarding_v1" in JS
    assert "localStorage.setItem(ONBOARDING_KEY" in JS
    assert "shouldShowOnboarding" in JS


def test_onboarding_provides_skip_and_replay_entries():
    # 「稍后再看」跳过选项
    assert "稍后再看" in JS
    # 设置中「重新查看新手引导」入口（force 重新打开）
    assert "重新查看" in JS
    assert "openOnboarding(true, replayBtn)" in JS
    assert 'id="replayOnboarding"' in JS


def test_onboarding_step2_reads_capability_not_tasks():
    # 引导第 2 步依赖能力检查接口，不读取任务数据
    assert 'api("/startup-status")' in JS
    # startup-status 调用不应出现在读取任务的位置
    assert "/tasks" not in JS[JS.index("onboardingStep2Body"):JS.index("onboardingStep3Body")]


def test_onboarding_focus_trap_and_escape_are_wired():
    assert "trapFocus" in JS
    assert "Escape" in JS
    assert "closeOnboarding" in JS


def test_onboarding_respects_reduced_motion():
    assert "prefers-reduced-motion" in CSS
    assert "animation: none" in CSS or "animation:none" in CSS


# ---------- 3. 能力检查与异常恢复卡片 ----------
def test_capability_check_uses_readonly_startup_status():
    assert "@app.route(\"/api/startup-status\")" in SERVER
    assert "download_dir_writable" in SERVER
    assert "_check_dir_writable" in SERVER


def test_startup_status_returns_cookie_and_writable_fields():
    # 接口字段齐备：组件、凭据、默认目录可写
    assert "cookies_loaded" in SERVER
    assert "ffmpeg" in SERVER
    assert "yt_dlp" in SERVER
    # JS 端映射使用这些字段
    assert "download_dir_writable" in JS
    assert "cookies_loaded" in JS


def test_dir_probe_is_isolated_safe_and_non_creating():
    # 探针使用固定名 .yingji_write_probe.tmp + O_TRUNC 覆盖 + finally 清理，
    # 且不自动创建目标目录（沙箱 unlink 受限时仅残留至多一个固定探针文件）
    helper = SERVER[SERVER.index("def _probe_download_dir"):SERVER.index("@app.route(\"/api/startup-status\")")]
    assert "yingji_write_probe" in helper
    assert "O_TRUNC" in helper
    assert "finally" in helper
    assert ".unlink()" in helper
    assert "mkdir" not in helper  # 状态检查不得改变文件系统结构


def test_anomaly_cards_render_for_non_ok_capabilities():
    assert "anomalyCardsHtml" in JS
    assert "anomalyCardHtml" in JS
    assert "anomaly-list" in CSS
    assert "anomaly-card" in CSS


def test_anomaly_cards_carry_stable_error_codes():
    for code in ("E-COMPONENT-MISSING", "E-DIR-READONLY", "W-COOKIE-MISSING"):
        assert code in JS


def test_anomaly_cards_explain_what_happened_and_advice():
    assert "anomaly-what" in CSS
    assert "anomaly-impact" in CSS
    assert "anomaly-advice" in CSS
    # 内容文案：说明「发生了什么」与「建议」
    assert "影响：" in JS
    assert "建议：" in JS


def test_cookie_missing_is_non_blocking_info_card():
    # Cookie 未加载为 info 级提示，不阻塞下载
    assert 'level === "info"' in JS
    assert "W-COOKIE-MISSING" in JS
    # info 卡片提供「暂不处理」可忽略，不阻断使用
    assert "data-anomaly-dismiss" in JS


def test_unwritable_dir_shows_retryable_error_card():
    # 目录不可写时显示可重试的错误卡片
    assert "E-DIR-READONLY" in JS
    assert "data-anomaly-retry" in JS
    # 重试按钮重新触发能力检查
    assert "runCapabilityCheck" in JS


def test_anomaly_and_onboarding_no_internal_tech_stack_leak():
    # 用户可见文案不得泄露内部技术栈 / 堆栈 / JSON / 内部路径
    # 仅抽取「用户可见的中文字符串」（忽略模板表达式与外层的代码标识符）
    segment = JS[JS.index("onboardingModalHtml"):JS.index("// ===== /UI-P0-05 =====")]
    user_strings = re.findall(r'`([^`]*[\u4e00-\u9fff][^`]*)`', segment)
    bad = ("FFmpeg", "yt-dlp", "Traceback", "stack-trace", "flask",
           "Flask", "Exception", "FileNotFoundError", "cookies.txt")
    for s in user_strings:
        # 模板中的 ${...} 表达式不含中文，跳过；只检查中文可见文案
        for word in bad:
            assert word not in s, f"引导/异常文案泄露内部词：{word}（{s[:30]}…）"


def test_existing_ffmpeg_status_call_count_unchanged():
    # 既有设置页只读接口调用次数不受新增 startup-status 影响
    assert JS.count('api("/ffmpeg-status")') == 1


# ---------- 4. R2 返修：状态真实性 ----------
def _seg(start, end, text=None):
    src = text if text is not None else JS
    return src[src.index(start):src.index(end)]


def test_no_unconditional_all_ready_copy_anywhere():
    # 阻断 / 检查中 / 读取失败状态下均不得出现「一切就绪」——源码已彻底移除该文案
    assert "一切就绪" not in JS
    assert "一切就绪" not in HTML


def test_capability_status_has_five_states():
    seg = _seg("function capabilityStatus", "function renderOnboardingModal")
    for token in ('"loading"', '"unavailable"', '"blocking"', '"advisory"', '"ready"'):
        assert token in seg


def test_loading_state_copy_and_cannot_bypass_check():
    assert "正在检查本机使用条件" in JS
    seg = _seg("function onboardingModalHtml", "function onboardingStep1Body")
    # 检查中：主按钮禁用，完成操作不能绕过尚未完成的检查
    assert "disabled>正在检查…" in seg


def test_ready_state_copy_and_start_first_download():
    assert "使用条件已就绪" in JS
    assert "开始第一次下载" in JS


def test_finish_button_only_in_ready_or_advisory_branch():
    seg = _seg("function onboardingModalHtml", "function onboardingStep1Body")
    ready_pos = seg.index('status === "ready"')
    assert "data-onboarding-finish" not in seg[:ready_pos]
    assert "data-onboarding-finish" in seg[ready_pos:]


def test_blocking_state_offers_recheck_and_real_solutions():
    assert "data-onboarding-recheck" in JS
    assert "重新检查" in JS
    assert "查看解决方法" in JS
    # 阻断态明确说明「什么不可用」
    assert "部分使用条件不可用" in JS


def test_unavailable_state_clears_old_results():
    assert "暂时无法读取使用条件" in JS
    seg = _seg("async function runCapabilityCheck", "function mapCapability")
    assert "state.capability = null" in seg  # 检查开始即清空旧成功结果
    assert "error: true" in seg              # 读取失败不保留旧成功结果


def test_finish_is_guarded_and_navigates_to_download_with_focus():
    seg = _seg("function finishOnboarding", "function bindOnboardingButtons")
    assert 'status !== "ready" && status !== "advisory"' in seg
    assert 'state.view = "download"' in seg
    assert "renderView()" in seg
    assert "#mediaUrl" in seg  # 完成后聚焦链接输入框（含设置页重新查看路径）


# ---------- 5. R2 返修：路径隐私 ----------
def test_internal_default_dir_path_never_rendered():
    # 前端不得直接渲染服务端默认目录完整路径（接口字段仅为兼容保留）
    assert not re.search(r"data\.download_dir\b(?!_)", JS)
    assert "默认保存位置可正常使用。" in JS
    assert "默认保存位置当前不可用。" in JS


def test_cookie_guidance_is_neutral_without_fake_help():
    assert "影迹数据目录" not in JS         # 不再指向不确定的数据目录位置
    assert "按帮助说明添加登录凭据" in JS   # 中性、真实口径
    assert "暂未添加登录凭据" in JS
    assert "打开帮助" not in JS             # 无假帮助按钮


# ---------- 6. R2 返修：局部更新与焦点闭环 ----------
def test_anomaly_updates_use_partial_region_not_full_rerender():
    assert 'id="capabilityAnomalyRegion"' in JS
    assert "updateAnomalyRegion" in JS
    seg = _seg("async function runCapabilityCheck", "function mapCapability")
    assert "renderView" not in seg  # 能力检查完成只更新异常区域，不重建下载表单


def test_dismiss_anomaly_does_not_rerender_download_view():
    seg = _seg("const anomalyDismiss", "#replayOnboarding")
    assert "updateAnomalyRegion" in seg
    assert "renderView" not in seg


def test_close_restores_focus_to_trigger_or_main_action():
    seg = _seg("function closeOnboarding", "function finishOnboarding")
    assert "onboardingTrigger" in seg
    assert ".focus()" in seg or "focus(" in seg


def test_recheck_moves_focus_to_latest_status_title():
    assert 'id="onbStatusTitle"' in JS
    seg = _seg("async function runCapabilityCheck", "function mapCapability")
    assert "#onbStatusTitle" in seg


def test_replay_button_recorded_as_trigger():
    assert "openOnboarding(true, replayBtn)" in JS


# ---------- 7. R2 返修：接口新增字段被前端容错使用 ----------
def test_frontend_tolerates_new_probe_fields():
    assert "download_dir_exists" in JS
    assert "download_dir_is_directory" in JS
    # 后端接口输出新增字段
    assert '"download_dir_exists"' in SERVER
    assert '"download_dir_is_directory"' in SERVER
