"""pytest 公共配置：把项目根加入 sys.path，保证 core 包可导入。

所有测试只使用 tmp_path 临时目录与模拟数据，
不访问真实 tasks.json / downloads / 回收站 / 真实网络平台。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def disable_local_session_guard_unless_test_enables_it(monkeypatch):
    """Existing endpoint tests focus on business behavior, not browser sessions.

    Production now enables the random local-session guard by default. Security
    tests explicitly set a token when they exercise that boundary.
    """
    import server

    monkeypatch.setattr(server, "_SESSION_TOKEN", "")
