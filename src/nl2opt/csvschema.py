"""CSV 表头判定与位置列名（dataset 摘要与 expand 共用）。

MIPLIB-NL 里有一批**无表头**文件（首行就是数据）：`DictReader` 会把数据行当表头，
于是报出「可用列: ['8.558...', '3.789...']」这种把数据值当列名的错（实测 gen-ip002 /
neos-3611689-kaihu / prod2）。这里统一判定，并给出位置列名 `col_1..col_n`。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

_NUM_RE = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")
_ID_RE = re.compile(r"^[A-Za-z]{0,3}\d{2,}$")


def is_number(cell: str) -> bool:
    return bool(_NUM_RE.match(cell.strip()))


def is_id_like(cell: str) -> bool:
    return bool(_ID_RE.match(cell.strip()))


def looks_like_data(cells: list[str]) -> bool:
    """首行是否像**数据**（无表头）。

    - 单列：该单元格是数值或 ID 样式（`C0000000`）→ 数据；
    - 多列：数值/ID 占比 ≥ 50%（(ID, 系数) 这种首行正好一半）。

    真表头（`Line_ID, Big_M_Capacity`、`Employee_ID, Task_0…`）不会命中。
    """
    vals = [c.strip() for c in cells if c.strip()]
    if not vals:
        return False
    numeric = sum(1 for c in vals if is_number(c))
    idlike = sum(1 for c in vals if is_id_like(c))
    if len(cells) == 1:
        return bool(numeric or idlike)
    return (numeric + idlike) / max(1, len(cells)) >= 0.5


def synthetic_columns(n: int) -> list[str]:
    """无表头文件的位置列名（1 起）：col_1..col_n。"""
    return [f"col_{i}" for i in range(1, n + 1)]


def read_first_row(path: Path) -> list[str]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            return [c.strip() for c in next(csv.reader(fh), [])]
    except (OSError, csv.Error):
        return []
