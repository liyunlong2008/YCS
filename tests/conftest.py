# -*- coding: utf-8 -*-
"""tests/conftest.py —— 最小配置：仅把 /workspace 注入 sys.path。

说明：
  2026-08-29 用户明确「历史数据+pytest 多余，抓紧上实盘」。
  为避免引入 stage8/18 CSV.GZ/拉 OKX 代理的额外复杂度，不再维护
  fixtures_available / @needs_fixtures 自动 SKIP 机制；相关测试文件
  （stage8/stage9/stage13/stage14）已整体移除。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest_plugins: list[str] = []


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT
