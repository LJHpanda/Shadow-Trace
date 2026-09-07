"""Run yt-dlp with the community edition outbound network guard enabled."""

from __future__ import annotations

import sys

from core.network_policy import install_yt_dlp_network_guard


def main(arguments=None) -> int:
    install_yt_dlp_network_guard()
    from yt_dlp import main as yt_dlp_main

    try:
        result = yt_dlp_main(list(sys.argv[1:] if arguments is None else arguments))
        return int(result or 0)
    except SystemExit as exc:
        return int(exc.code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
