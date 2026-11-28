# 中文维护说明：定义 TOML 解析器的内部类型别名；不增加运行时业务依赖。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2021 Taneli Hukkinen
# Licensed to PSF under a Contributor Agreement.

from typing import Any, Callable, Tuple

# Type annotations
ParseFloat = Callable[[str], Any]
Key = Tuple[str, ...]
Pos = int
