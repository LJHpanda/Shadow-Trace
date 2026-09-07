# -*- coding: utf-8 -*-
"""UI-P0-04 设置页产品化——前端静态契约测试。

原则校验：六分区固定、真实能力才有控件、版本来自真实接口、
Cookie 只显示安全状态、社区版无激活入口、无技术栈词泄漏。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")


def _settings_segment():
    """截取设置中心实现段（供文案检查用）。"""
    start = JS.index("===== UI-P0-04")
    end = JS.index("function renderView")
    return JS[start:end]


# 1. 六个固定分区
def test_1_six_sections_present():
    for key, label in [
        ("general", "常规"), ("download", "下载"), ("network", "网络与 Cookie"),
        ("update", "更新"), ("privacy", "隐私"), ("about", "关于"),
    ]:
        assert f'["{key}", "{label}"]' in JS
    assert '["license", "授权"]' not in JS


# 2. 已支持设置真实生效并持久化（localStorage）
def test_2_real_settings_persist():
    assert 'id="themeSwitch"' in JS
    assert 'localStorage.setItem("yingji_alt_theme"' in JS
    assert 'id="pollingSwitch"' in JS
    assert 'localStorage.setItem("yingji_alt_polling"' in JS


# 3. 未支持能力没有可交互假控件
def test_3_unavailable_capabilities_have_no_fake_controls():
    seg = _settings_segment()
    # 暂未提供的能力统一用只读状态，而不是开关/输入框
    for row_title in ("开机自动启动", "最小化到托盘", "代理设置", "应用内更新"):
        assert row_title in seg
        # 对应行使用 unavailable() 只读状态
    assert seg.count("unavailable()") >= 4
    # 不存在假控件 id
    for fake_id in ("proxyInput", "proxyHost", "autostartSwitch", "traySwitch", "autoUpdateSwitch", "licenseKey"):
        assert f'id="{fake_id}"' not in JS


# 4. 版本显示来自真实接口，无硬编码版本号
def test_4_version_from_real_api():
    assert 'api("/ffmpeg-status")' in JS
    assert "sys.version" in JS
    assert '"v2.7"' not in JS  # 前端不硬编码版本


# 5. Cookie 只显示安全状态，不输出内容和完整路径
def test_5_cookie_safe_status_only():
    assert "cookies_loaded" in JS
    # Cookie 文案允许在「本机 Data/cookies.txt」这类相对说明中出现文件名，
    # 但不得出现把 cookies.txt 放在绝对系统路径下的写法（泄露真实存储位置），
    # 也不得暴露 cookie_path / cookies_file 字段
    assert not re.search(r"[A-Za-z]:[\\\\/].*cookies\.txt|/Users/.*cookies\.txt|/home/.*cookies\.txt|/root/.*cookies\.txt", JS)
    seg = _settings_segment()
    assert "已加载" in seg and "未加载" in seg
    # 不展示 cookie 路径字段
    assert "cookie_path" not in JS and "cookies_file" not in JS


# 6. 社区版只展示开源许可，不提供订单激活入口
def test_6_community_license_copy():
    seg = _settings_segment()
    assert "开源许可" in seg
    assert "GPLv3" in seg
    for phrase in ("订单授权码", "激活本机授权", "本机设备标识", "原始授权码"):
        assert phrase not in seg


# 7. 更新区不出现虚假「已是最新版」
def test_7_update_no_fake_latest():
    seg = _settings_segment()
    assert "尚未启用应用内更新" in seg
    assert "已是最新版" not in JS
    assert "检查更新" not in seg  # 没有假检查更新按钮


# 8. 设置文案不包含技术栈词
def test_8_no_tech_words_in_settings_copy():
    seg = _settings_segment()
    # 排除合法的代码层引用（属性访问），再检查文案
    cleaned = seg.replace("sys.ffmpeg", "").replace("sys.yt_dlp", "")
    for word in ("ffmpeg", "yt_dlp", "yt-dlp", "Python", "python", "命令行", "堆栈", "m3u8-tool"):
        assert word not in cleaned, f"设置文案泄漏技术词: {word}"


# 9. 明暗主题设置保留 + 浅色主题样式
def test_9_theme_setting_preserved():
    seg = _settings_segment()
    assert "亮色主题" in seg
    assert 'html[data-theme="light"] .setting-status' in CSS


# 10. 任务自动刷新行为不回归
def test_10_polling_not_regressed():
    assert "function togglePolling" in JS
    assert "schedulePolling()" in JS
    seg = _settings_segment()
    assert "自动刷新任务" in seg


# 11. 布局与无障碍：分区导航语义 + 键盘焦点 + 响应式
def test_11_layout_accessibility():
    assert 'aria-label="设置分区"' in JS
    assert "aria-current=" in JS
    assert ".settings-nav-item:focus-visible" in CSS
    assert "@media (max-width: 960px)" in CSS
    assert ".settings-layout { grid-template-columns: 1fr; }" in CSS


# 12. 只读状态具备语义表达（role=status），不依赖颜色
def test_12_status_has_semantic_role():
    assert 'role="status"' in JS
    assert "暂未提供" in _settings_segment()


# 13. 隐私区按代码事实表述，无未来承诺、无清空全部数据按钮
def test_13_privacy_factual_no_dangerous_actions():
    seg = _settings_segment()
    assert "不包含任何使用情况收集或上报功能" in seg
    assert "清空全部" not in JS
    assert "清除所有数据" not in JS


# 14. 系统状态读取失败提供真实可用的重试入口
def test_14_failure_recovery_entry():
    assert 'id="retrySystemStatus"' in JS
    assert 'id="recheckNetwork"' in JS


# ===== 设置状态真实性回归契约 =====

def _load_system_status_segment():
    """截取 loadSystemStatus 实现段。"""
    start = JS.index("async function loadSystemStatus")
    end = JS.index("async function loadDetectRecords")
    return JS[start:end]


# R1-1. Cookie 文案明确用于访问对应网站
def test_r1_cookie_copy_states_target_site_usage():
    seg = _settings_segment()
    assert "用于向对应网站发起已登录访问请求" in seg


# R1-2. Cookie 文案明确不发送给影迹开发者或无关服务
def test_r1_cookie_copy_states_no_developer_upload():
    seg = _settings_segment()
    assert "不会发送给影迹开发者或无关服务" in seg


# R1-3. 不再出现「仅在本机使用，不会上传」这一不准确组合
def test_r1_old_inaccurate_cookie_copy_removed():
    assert "仅在本机使用，不会上传" not in JS


# R1-4. 隐私文案包含目标网站请求边界，不做模糊绝对承诺
def test_r1_privacy_copy_states_target_site_boundary():
    seg = _settings_segment()
    assert "完成解析和下载所需的目标网站请求" in seg
    assert "不会主动上传" not in JS


# R3. 社区版不得残留商业激活逻辑或文案
def test_r3_commercial_activation_removed():
    seg = _settings_segment()
    for phrase in ("订单", "卖家", "激活", "设备标识", "LICENSE_REQUIRED"):
        assert phrase not in seg


# R2-1. 系统状态读取失败时清空旧快照，不残留过期结果
def test_r2_failure_clears_stale_snapshot():
    seg = _load_system_status_segment()
    ok_pos = seg.index('state.system = await api("/ffmpeg-status")')
    catch_pos = seg.index("catch")
    clear_pos = seg.index("state.system = null", catch_pos)
    assert ok_pos < catch_pos < clear_pos, "catch 分支必须清空 state.system"
    assert "state.systemLoadFailed = true" in seg


# R2-2. 失败后所有依赖系统状态的分区展示持续失败态 + 重试入口
def test_r2_persistent_retry_entry_in_dependent_sections():
    seg = _settings_segment()
    # 下载 / 网络与 Cookie：整段回落到不可读面板
    assert seg.count('settingsUnreadablePanel(') >= 2
    # 更新 / 关于：sys 为空时渲染 systemRetryBlock（含重试按钮）
    assert "function systemRetryBlock" in seg
    assert seg.count("systemRetryBlock()") >= 2
    assert "系统状态读取失败" in seg


# R2-3. 渲染只读取 state.system 单一来源，无第二份缓存副本
def test_r2_single_source_of_truth():
    # 除 loadSystemStatus 的赋值与清空外，不存在其他 state.system 赋值（无缓存旧值行为）
    import re
    assigns = re.findall(r"state\.system\s*=", JS)
    assert len(assigns) <= 3  # 初始化 null + 成功赋值 + 失败清空


# R2-4. 失败与恢复均有 toast 提示，且不泄漏异常详情
def test_r2_toast_on_fail_and_recover_no_internals():
    assert "仍无法读取，请稍后重试" in JS
    assert "系统状态已恢复" in JS
    seg = _settings_segment()
    for leaked in ("error.stack", "error.message", "Traceback", "stack"):
        assert leaked not in seg, f"设置页泄漏内部异常字段: {leaked}"


# R2-5. 重试入口绑定真实的重新读取动作
def test_r2_retry_binds_reload():
    assert '$("#retrySystemStatus")?.addEventListener' in JS
    # 重试动作调用 loadSystemStatus 重新读取
    idx = JS.index('$("#retrySystemStatus")?.addEventListener')
    assert "loadSystemStatus" in JS[idx:idx + 300]
