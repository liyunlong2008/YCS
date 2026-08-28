# -*- coding: utf-8 -*-
"""全局 pytest 配置：自动注入 asyncio_mode，把 /workspace 加入 sys.path。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest_plugins: list[str] = []
