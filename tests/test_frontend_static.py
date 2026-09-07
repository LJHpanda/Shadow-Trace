"""P1 前端静态检查：对 static/ 根目录唯一前端（index.html 外壳 + app.js 逻辑 + app.css）做文本级断言。

static/index.html 只提供页面外壳并加载外部 app.js/app.css；逻辑类断言检查
app.js，与 test_frontend_main.py 互为补充。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
SRC = HTML + "\n" + JS + "\n" + CSS  # 合并源，用于跨文件否定断言


class TestApiBase:
    def test_api_is_relative(self):
        # P1-4：API 必须是同源相对路径（位于 app.js）
        assert 'const API = "/api"' in JS

    def test_no_hardcoded_localhost_api(self):
        assert "http://localhost:5001/api" not in SRC
        assert "http://127.0.0.1:5001/api" not in SRC


class TestBatchSemantics:
    def test_batch_button_text(self):
        # P1-1：按钮语义 = 加入待下载列表
        assert "加入待下载列表" in JS
        assert "开始批量下载<" not in SRC  # 旧文案不得残留在按钮上


class TestMarketingCopy:
    def test_no_thousands_of_sites_claim(self):
        # P1-7：不夸大支持范围
        assert "数千网站" not in SRC


class TestIncompleteStatus:
    def test_status_label(self):
        # incomplete 状态文案存在于 app.js
        assert "残留待确认" in JS

    def test_css_has_incomplete_style(self):
        # 存在 incomplete 相关样式类（具体命名随前端演进）
        assert "incomplete" in CSS


class TestConfirmDialog:
    def test_no_native_confirm_calls(self):
        # P1-5：业务代码不回退到原生 confirm()
        script = re.search(r"<script[^>]*>([\s\S]*?)</script>", HTML)
        script = script.group(1) if script else ""
        script = re.sub(r"/\*[\s\S]*?\*/", "", script)
        script = re.sub(r"(?m)^\s*//.*$", "", script)
        bare = re.findall(r"(?<![\w.])confirm\s*\(", script)
        assert not bare, f"发现 {len(bare)} 处原生 confirm() 调用"


class TestPolling:
    def test_visibility_pause(self):
        assert "visibilitychange" in JS
        assert "document.hidden" in JS
