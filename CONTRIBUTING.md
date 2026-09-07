# 参与贡献

感谢你改进影迹。项目优先接受范围清晰、可验证、不会扩大本地安全边界的改动。

## 开始之前

1. 先搜索现有 Issue，确认问题尚未被报告。
2. Bug 请提供系统版本、Python 版本、复现步骤和经过脱敏的错误信息。
3. 新功能建议先开 Issue 说明使用场景；不要直接提交大规模重构。
4. 不要上传 Cookie、Bot Token、API Key、下载链接签名、本机绝对路径、日志或媒体文件。

## 本地开发

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\public_release_check.py
```

前端是原生 HTML/CSS/JavaScript，无需 Node 构建；如本机有 Node，可额外执行：

```powershell
node --check static\app.js
node --check static\telegram-bot.js
```

## Pull Request 要求

- 一个 PR 解决一个明确问题。
- 行为变化必须增加或更新测试。
- 保持本地服务仅监听 `127.0.0.1`，不得改为局域网或公网监听。
- 不得绕过 URL 网络策略、会话 Cookie、Telegram 允许名单或发布洁净度检查。
- 面向用户的错误不得包含 Token、Cookie、签名参数、本机路径或原始 traceback。
- 提交前确认 `pytest`、公开源码检查和相关语法检查通过。

提交贡献即表示你有权提供该内容，并同意按本项目的
`GPL-3.0-or-later` 许可证发布。
