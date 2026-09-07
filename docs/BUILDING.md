# Windows 社区版构建

## 构建要求

- Windows 10/11 x64
- Python 3.10 或更高版本
- `requirements-build.txt` 中的固定依赖
- 包含 `ffmpeg.exe` 和 `ffprobe.exe` 的 `Runtime` 目录
- 构建安装器时需要 NSIS 3
- 正式发布建议准备 Authenticode 代码签名证书

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\scripts\build_release.ps1 `
  -PythonExe .\.venv\Scripts\python.exe `
  -RuntimeSource .\Runtime `
  -SigningThumbprint <证书指纹>
```

本地测试可使用：

```powershell
.\scripts\build_release.ps1 -AllowUnsigned -SkipInstaller
```

发布脚本会生成 Portable ZIP、可选安装器、`SHA256SUMS.txt`、构建清单和发布洁净度报告。未签名构建只适合测试或明确标注的社区预览。

FFmpeg 发布义务见 `legal/FFMPEG_SOURCE.txt`。发布者必须保存并提供与实际二进制对应的源码和构建信息，不能只依赖可能失效的网页链接。
