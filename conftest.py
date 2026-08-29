# -*- coding: utf-8 -*-
"""全局 pytest 配置：自动注入 asyncio_mode，把项目根目录（conftest.py 所在目录）加入 sys.path。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest_plugins: list[str] = []
