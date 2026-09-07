"""影迹主前端（static/ 根目录唯一一套）的静态契约检查。

前端源码由 static/index.html + app.js + app.css 组成，本测试直接校验
公开仓库中的唯一前端入口。
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "static"
HTML = (MAIN / "index.html").read_text(encoding="utf-8")
JS = (MAIN / "app.js").read_text(encoding="utf-8")
CSS = (MAIN / "app.css").read_text(encoding="utf-8")


def test_main_frontend_is_self_contained():
    assert (MAIN / "index.html").is_file()
    assert "影迹 · 本地媒体工作台" in HTML
    assert "社区版" in HTML
    assert "专业版" not in HTML
    assert "个人专业版" not in HTML
    assert "PRO" not in HTML


def test_uses_same_origin_api_and_covers_primary_views():
    assert 'const API = "/api"' in JS
    assert "localhost:5001/api" not in JS
    for view in ("download", "batch", "tasks", "data", "settings"):
        assert f'data-view="{view}"' in HTML


def test_core_api_contracts_are_wired():
    for path in (
        "/download",
        "/detect-name",
        "/formats",
        "/playlist",
        "/batch-download",
        "/import/excel",
        "/harvest/export",
        "/detect-records",
        "/tasks",
        "/stats",
    ):
        assert path in JS


def test_accessibility_and_responsive_styles_are_present():
    assert 'aria-label="主要导航"' in HTML
    assert 'aria-live="assertive"' in HTML
    assert ":focus-visible" in CSS
    assert "@media (max-width: 680px)" in CSS


def test_download_polling_updates_live_regions_without_rerendering_the_view():
    assert 'state.view === "download") refreshDownloadLiveRegions()' in JS
    assert 'id="activeTaskCount"' in JS
    assert 'id="recentTaskList"' in JS


def test_task_polling_and_filters_update_only_the_task_list():
    assert 'state.view === "tasks") refreshTaskLiveRegions()' in JS
    assert 'id="taskSummary"' in JS
    assert 'id="tasksList"' in JS
    assert "refreshTaskLiveRegions();" in JS
    assert ".view.no-entry-animation" in CSS


def test_running_tasks_show_live_progress_speed_and_eta_in_both_views():
    assert "function taskProgressHtml" in JS
    assert 'details.push(task.speed)' in JS
    assert 'details.push(`剩余 ${task.eta}`)' in JS
    assert 'taskProgressHtml(task, "compact")' in JS
    assert 'taskProgressHtml(task, "card")' in JS
    assert ".task-live-progress.compact" in CSS
    assert ".task-live-progress-track" in CSS


def test_detection_has_staged_feedback_partial_success_and_client_timeout():
    assert "正在识别名称" in JS
    assert "正在读取格式" in JS
    assert "Promise.allSettled" in JS
    assert "new AbortController()" in JS
    assert "25000" in JS
    assert "格式暂不可用，可直接下载" in JS


def test_light_theme_is_persistent_and_available_in_settings():
    assert 'localStorage.getItem("yingji_alt_theme")' in HTML
    assert 'id="themeSwitch"' in JS
    assert 'localStorage.setItem("yingji_alt_theme", state.theme)' in JS
    assert 'html[data-theme="light"]' in CSS


def test_directory_picker_is_explicit_and_default_downloads_remain_available():
    # Portable 首页留空时写入自带 Downloads，不再隐式弹窗阻塞任务创建。
    # 浏览按钮、编辑台和批量流程仍可明确调用系统目录选择器。
    assert "requestDownloadDirectory" in JS
    assert JS.count("await requestDownloadDirectory()") == 3
    assert "留空保存到软件内 Downloads" in JS
    assert "directoryPickerBusy" in JS
    assert "Promise.all([loadTasks(false), loadGroups()])" in JS
    assert "yingji_default_download_dir" not in JS
    for removed_control in ("outputDir", "batchOutputDir", "defaultDownloadDir"):
        assert f'id="{removed_control}"' not in JS


def test_home_and_settings_hide_implementation_details_from_users():
    assert "今日保存" in JS
    assert "本周保存" in JS
    assert "可用空间" in JS
    settings_source = JS[JS.index("function renderSettings()"):JS.index("function renderView(")]
    for implementation_detail in ("FFmpeg", "yt-dlp", "Cookie", "分片并发", "日志目录"):
        assert implementation_detail not in settings_source
    assert "userFriendlyTaskError" in JS
    assert "任务处理未完成，请检查链接后重试" in JS
    assert "esc(task.error)" not in JS
    assert "toast(error.message" not in JS
    # UI-P0-04：设置页需从真实接口读取版本与只读状态，允许调用该接口一次，
    # 但内部字段（组件路径/日志目录/片段保留）不得渲染到用户界面。
    assert JS.count('api("/ffmpeg-status")') == 1
    for internal_field in ("ffmpeg_path", "log_dir", "keep_fragments"):
        assert internal_field not in JS
    assert "serviceDot" not in HTML


def test_task_operations_match_the_classic_frontend():
    for label in (
        "播放视频",
        "打开所在文件夹",
        "续传任务",
        "开始下载",
        "中断下载",
        "清理任务残留",
        "删除任务",
    ):
        assert label in JS
    assert "open-file" in JS
    assert "open-folder" in JS
    assert "?delete_files=true" in JS


def test_collection_workbench_keeps_edit_export_and_queue_capabilities():
    for text in (
        "采集编辑台",
        "导入 Excel",
        "下载模板",
        "前缀 + 序号",
        "替换文件名",
        "自动过滤已下载",
        "导出 Excel",
        "加入下载队列",
    ):
        assert text in JS
    assert "workbenchRows" in JS
    assert "importWorkbenchExcel" in JS
    assert "exportWorkbench" in JS
    assert "queueWorkbench" in JS


def test_detection_records_can_be_saved_loaded_and_deleted():
    for text in (
        "检测记录",
        "载入编辑台",
        "每次成功检测都会自动保存在本机",
    ):
        assert text in JS
    assert "saveDetectRecord" in JS
    assert "loadRecordToWorkbench" in JS
    assert "deleteDetectRecord" in JS
    assert 'method: "DELETE"' in JS


def test_batch_workspace_has_consistent_modes_and_optimized_tool_groups():
    for mode in ("flow", "records", "workbench"):
        assert f'["{mode}"' in JS
    assert "batch-switcher" in CSS
    assert "workbench-command-bar" in CSS
    assert "tool-heading" in CSS
    assert "record-card" in CSS


def test_batch_inspect_button_and_columns_exist():
    """UI-P0-06：批量获取·检查与选择 步骤2 增加「文件名 + 格式」检测列与按钮。"""
    assert "检测文件名与格式" in JS
    assert "inspectBatchSelected" in JS
    assert "/batch-inspect" in JS
    # 行内三列 class 都应在 JS 与 CSS 中出现
    for cls in ("batch-row-filename", "batch-row-ext", "batch-row-duration"):
        assert cls in JS, f"{cls} missing in app.js"
        assert cls in CSS, f"{cls} missing in app.css"
    assert "batch-toolbar-actions" in JS or "batch-toolbar-actions" in CSS
    assert ".batch-row.is-inspecting" in CSS
    # 采集编辑台新增「格式」列
    assert 'class="col-ext"' in JS
    assert "data-workbench-field=\"ext\"" in JS


def test_sanitize_basename_and_url_inference_helpers_exist_in_server():
    """sanitize 与 URL 兜底推断逻辑已抽到 module-level，供 /api/detect-name 与 /api/batch-inspect 复用。"""
    server_src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "def _sanitize_basename(raw" in server_src
    assert "def _infer_filename_from_url(url)" in server_src
    assert "def _infer_ext_from_url(url)" in server_src
    # 路由注册
    assert '@app.route("/api/batch-inspect"' in server_src
    # detect-records 落盘放行 filename / ext
    detect_records_block = server_src[server_src.index("def api_add_detect_record"):server_src.index("@app.route(\"/api/detect-records/<record_id>\"")]
    assert "\"filename\"" in detect_records_block
    assert "\"ext\"" in detect_records_block
