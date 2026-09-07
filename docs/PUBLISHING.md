# 公开仓库发布说明

## 不要直接公开当前私有仓库

GitHub 的可见性作用于整个仓库，不是某一个分支。当前私有仓库还保存其他开发分支和既往历史，因此不能通过把该仓库直接改为 Public 来只公开社区版分支。

正确做法是创建一个新的公开仓库，并从无 Git 历史的社区版源码快照开始：

1. 在 `opensource` 分支确认工作区干净且 CI 全部通过。
2. 执行 `python scripts/public_release_check.py`。
3. 执行 `powershell -ExecutionPolicy Bypass -File scripts/export_public_source.ps1`。
4. 解压 `release-source/YingJi-<version>-source.zip` 到一个新目录。
5. 在该目录执行 `git init -b main`、首次提交，再关联新的公开 GitHub 仓库。
6. 在公开仓库启用 Issues、Discussions、Private vulnerability reporting、Dependabot alerts 和 Code scanning。

这样公开仓库只有经过检查的社区版快照，不包含私有仓库的其他分支、删除过的文件或历史对象。

## 首次公开前检查

- 仓库描述明确说明仅用于保存有权处理的内容。
- 默认分支为 `main`，分支保护要求 CI 通过。
- `SECURITY.md`、`CONTRIBUTING.md`、行为准则和 Issue 模板可见。
- 不存在 Cookie、Token、API Key、真实日志、下载文件、本机路径或内部报告。
- 源码 ZIP 和 Windows 包均发布 SHA-256。
- Windows 安装包如果没有 Authenticode 签名，必须明确标注“未签名社区构建”。
- FFmpeg 二进制、许可证和对应源码提供方式已复核。
