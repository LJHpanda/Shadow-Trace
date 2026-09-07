# 影迹 · Capture Every Stream

影迹是面向 Windows 10/11 x64 的开源本地媒体下载工作台。复制媒体链接、选择保存目录，即可完成资源识别、画质选择、任务下载与格式转换；全部社区版功能可直接使用，无设备激活机制。

项目以 yt-dlp 为媒体解析核心，兼容范围不局限于少数几个视频网站。根据实际使用情况，大多数主流平台中可在浏览器公开访问的 HTTPS 视频页面都可以直接粘贴解析，覆盖综合视频、短视频、社交媒体公开视频、资讯与教育媒体、音频内容以及公开 HLS/M3U8 流等常见来源。

## 平台兼容性

- **重点验证：**B 站、YouTube、抖音，以及公开 HLS/M3U8 链接。
- **广泛兼容：**支持大量 yt-dlp 已适配的主流媒体站点；如果内容能在浏览器中正常公开访问，通常可以直接把页面 HTTPS 链接交给影迹识别。
- **统一体验：**支持自动读取标题、封面、可用画质和媒体格式，并使用同一套任务队列完成下载、暂停、恢复与转换。
- **持续适配：**平台兼容能力会随 yt-dlp 和影迹自身更新继续扩展，无需为每个平台分别安装下载工具。

平台规则会持续变化。需要登录、Cookie、会员或付费权限的内容，以及受到地区限制、风控验证、版权保护或 DRM 保护的资源，可能无法解析或下载；项目也无法承诺任何第三方平台永久可用。

> 请只保存和使用你有权处理的内容，并遵守内容来源平台的服务条款及所在地法律。

## 主要功能

- 单视频、公开 M3U8、播放列表和合集下载
- 自动识别标题、画质和可用格式
- 多任务排队、暂停、恢复、取消与断点续传
- 自定义保存目录、任务持久化、完成通知
- 音视频和图片格式转换
- Cookie 文件支持
- Telegram 允许名单远程助手和可选翻译
- Windows Portable ZIP 与 NSIS 安装器构建

## 安全设计

- Web 服务仅监听 `127.0.0.1`，不面向局域网或公网。
- 本地 API 使用随机会话 Cookie，并校验请求来源。
- 用户链接、yt-dlp 内部请求和重定向均拒绝本机/内网目标；直连时还校验每次 DNS 结果。显式代理模式下，域名解析边界由该代理承担。
- Telegram 默认关闭，必须在本机配置 Token 和允许名单；每个 Chat ID 有提交频率和并发配额。
- Cookie、Token、任务、日志、下载文件和构建运行时均被排除在 Git 和发布源码之外。

更多信息见 [安全策略](SECURITY.md) 和 [Telegram 使用说明](docs/TELEGRAM.md)。

## 直接使用源码

### 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10/11 x64 |
| Python | 3.10 或更高版本 |
| FFmpeg | `ffmpeg` 和 `ffprobe` 可在 PATH 中使用，或放入 `Runtime/` |
| 浏览器 | 任意现代浏览器 |

### 安装

1. 克隆或下载完整源码，不要只复制 `server.py`。
2. 双击 `setup.bat`。脚本会在项目内创建独立 `.venv` 并安装 `requirements.txt`。
3. 双击 `start.bat`。
4. 浏览器打开 `http://127.0.0.1:5001/`。
5. 停止时使用当前窗口的 `Ctrl+C`，或双击 `stop.bat`。

PowerShell 等价命令：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

`stop.bat` 只读取本项目生成的 `server.pid`，确认命令行属于本项目后才停止该进程树，不会全局终止其他 `python`、`yt-dlp` 或 `ffmpeg` 任务。

## FFmpeg 与 Cookie

开发模式依次查找：

1. `Runtime/ffmpeg.exe` 和 `Runtime/ffprobe.exe`；
2. 系统 PATH 中的 `ffmpeg` 和 `ffprobe`。

需要登录的网站可使用 Netscape 格式的 `cookies.txt`：

- 源码开发模式：项目根目录 `cookies.txt`；
- Portable 模式：`Data/cookies.txt`；
- 安装版：`%LOCALAPPDATA%\YingJi\Data\cookies.txt`。

Cookie 等同账号凭据，不要提交到 Git、Issue 或日志。`.gitignore` 已默认排除该文件。

## 配置目录

| 运行方式 | 数据目录 | 默认下载目录 |
|---|---|---|
| 源码 | 项目根目录 | `downloads/` |
| Portable | 程序目录 `Data/` | 程序目录 `Downloads/` |
| 安装版 | `%LOCALAPPDATA%\YingJi\Data` | `%USERPROFILE%\Downloads\YingJi` |

可用于隔离开发环境的变量：

- `YINGJI_PORT`
- `YINGJI_DATA_DIR`
- `YINGJI_DOWNLOADS_DIR`
- `YINGJI_RUNTIME_DIR`
- `YINGJI_COOKIES_FILE`
- `YINGJI_PORTABLE`

## 开发与测试

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\public_release_check.py
node --check static\app.js
node --check static\telegram-bot.js
```

贡献要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## Windows 构建

安装构建依赖后执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\scripts\build_release.ps1 `
  -PythonExe .\.venv\Scripts\python.exe `
  -RuntimeSource .\Runtime `
  -SigningThumbprint <代码签名证书指纹>
```

本地无签名 Portable 验证可使用：

```powershell
.\scripts\build_release.ps1 -AllowUnsigned -SkipInstaller
```

详细要求见 [Windows 构建说明](docs/BUILDING.md)。构建脚本会检查许可证、FFmpeg、运行数据污染、签名状态并生成 SHA-256 和构建清单。

## 公开仓库边界

当前私有开发仓库包含其他分支和历史，不能直接切换为 Public。GitHub 仓库可见性会公开整个仓库，而不是只公开 `opensource` 分支。

首次公开应执行：

```powershell
.\.venv\Scripts\python.exe scripts\public_release_check.py
.\scripts\export_public_source.ps1
```

然后用生成的无 Git 历史源码 ZIP 创建新的公开仓库。完整步骤见 [公开发布说明](docs/PUBLISHING.md)。

## 项目结构

| 路径 | 说明 |
|---|---|
| `server.py` | Flask 本地服务和任务管理 |
| `desktop_launcher.py` | Windows 桌面/Portable 启动器 |
| `yt_dlp_worker.py` | 带出站网络保护的 yt-dlp 子进程入口 |
| `core/` | 下载、转换、路径、安全和 Telegram 核心模块 |
| `static/` | 原生 Web 前端 |
| `tests/` | pytest 回归测试 |
| `scripts/` | 发布构建、源码导出和公开检查 |
| `installer/` | NSIS 安装器配置 |
| `legal/` | 第三方许可证与 FFmpeg 源码说明 |
| `.github/` | CI、安全扫描和协作模板 |

## 开源许可证

本仓库中由项目提供者拥有版权的源代码采用 **GNU General Public License v3.0 or later**（`GPL-3.0-or-later`）许可，完整条款见 [`LICENSE`](LICENSE)。

第三方组件继续适用各自许可证。FFmpeg 和其他组件说明见 [第三方软件声明](legal/THIRD_PARTY_NOTICES.txt) 与 [FFmpeg 源码说明](legal/FFMPEG_SOURCE.txt)。
