# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

yt_datas, yt_binaries, yt_hiddenimports = collect_all("yt_dlp")
certifi_datas, certifi_binaries, certifi_hiddenimports = collect_all("certifi")

# 远程助手扩展模块（弱绑定）：依赖缺失时静默跳过，主程序与打包流程不受影响
try:
    tg_datas, tg_binaries, tg_hiddenimports = collect_all("telegram")
    # 该库通过 httpx 通信，显式列出依赖链以确保打包完整
    tg_hiddenimports = list(tg_hiddenimports) + [
        "httpx", "httpcore", "anyio", "h11", "idna", "sniffio",
    ]
except Exception:  # noqa: BLE001
    tg_datas, tg_binaries, tg_hiddenimports = [], [], []

a = Analysis(
    ["desktop_launcher.py"],
    pathex=[],
    binaries=yt_binaries + certifi_binaries + tg_binaries,
    datas=[("static", "static")] + yt_datas + certifi_datas + tg_datas,
    hiddenimports=yt_hiddenimports + certifi_hiddenimports + tg_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YingJi",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    contents_directory="Runtime",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="YingJi",
)
