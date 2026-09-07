"""Development and Windows Portable path resolution.

Portable builds keep immutable application resources separate from mutable
user data.  Environment overrides remain available for tests and isolated
development runs.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    app_dir: Path
    resource_dir: Path
    runtime_dir: Path
    data_dir: Path
    downloads_dir: Path
    static_dir: Path
    cookies_file: Path
    portable: bool


def resolve_runtime_paths() -> RuntimePaths:
    source_dir = Path(__file__).resolve().parent.parent
    frozen = bool(getattr(sys, "frozen", False))

    if frozen:
        app_dir = Path(sys.executable).resolve().parent
        resource_dir = Path(getattr(sys, "_MEIPASS", app_dir)).resolve()
    else:
        app_dir = Path(os.environ.get("YINGJI_APP_DIR") or source_dir).resolve()
        resource_dir = source_dir

    portable_override = os.environ.get("YINGJI_PORTABLE")
    portable_marker = app_dir / "portable.flag"
    portable = portable_override == "1" or (
        frozen and portable_override != "0" and portable_marker.is_file()
    )

    if portable:
        data_default = app_dir / "Data"
        cookies_default = data_default / "cookies.txt"
        downloads_default = app_dir / "Downloads"
    elif frozen:
        local_app_data = Path(
            os.environ.get("LOCALAPPDATA")
            or (Path.home() / "AppData" / "Local")
        )
        data_default = local_app_data / "YingJi" / "Data"
        cookies_default = data_default / "cookies.txt"
        downloads_default = Path.home() / "Downloads" / "YingJi"
    else:
        data_default = app_dir
        cookies_default = app_dir / "cookies.txt"
        downloads_default = data_default / "downloads"

    data_dir = Path(os.environ.get("YINGJI_DATA_DIR") or data_default).resolve()
    downloads_dir = Path(
        os.environ.get("YINGJI_DOWNLOADS_DIR") or downloads_default
    ).resolve()
    runtime_dir = Path(
        os.environ.get("YINGJI_RUNTIME_DIR") or app_dir / "Runtime"
    ).resolve()
    cookies_file = Path(
        os.environ.get("YINGJI_COOKIES_FILE") or cookies_default
    ).resolve()
    return RuntimePaths(
        app_dir=app_dir,
        resource_dir=resource_dir,
        runtime_dir=runtime_dir,
        data_dir=data_dir,
        downloads_dir=downloads_dir,
        static_dir=resource_dir / "static",
        cookies_file=cookies_file,
        portable=portable,
    )
