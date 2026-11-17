"""符号化 IR 展开器：读数据 → 解析引用 → 展开索引 → 产出**展平 IR**。

## 为什么数据解析必须放在这一层

`verifier.check_feasibility` 把解代回
每条约束 —— 两者都要求 `Term.coef` 是**字面浮点**。若把 CSV 读取推迟到求解层，
验证器就拿不到系数，「防伪造 objective」的防线会直接失效。

因此本模块是**求解与验证的共同前置**：

    SymbolicIR + 数据目录 ──expand()──► IR ──┬──► solver
                                              └──► verifier（同一份已解析的 IR）

## 展开语义

- **变量族**：`dims` 按顺序映射到索引名 `i, j, k, l, m`，实例名 `基名_值1_值2...`（多维用 `_` 连接，
  与 `ir.Variable.instances()` 完全一致）。
- **约束**：`for_each="j"` → 对 `j` 的每个取值生成一条，约束名里的 `{j}` 同步替换。
- **项里的占位符**：
  - 若该名字被 `for_each` **绑定** → 取其当前值（一条约束内固定）；
  - 若**未绑定** → 视为**对该维求和**，展开成该维所有取值的项之和。
- **索引对齐**：来自 CSV 的索引维按该列**去重保序**；`DataRef` 的键在该文件里找不到时报错并
  列出可用键，避免"静默取不到值当 0"。
"""
from __future__ import annotations

import csv
import difflib
import itertools
import re
from pathlib import Path
from typing import Any

from .csvschema import looks_like_data, synthetic_columns

from .ir import Constraint, IR, LinearExpr, Objective, Term, Variable
from .symbolic import (
    DataRef,
    ForEachRows,
    IndexSource,
    SymConstraint,
    SymExpr,
    SymVar,
    SymbolicIR,
)

__all__ = [
    "ExpandError",
    "DataStore",
    "load_data_dir",
    "empty_data_store",
    "expand",
    "scan_data_refs",
    "check_data_files",
    "check_data_refs",
]

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# 列模板/行号里的占位符，允许算术：`Task_{t}`、`{s+1}`、`__{k-2}`
_TEMPLATE = re.compile(r"\{(\w+)([+-]\d+)?\}")
_INDEX_NAMES = ("i", "j", "k", "l", "m")
DEFAULT_MAX_INSTANCES = 50_000
_MAX_COL_PREVIEW = 12


def _norm_col(name: str) -> str:
    """列名归一化：忽略大小写、空格、连字符、句点与连续下划线。

    动机：`instance.json` 的描述是散文，LLM 常把 `Line_ID` 写成 `Line ID` /
    `line-id`。列名归一化后能唯一命中就不该判死，命中记录进 `DataStore.repairs`
    （报告里单独统计，不悄悄掩盖模型错误）。
    """
    s = name.strip()
    s = re.sub(r"[\s\-.]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s.casefold()


def _has_placeholder(name: str) -> bool:
    return "{" in name or "}" in name


def _col_preview(cols: list[str]) -> str:
    shown = ", ".join(cols[:_MAX_COL_PREVIEW])
    if len(cols) > _MAX_COL_PREVIEW:
        return f"{shown}, …（共 {len(cols)} 列）"
    return f"{shown}（共 {len(cols)} 列）"


class ExpandError(Exception):
    """展开失败（数据缺失、索引对不齐、规模超限等）。"""


class DataStore:
    """数据目录的只读视图：按文件缓存 CSV 的列与索引。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        if not self.root.exists():
            raise ExpandError(f"数据目录不存在: {self.root}")
        self._cols: dict[str, dict[str, list[str]]] = {}
        self._keys: dict[tuple[str, str], dict[str, int]] = {}
        self._colmap: dict[str, dict[str, list[str]]] = {}
        self._headerless: dict[str, bool] = {}
        # 归一化命中的列名（file, 请求名, 实际列名）—— 报告里单独统计
        self.repairs: list[tuple[str, str, str]] = []

    # ---------- 内部 ----------

    def _resolve(self, file: str) -> Path:
        """把 `file` 解析成实际路径，容忍 LLM 常见的几种前缀写法。

        `DataStore.root` 通常已经指向实例的 `data/` 目录，而 LLM 可能写
        `duration.csv` / `./data/duration.csv` / `data/duration.csv`，
        直接拼会得到 `data/data/...`。这里按「原样 → 去 data/ 前缀 → 仅文件名」依次尝试。
        """
        raw = file.strip().replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        candidates = [self.root / raw]
        if raw.startswith("data/"):
            candidates.append(self.root / raw[len("data/"):])
        name = Path(raw).name
        candidates.append(self.root / name)
        # root 的上一级也试一下（有些数据集的 files.path 相对实例根而非 data/）
        candidates.append(self.root.parent / raw)

        seen: set[str] = set()
        for c in candidates:
            key = str(c)
            if key in seen:
                continue
            seen.add(key)
            if c.exists() and c.is_file():
                return c
        raise ExpandError(
            f"数据文件不存在: {file!r}；在 {self.root} 下尝试过 "
            f"{[str(c) for c in candidates]}"
        )

    def _load(self, file: str) -> dict[str, list[str]]:
        if file in self._cols:
            return self._cols[file]
        p = self._resolve(file)
        try:
            with p.open(encoding="utf-8-sig", newline="") as fh:
                reader = csv.reader(fh)
                first = next(reader, None)
                if not first:
                    raise ExpandError(
                        f"{p.name} 是空文件（0 字节/无数据行），不能当作数据表引用"
                    )
                # 无表头（首行就是数据）→ 首行也当数据，列名用位置名 col_1..col_n
                headerless = looks_like_data([c.strip() for c in first])
                names = synthetic_columns(len(first)) if headerless else [c.strip() for c in first]
                cols: dict[str, list[str]] = {name: [] for name in names}
                rows = itertools.chain([first] if headerless else [], reader)
                for row in rows:
                    for i, name in enumerate(names):
                        cols[name].append((row[i] if i < len(row) else "").strip())
        except OSError as exc:
            raise ExpandError(f"读取 {p} 失败: {exc}") from exc
        self._headerless[file] = headerless
        self._cols[file] = cols
        return cols

    def is_headerless(self, file: str) -> bool:
        self._load(file)
        return self._headerless.get(file, False)

    def column_names(self, file: str) -> list[str]:
        return list(self._load(file))

    def row_count(self, file: str) -> int:
        cols = self._load(file)
        return len(next(iter(cols.values()))) if cols else 0

    def _column_map(self, file: str) -> dict[str, list[str]]:
        """归一化列名 → 原始列名列表（用于容错匹配）。"""
        if file not in self._colmap:
            m: dict[str, list[str]] = {}
            for c in self._load(file):
                m.setdefault(_norm_col(c), []).append(c)
            self._colmap[file] = m
        return self._colmap[file]

    def _column_error(self, file: str, name: str) -> ExpandError:
        cols = list(self._load(file))
        close = difflib.get_close_matches(name, cols, n=5, cutoff=0.4)
        norm_hits = self._column_map(file).get(_norm_col(name), [])
        hints: list[str] = []
        if norm_hits:
            hints.append(f"忽略大小写/空格/连字符后可以匹配到: {norm_hits}")
        if close:
            hints.append(f"最接近的列名: {close}")
        msg = f"{file} 没有列 {name!r}；可用列: {_col_preview(cols)}"
        if hints:
            msg += "\n  " + "\n  ".join(hints)
        return ExpandError(msg)

    def _resolve_column(self, file: str, name: str,
                        bindings: dict[str, str] | None = None) -> str:
        """把模型写的列名解析成文件里的真实列名。

        顺序：占位符替换（列模板，需 bindings）→ 精确命中 → 归一化唯一命中 → 报错。
        """
        if _has_placeholder(name):
            if bindings is None:
                raise ExpandError(
                    f"{file}: 列名 {name!r} 含占位符 —— 列模板只能用在有 for_each/dims "
                    "绑定的上下文里（DataRef.column），不能用在索引维声明里。"
                )
            name = self._substitute(name, bindings, f"{file} 的列模板")
        cols = self._load(file)
        if name in cols:
            return name
        norm_hits = self._column_map(file).get(_norm_col(name), [])
        if len(norm_hits) == 1:
            used = norm_hits[0]
            self.repairs.append((file, name, used))
            return used
        raise self._column_error(file, name)

    @staticmethod
    def _substitute(template: str, bindings: dict[str, str], ctx: str) -> str:
        """把 `Task_{t}` / `{s+1}` 里的占位符替换成绑定值（支持 ±整数）。"""
        def repl(m: "re.Match[str]") -> str:
            name, delta = m.group(1), m.group(2)
            if name not in bindings:
                raise ExpandError(
                    f"{ctx}: 占位符 {{{name}}} 未绑定（当前可用绑定 {sorted(bindings)}）"
                )
            val = str(bindings[name])
            if not delta:
                return val
            try:
                return str(int(float(val)) + int(delta))
            except (TypeError, ValueError) as exc:
                raise ExpandError(
                    f"{ctx}: 占位符 {{{name}}}{delta} 需要数值绑定，实际是 {val!r}。"
                    "若这是标签（如 `economic scenario_1`），请改用把它作为 key_value 的引用，"
                    "而不是当作行号做算术。"
                ) from exc

        return _TEMPLATE.sub(repl, template)

    def _key_index(self, file: str, key_column: str) -> dict[str, int]:
        col = self._resolve_column(file, key_column)
        ck = (file, col)
        if ck in self._keys:
            return self._keys[ck]
        cols = self._load(file)
        idx = {v: i for i, v in enumerate(cols[col])}
        self._keys[ck] = idx
        return idx

    # ---------- 对外 ----------

    # 元数据文件：不是"数据"，不应被当成可引用的数据文件
    _META_NAMES = frozenset({"instance.json", "model.md", "solve.py", "readme.md"})
    _META_SUFFIX = frozenset({".md", ".py"})

    def available_files(self) -> set[str]:
        """数据目录下真实存在的**数据文件**名集合（已排除元数据）。

        为什么排除：无 `data/` 子目录的实例，`data_dir` 会退到实例根目录，
        那里只有 `instance.json` / `model.md` / `solve.py`。若把这三个也列进
        "可用数据文件"，报错信息会误导模型（它们不是数据）。
        """
        if not self.root.is_dir():
            return set()
        out: set[str] = set()
        for p in self.root.iterdir():
            if not p.is_file():
                continue
            if p.name.lower() in self._META_NAMES:
                continue
            if p.suffix.lower() in self._META_SUFFIX:
                continue
            try:
                if p.stat().st_size == 0:
                    continue      # 空文件（数据集缺陷）不是可用数据文件
            except OSError:
                pass
            out.add(p.name)
        return out

    def column(self, file: str, column: str) -> list[str]:
        col = self._resolve_column(file, column)
        return self._load(file)[col]

    def distinct(self, file: str, column: str) -> list[str]:
        """去重保序（索引维的顺序会影响约束名，必须稳定）。"""
        seen: dict[str, None] = {}
        for v in self.column(file, column):
            if v != "":
                seen.setdefault(v, None)
        return list(seen)

    def rows(self, file: str, keys: list[str]) -> list[dict[str, str]]:
        """按行取若干列（用于 `ForEachRows` 的跨表关联遍历）。"""
        cols = self._load(file)
        resolved = [self._resolve_column(file, k) for k in keys]
        n = len(cols[resolved[0]])
        for k in resolved[1:]:
            if len(cols[k]) != n:
                raise ExpandError(f"{file} 的列长度不一致（{resolved[0]}={n}, {k}={len(cols[k])}）")
        return [{k: cols[k][i] for k in resolved} for i in range(n)]

    def _resolve_key_values(self, ref: DataRef, bindings: dict[str, str]) -> list[str]:
        """把 ref 的 key_value 解析成字面键值（占位符从 bindings 取）。"""
        out: list[str] = []
        for kv in ref.key_values:
            if isinstance(kv, str):
                m = _PLACEHOLDER.fullmatch(kv.strip())
                if m:
                    name = m.group(1)
                    if name not in bindings:
                        raise ExpandError(
                            f"{ref.file} 的键占位符 {{{name}}} 未绑定：当前可用绑定 "
                            f"{sorted(bindings)}。key_value 里的占位符只能来自当前约束的 "
                            "for_each（字符串索引名 / rows 的 names）或所引用变量族的 dims 索引名。"
                        )
                    out.append(bindings[name])
                    continue
            out.append(str(kv))
        return out

    def lookup_raw(self, ref: DataRef, bindings: dict[str, str]) -> str:
        """取**原始字符串**（不转数值），用于 `bind`（按分类值分派）。"""
        if getattr(ref, "row", None) is not None:
            row = self._resolve_row(ref, bindings)
            cols = self._load(ref.file)
            col = self._resolve_column(ref.file, ref.column, bindings)
            if row < 1 or row > len(cols[col]):
                raise ExpandError(
                    f"{ref.file}: row={row} 超出范围（共 {len(cols[col])} 行，行号从 1 起）"
                )
            raw = cols[col][row - 1]
            if raw == "":
                raise ExpandError(f"{ref.file} 第 {row} 行的 {col} 为空，无法用于按值分派")
            return raw
        resolved = self._resolve_key_values(ref, bindings)
        cols = self._load(ref.file)
        kcs = [self._resolve_column(ref.file, kc, bindings) for kc in ref.key_columns]
        col = self._resolve_column(ref.file, ref.column, bindings)

        n = len(cols[col])
        for i in range(n):
            if all(cols[kc][i] == rv for kc, rv in zip(kcs, resolved)):
                raw = cols[col][i]
                if raw == "":
                    raise ExpandError(
                        f"{ref.file}: 键 {dict(zip(kcs, resolved))} 的 {col} 为空，"
                        "无法用于按值分派"
                    )
                return raw
        sample = [{kc: cols[kc][i] for kc in kcs} for i in range(min(6, n))]
        raise ExpandError(
            f"{ref.file}: 键 {dict(zip(kcs, resolved))} 不存在（共 {n} 行，示例 {sample}）"
        )

    def lookup(self, ref: DataRef, bindings: dict[str, str]) -> float:
        """查一个标量，返回 `value * scale + offset`。

        两种定位方式：
        - 键定位（`key_column` + `key_value`，支持复合键/占位符/列模板）；
        - 行序定位（`row`，1 起）+ `column`（可模板）—— 用于无键列的宽/转置表。
        """
        col = self._resolve_column(ref.file, ref.column, bindings)
        cols = self._load(ref.file)
        n = len(cols[col])

        if getattr(ref, "row", None) is not None:
            row = self._resolve_row(ref, bindings)
            if row < 1 or row > n:
                mode = getattr(ref, "on_missing", "skip")
                if mode == "skip":
                    return None  # type: ignore[return-value]
                if mode == "zero":
                    return 0.0
                raise ExpandError(
                    f"{ref.file}: 行号 {row} 超出范围（共 {n} 行，行号从 1 起）"
                )
            raw = cols[col][row - 1]
            return self._finish_value(ref, raw, f"{ref.file} 第 {row} 行")

        resolved = self._resolve_key_values(ref, bindings)
        kcs = [self._resolve_column(ref.file, kc, bindings) for kc in ref.key_columns]

        hit = -1
        for i in range(n):
            if all(cols[kc][i] == rv for kc, rv in zip(kcs, resolved)):
                hit = i
                break
        if hit < 0:
            mode = getattr(ref, "on_missing", "skip")
            if mode == "skip":
                # 该组合在数据里不存在（工业数据通常只列可用组合）→ 调用方跳过该项
                return None  # type: ignore[return-value]
            if mode == "zero":
                return 0.0
            sample = [
                {kc: cols[kc][i] for kc in kcs} for i in range(min(6, n))
            ]
            raise ExpandError(
                f"{ref.file}: 键 {dict(zip(kcs, resolved))} 不存在"
                f"（文件共 {n} 行，前几行的键示例 {sample}）"
            )
        raw = cols[col][hit]
        return self._finish_value(ref, raw, f"{ref.file} 键 {dict(zip(kcs, resolved))}")

    def _resolve_row(self, ref: DataRef, bindings: dict[str, str]) -> int:
        rv = ref.row
        if isinstance(rv, str):
            if _has_placeholder(rv):
                rv = self._substitute(rv.strip(), bindings, f"{ref.file} 的 row")
        try:
            return int(float(str(rv)))
        except (TypeError, ValueError) as exc:
            raise ExpandError(
                f"{ref.file}: row={ref.row!r} 解析后是 {rv!r}，不是整数行号。"
                "行号是数据行的**序号 1..N**，不是行标签的值；如果你手里的值是标签"
                "（如 `economic scenario_1`），请把它所在的列写成 key_column/key_value 去查。"
            ) from exc

    @staticmethod
    def _finish_value(ref: DataRef, raw: str, ctx: str) -> float:
        if raw == "":
            mode = getattr(ref, "on_empty", "skip")
            if mode == "skip":
                return None  # type: ignore[return-value]
            if mode == "zero":
                return 0.0
            raise ExpandError(f"{ctx} 的 {ref.column} 为空（若该组合无定义，应从模型中排除）")
        try:
            return float(raw) * ref.scale + ref.offset
        except ValueError as exc:
            raise ExpandError(f"{ctx} 的 {ref.column}={raw!r} 不是数值") from exc


def load_data_dir(path: str | Path) -> DataStore:
    return DataStore(path)


def empty_data_store() -> DataStore:
    """一个**保证为空**的数据目录，供没有数据文件的题目走同一条展开链路。

    为什么不用 `DataStore(".")` 或实例根目录：`_resolve` 会退化到「仅文件名」和
    「root 的上一级」，那样可能意外命中真实文件，让「无数据」悄悄变成「有数据」；
    而 `available_files()` 也会把无关文件报成「实际可用」。
    """
    import tempfile

    d = Path(tempfile.gettempdir()) / "nl2opt-empty-data"
    d.mkdir(parents=True, exist_ok=True)
    return DataStore(d)


# ---------------- 虚构文件预检 ----------------


def _basename(file: str) -> str:
    """把 `./data/x.csv` / `data/x.csv` / `x.csv` 统一成 `x.csv`。"""
    raw = file.strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if raw.startswith("data/"):
        raw = raw[len("data/"):]
    return Path(raw).name


def scan_data_refs(sym: SymbolicIR) -> set[str]:
    """收集符号模型里引用的**全部**数据文件（去重）。"""
    files: set[str] = set()

    def _add(ref: Any) -> None:
        if isinstance(ref, DataRef) and getattr(ref, "file", None):
            files.add(str(ref.file))

    for v in sym.variables:
        for src in v.dims:
            if src.kind in ("csv", "columns", "rows") and src.file:
                files.add(str(src.file))

    def _scan_expr(expr: SymExpr) -> None:
        for t in expr.terms:
            _add(t.ref)
            if t.bind is not None:
                _add(t.bind.ref)

    for c in sym.constraints:
        _scan_expr(c.lhs)
        if isinstance(c.for_each, ForEachRows):
            files.add(str(c.for_each.file))
        if isinstance(c.rhs, DataRef):
            _add(c.rhs)
        elif isinstance(c.rhs, SymExpr):
            _scan_expr(c.rhs)
    _scan_expr(sym.objective.expr)
    return files


def check_data_refs(sym: SymbolicIR, data: DataStore) -> None:
    """**展开前**校验：模型引用的每个列名都必须真实存在。

    动机：LLM 会猜列名（`Line ID` vs `Line_ID`）、把数据行当列名、或把占位符写进
    `column`。旧实现要等读到那一行才报**第一个**错，一次重写只能修一处。
    这里一次性列出所有列名问题（带候选/归一化提示），让一次重写修全。

    归一化能唯一命中的列名在这里会被**修复并记录**（`DataStore.repairs`），
    其余问题聚合成一条 `ExpandError`。
    """
    if not data.root.is_dir():
        return
    problems: list[str] = []

    def check(file: str, column: str, ctx: str, template_ok: bool = False) -> None:
        if template_ok and _has_placeholder(column):
            return          # 列模板由展开时按绑定替换，预检阶段不展开
        try:
            data._resolve_column(file, column)
        except ExpandError as exc:
            problems.append(f"{ctx}: {exc}")

    def scan_expr(expr: SymExpr, ctx: str) -> None:
        for t in expr.terms:
            if t.ref is not None:
                check(t.ref.file, t.ref.column, f"{ctx}的项 {t.var}", template_ok=True)
            if t.bind is not None:
                check(t.bind.ref.file, t.bind.ref.column, f"{ctx}的项 {t.var} 的 bind")

    for v in sym.variables:
        for src in v.dims:
            if src.kind == "csv" and src.file and src.column:
                check(src.file, src.column, f"变量族 {v.name} 的索引维")
            elif src.kind in ("columns", "rows") and src.file:
                try:
                    vals = _dim_values(src, data)
                except ExpandError as exc:
                    problems.append(f"变量族 {v.name} 的索引维: {exc}")
                else:
                    if not vals:
                        problems.append(
                            f"变量族 {v.name} 的索引维: {src.kind} 取自 {src.file} "
                            f"（prefix={src.prefix!r}）没有匹配到任何值"
                        )

    scan_expr(sym.objective.expr, "目标函数")
    for c in sym.constraints:
        ctx = f"约束 {c.name}"
        scan_expr(c.lhs, ctx)
        if isinstance(c.for_each, ForEachRows):
            for k in c.for_each.keys:
                check(c.for_each.file, k, f"{ctx} 的 for_each rows keys")
        if isinstance(c.rhs, DataRef):
            check(c.rhs.file, c.rhs.column, f"{ctx} 的 rhs", template_ok=True)
        elif isinstance(c.rhs, SymExpr):
            scan_expr(c.rhs, ctx)

    if not problems:
        return
    head = "模型引用的列名对不上（按【数据文件表头】逐条核对；column/key_column 必须逐字照抄）:"
    body = "\n  ".join(problems[:12])
    more = f"\n  …（共 {len(problems)} 处）" if len(problems) > 12 else ""
    raise ExpandError(head + "\n  " + body + more)


def check_data_files(sym: SymbolicIR, data: DataStore) -> None:
    """**展开前**校验：模型引用的数据文件必须真实存在。

    动机：LLM 会凭空造文件名（实测 `sorrell3` 该题没有任何数据声明，
    却被写出 `sequences.csv` / `lcs_conflicts.csv`）。早期实现要等读到
    那一行才报错，此时已经花掉一次完整 LLM 调用。

    这里一次性列出**所有**缺失文件 + 实际可用文件，报错可直接定位。

    `data_dir` 不存在时不拦截（把判断交给具体读取处，保持向后兼容）。
    """
    if not data.root.is_dir():
        return
    referenced = scan_data_refs(sym)
    if not referenced:
        return
    available = data.available_files()
    missing = sorted(f for f in referenced if _basename(f) not in available)
    if not missing:
        return
    raise ExpandError(
        "模型引用了**不存在的数据文件**（模型生成的文件名，题面里没有）：\n"
        f"  缺失: {missing}\n"
        f"  实际可用: {sorted(available) if available else '（该实例没有任何数据文件）'}\n"
        "  请只使用题面【数据文件清单】里列出的文件；"
        "若某个量确实不在数据里，应把它建成决策变量。"
    )


# ---------------- 索引维展开 ----------------


def _dim_values(src: IndexSource, data: DataStore) -> list[str]:
    """索引维的取值集合。

    - list：显式列举；
    - csv：某列去重（保序）；
    - columns：文件的**列名**（可加前缀过滤）——宽表；
    - rows：**行序** 1..N —— 无键列的转置/宽表。
    """
    if src.kind == "list":
        return [str(v) for v in (src.values or [])]
    assert src.file
    if src.kind == "csv":
        assert src.column
        vals = data.distinct(src.file, src.column)
        if not vals:
            raise ExpandError(f"索引维取自 {src.file}.{src.column}，但该列没有非空值")
        return vals
    if src.kind == "columns":
        names = data.column_names(src.file)
        if src.prefix:
            names = [n for n in names if n.startswith(src.prefix)]
        if not names:
            raise ExpandError(
                f"索引维用列名取自 {src.file}（prefix={src.prefix!r}），但没有匹配的列"
            )
        return names
    if src.kind == "rows":
        n = data.row_count(src.file)
        if n <= 0:
            raise ExpandError(f"索引维用行序取自 {src.file}，但文件没有数据行")
        return [str(i) for i in range(1, n + 1)]
    raise ExpandError(f"未知的索引维类型: {src.kind!r}")


def _family_dims(var: SymVar, data: DataStore) -> dict[str, list[str]]:
    """变量族的索引名 → 取值列表（索引名来自 `var.order`，支持自定义 `dim_names`）。"""
    out: dict[str, list[str]] = {}
    order = var.order
    for name, src in zip(order, var.dims):
        out[name] = _dim_values(src, data)
    return out


def _instance_name(base: str, bindings: dict[str, str], order: list[str]) -> str:
    """按变量族的维序拼接实例名（与 ir.Variable.instances 一致）。"""
    if not order:
        return base
    return base + "_" + "_".join(bindings[d] for d in order)


def _split_index_part(t: str) -> list[str]:
    """把 `{i}_{j}` / `i_j` / `{i}_1` 这类索引串拆成 ["i", "j"] / ["i","1"]。"""
    parts: list[str] = []
    buf = ""
    depth = 0
    for ch in t:
        if ch == "{":
            depth += 1
            buf += ch
        elif ch == "}":
            depth = max(0, depth - 1)
            buf += ch
        elif ch == "_" and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return [p[1:-1] if p.startswith("{") and p.endswith("}") else p for p in parts if p != ""]


def _split_instance(
    t: str, families: dict[str, Any] | None = None
) -> tuple[str, list[str]]:
    """把变量模板拆成 (基名, 索引列表)。

    **优先按已知变量族名做最长前缀匹配** —— 因为族名本身可含下划线
    （`x_start`、`net_flow`），盲目按第一个 `_` 切分会把 `x_start_{i}`
    误拆成 base=`x` + idx=`["start","i"]`（实测导致 'x' 未声明）。

    ```
    x_start_{i}  + families{x_start}  → ("x_start", ["i"])
    y_{i}_{j}                          → ("y", ["i", "j"])
    y_{i}_1                            → ("y", ["i", "1"])
    y_1_2                              → ("y", ["1", "2"])
    x                                  → ("x", [])
    ```
    """
    if families:
        for base in sorted(families, key=len, reverse=True):
            if t == base:
                return base, []
            if t.startswith(base + "_"):
                return base, _split_index_part(t[len(base) + 1:])

    # 回退：没有族表可用时，按第一个下划线分割
    if "_" not in t:
        return t, []
    base, _, rest = t.partition("_")
    return base, _split_index_part(rest)


def _resolve_index(
    name: str,
    d: str,
    dims: dict[str, list[str]],
    bound: dict[str, str],
    ctx: str,
) -> list[str]:
    """把**单个索引**解析成候选取值列表（空列表 = 全部越界，该项应跳过）。

    支持四种写法：
    - 维名（`k`）→ 该维全部取值（求和）；
    - 绑定值（for_each 提供的 `p`）→ 单值；
    - 字面值（`3` / `J1`）→ 单值；
    - **算术偏移**（`k-1`、`t28+1`、`k+2`）→ 基础值 ± 偏移量。

    算术偏移的边界语义：**越界即从候选中剔除**（该组合不存在）。
    例：颜色维取值 `1..6`，`{k-1}` 在 k=1 时得到 0，不在取值集合里 → 跳过。
    这与数据引用缺失时的 `on_missing: skip` 语义一致：工业模型里"相邻颜色/前一周期"
    在边界上本来就不存在，应视为该项不参与，而不是补一个 0 把模型扭歪。
    """
    m = re.fullmatch(r"([A-Za-z_]\w*|\d+)([+-]\d+)", name.strip())
    if m:
        base, delta = m.group(1), int(m.group(2))
    else:
        base, delta = name.strip(), 0

    # 确定基础取值集合
    # ① "维名 + 数字"（如 `t28`、`k3`）→ 视为该维的具体取值 "28"/"3"。
    #    LLM 常把「第 28 个周期」写成 t28，而不是 {t} 配 for_each。
    m2 = re.fullmatch(rf"{re.escape(d)}(\d+)", base)
    if m2:
        bases = [m2.group(1)]
    elif base in bound:
        bases = [bound[base]]
    elif base.isdigit():
        bases = [base]
    elif base == d:
        bases = list(dims[d])
    elif base in dims[d]:
        bases = [base]
    else:
        raise ExpandError(
            f"{ctx}: 第 {d} 维的索引 {name!r} 无法解析 —— 既不是该维名 {d!r}、"
            f"也不是该维的取值 {dims[d][:6]}、也没有被 for_each 绑定"
        )

    if delta == 0:
        return bases

    out: list[str] = []
    skipped_nonnum = 0
    for v in bases:
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            skipped_nonnum += 1
            continue
        cand = str(n + delta)
        if cand in dims[d]:
            out.append(cand)
    if skipped_nonnum and not out:
        # 该维取值不是数字，却写了算术偏移 → 这是建模错误，明确报出
        raise ExpandError(
            f"{ctx}: 索引 {name!r} 对第 {d} 维做了算术偏移，但该维取值不是数字"
            f"（{dims[d][:6]}）"
        )
    return out


def _expand_term_vars(
    var_template: str,
    families: dict[str, tuple[SymVar, dict[str, list[str]], list[str]]],
    bound: dict[str, str],
) -> list[tuple[str, dict[str, str]]]:
    """把项里的变量模板展开成 (实例名, 用于解析 DataRef 的绑定) 列表。

    **索引按位置对应变量族的维**（第 k 个索引 ↔ 第 k 维），每个索引由
    `_resolve_index` 解析成候选集合；任一维候选为空 → 整个项不产生实例
    （返回空列表，调用方据此跳过）。

    没有索引时（裸族名）→ 全部维求和。
    """
    base, idx = _split_instance(var_template, families)
    fam = families.get(base)
    if fam is None:
        raise ExpandError(
            f"项引用了未声明的变量族 {base!r}（模板 {var_template!r}；"
            f"已声明 {sorted(families)}）"
        )
    var, dims, order = fam

    if not order:
        if idx:
            raise ExpandError(
                f"项 {var_template!r}: 标量变量 {base!r} 不应带索引 {idx}"
            )
        return [(base, dict(bound))]

    if not idx:
        # 裸族名 → 全维求和
        combos = [dict(bound)]
        for d in order:
            nxt = []
            for c in combos:
                for v in dims[d]:
                    nxt.append({**c, d: v})
            combos = nxt
        return [(_instance_name(base, c, order), c) for c in combos]

    if len(idx) != len(order):
        raise ExpandError(
            f"项 {var_template!r}: 给了 {len(idx)} 个索引 {idx}，但变量族 {base!r} 有 "
            f"{len(order)} 维 {order}。每一维都要有一个索引（字面值也要写，如 y_{{i}}_{{1}}）"
        )

    candidates = [
        _resolve_index(name, d, dims, bound, f"项 {var_template!r}")
        for name, d in zip(idx, order)
    ]
    if any(not c for c in candidates):
        return []          # 某一维全部越界 → 该项不存在（由调用方计入 skipped）

    combos = [{}]
    for d, cands in zip(order, candidates):
        nxt = []
        for c in combos:
            for v in cands:
                nxt.append({**c, d: v})
        combos = nxt

    out: list[tuple[str, dict[str, str]]] = []
    for c in combos:
        binds = {**bound, **c}
        out.append((_instance_name(base, binds, order), binds))
    return out


def _objective_outlier(obj_expr: LinearExpr) -> str | None:
    """目标系数出现「big-M 级」离群值时报警（实测 neckar：5 个 5e5 系数写进目标）。

    判据：|coef| 最大值 ≥ 1e5 且 ≥ 100 × 中位数。固定费用 vs 单位费用这种
    真实量级差（实测 drayage 13 倍、mik 6 倍）不会命中。
    """
    coefs = sorted(abs(t.coef) for t in obj_expr.terms if t.coef)
    if len(coefs) < 5:
        return None
    med = coefs[len(coefs) // 2]
    mx = coefs[-1]
    if mx >= 1e5 and mx >= 100 * max(med, 1e-12):
        return (
            f"目标函数系数出现极端量级：最大 |coef|={mx:g}，中位数={med:g}"
            f"（相差 {mx / max(med, 1e-12):.0f} 倍）。big-M / 惩罚系数通常只应出现在**约束**里；"
            "请检查是否把 big-M 写进了目标（这会让目标值量级失控）。"
        )
    return None


def _missed_key_message(missed: list[tuple[str, str, list[str], dict[str, str]]],
                        data: DataStore) -> str:
    """把「整条约束因键不存在被跳过」变成可行动的错误（带真实键取值示例）。"""
    lines = [
        f"{len(missed)} 条约束因为数据键取不到值而整条被跳过"
        "（默认 on_missing=\"skip\" 会把它们静默丢掉，这里判为建模失败）：",
    ]
    for ctx, file, kcs, desc in missed[:3]:
        sample = ""
        try:
            vals = data.distinct(file, kcs[0])
            shown = ", ".join(repr(v) for v in vals[:8])
            sample = f"；{kcs[0]} 的实际取值示例: [{shown}]（共 {len(vals)} 个）"
        except ExpandError:
            pass
        lines.append(f"  - {ctx}：{file} 的键 {desc or kcs} 不存在{sample}")
    if len(missed) > 3:
        lines.append(f"  …（共 {len(missed)} 条，以上只列前 3 条）")
    lines.append(
        "这通常说明 key_value 的写法与文件里的键风格不一致（例如把列名 `Task_0` 当键值，"
        "而文件里是 `0`）。请按上面的真实取值改 key_value；不确定时先在 assumptions 里写明。"
    )
    return "\n".join(lines)


def expand(
    sym: SymbolicIR,
    data: DataStore,
    *,
    max_instances: int = DEFAULT_MAX_INSTANCES,
) -> IR:
    """把符号模型展开成展平 IR（系数全部解析成字面值）。"""
    # 最先做虚构文件预检：LLM 常凭空造文件名，越早拦越省（一次 LLM 调用不便宜）
    check_data_files(sym, data)
    # 其次做列名预检（一次性列全），避免「一轮只修一个列名笔误」
    check_data_refs(sym, data)

    probs = sym.validate_placeholders()
    if probs:
        raise ExpandError("符号模型占位符校验失败:\n  " + "\n  ".join(probs))

    families: dict[str, tuple[SymVar, dict[str, list[str]], list[str]]] = {}
    for v in sym.variables:
        dims = _family_dims(v, data)
        families[v.name] = (v, dims, v.order)

    total = 1
    for name, (v, dims, order) in families.items():
        n = 1
        for d in order:
            n *= len(dims[d])
        total += n
        if n > max_instances:
            raise ExpandError(
                f"变量族 {name} 需展开 {n} 个实例，超过上限 {max_instances}。"
                "该规模应改用分块建模，或人工拆分后分别求解。"
            )
    if total > max_instances:
        raise ExpandError(
            f"模型总实例数 {total} 超过上限 {max_instances}，请拆分后再展开。"
        )

    # ---- 变量声明 ----
    variables: list[Variable] = []
    family_combos: dict[str, list[list[str]]] = {}      # 族名 → 已声明组合（供后续补齐）
    for name, (v, dims, order) in families.items():
        if not order:
            variables.append(
                Variable(name=name, var_type=v.var_type, index_set=None,
                         lower=v.lower, upper=v.upper)
            )
            family_combos[name] = []
            continue
        combos: list[list[str]] = [[]]
        for d in order:
            combos = [c + [val] for c in combos for val in dims[d]]
        family_combos[name] = combos
        index_set: list[Any]
        if len(order) == 1:
            index_set = [c[0] for c in combos]          # 一维：扁平列表（与 ir 约定一致）
        else:
            index_set = combos                          # 多维：组合列表
        variables.append(
            Variable(name=name, var_type=v.var_type, index_set=index_set,
                     lower=v.lower, upper=v.upper)
        )
        if len(variables[-1].instances()) != len(combos):
            raise ExpandError(f"变量族 {name} 实例名展开异常")

    declared = {inst for var in variables for inst in var.instances()}
    # 补齐记录：族名 → 新增的索引组合（LLM 引用了笛卡尔积里没有的组合）
    auto_added: dict[str, list[list[str]]] = {}

    # ---- 表达式展开 ----

    skipped: list[str] = []      # 因数据为空而跳过的项（on_empty="skip"）
    # 因「键在文件里根本不存在」而被整条跳过的约束 rhs：记录上下文，用于事后判失败
    missed_rhs: list[tuple[str, str, list[str], dict[str, str]]] = []



    def build_expr(expr: SymExpr, bound: dict[str, str], ctx: str) -> LinearExpr:
        terms: list[Term] = []
        for t in expr.terms:
            # 按分类值分派：先把数据里的**字符串值**绑成一个索引变量，
            # 再展开变量模板（`hire_{rtype}` → `hire_Techniker`）。
            local_bound = dict(bound)
            if t.bind is not None:
                local_bound[t.bind.as_] = data.lookup_raw(t.bind.ref, local_bound)

            expanded = _expand_term_vars(t.var, families, local_bound)
            if not expanded:
                # 索引算术越界（如 k-1 在 k=1 时）→ 该组合不存在，跳过该项
                skipped.append(f"{ctx}:{t.var}(索引越界)")
                continue
            for inst, binds in expanded:
                if inst not in declared:
                    # **自动补齐**：LLM 常引用"笛卡尔积里没有"的索引组合
                    # （如声明了 xs_{t1}_{k}_{c} 却写 xs_1_2_1，而该组合不在取值集合里）。
                    # 早期实现直接报错，整题失败；实测 chromaticindex32-8 就卡在这里。
                    # 现改为把该组合补进变量族 —— 它与"漏声明一个变量"等价，
                    # 补齐后模型仍可求解，并在 assumptions 留痕供复核。
                    _base, _ = _split_instance(t.var, families)
                    _fam = families.get(_base)
                    _order = _fam[2] if _fam else []
                    if _order and all(binds.get(d) is not None for d in _order):
                        _combo = [binds[d] for d in _order]
                        if _combo not in family_combos.get(_base, []):
                            auto_added.setdefault(_base, []).append(_combo)
                            family_combos.setdefault(_base, []).append(_combo)
                            declared.add(inst)
                        else:
                            raise ExpandError(
                                f"{ctx}: 引用未声明的实例 {inst!r}"
                                f"（变量族 {t.var} 的组合已存在但未展开，属内部错误）"
                            )
                    else:
                        raise ExpandError(
                            f"{ctx}: 引用未声明的实例 {inst!r}"
                            f"（变量族 {t.var} 的索引组合不在范围内，且无法自动补齐）"
                        )
                if t.ref is not None:
                    # 数据引用取到的值；若同时给了 coef，则作为倍数（`coef × ref值`）
                    val = data.lookup(t.ref, binds)
                    if val is None:
                        # 该键的取值为空且 on_empty="skip"（工业数据里表示"该组合不可用"）
                        skipped.append(f"{ctx}:{inst}")
                        continue
                    coef = val
                    if t.coef is not None:
                        coef *= float(t.coef)
                else:
                    coef = float(t.coef)  # type: ignore[arg-type]
                terms.append(Term(var=inst, coef=coef))
        return LinearExpr(terms=terms, constant=expr.constant)

    # ---- 目标 ----
    obj_expr = build_expr(sym.objective.expr, {}, "目标函数")
    if not obj_expr.terms and not obj_expr.constant:
        raise ExpandError("目标函数展开后为空（既无项也无常数），属建模失败")

    objective = Objective(sense=sym.objective.sense, expr=obj_expr)
    # 目标退化成常数（或所有项都被跳过）→ 建模失败，不能拿去和参考值比
    if not obj_expr.terms and sym.variables:
        raise ExpandError(
            f"目标函数展开后不含任何变量项（常数部分 {obj_expr.constant}）。"
            "目标必须至少含一个决策变量项；这通常说明目标项的 ref 全被跳过，"
            "或把目标写成了常数/占位（如只写固定成本 C_fixed）。"
        )
    _outlier = _objective_outlier(obj_expr)
    if _outlier:
        raise ExpandError(_outlier)

    # ---- 约束 ----
    def rhs_value(rhs, bound: dict[str, str], ctx: str) -> tuple[float, list[Term]]:
        """返回 (rhs 数值, 需要移到左边的项)。

        - 数值 → 直接用；
        - DataRef → 查表；
        - VarRef → `lhs <= c*v` ⇔ `lhs - c*v <= 0`；
        - SymExpr → `lhs <= (Σc_i v_i + k)` ⇔ `lhs - Σc_i v_i <= k`。
        """
        from .symbolic import VarRef

        if isinstance(rhs, SymExpr):
            expr = build_expr(rhs, bound, ctx)
            return expr.constant, [Term(var=t.var, coef=-t.coef) for t in expr.terms]
        if isinstance(rhs, VarRef):
            moved: list[Term] = []
            moved_insts = _expand_term_vars(rhs.var, families, bound)
            if not moved_insts:
                # rhs 的变量索引全越界 → 整条约束不适用
                return None, []
            for inst, _binds in moved_insts:
                if inst not in declared:
                    raise ExpandError(f"{ctx}: rhs 引用了未声明的实例 {inst!r}")
                moved.append(Term(var=inst, coef=-rhs.coef))
            return 0.0, moved
        if isinstance(rhs, DataRef):
            v = data.lookup(rhs, bound)
            # None = 键不存在/值为空（skip 语义）→ 整条约束不适用，调用方跳过。
            # 但「整条约束家族都查不到」几乎一定是 key_value 风格写错（如列名 Task_0
            # 当成键值，而文件里是 0），不能静默丢掉 —— 记下来，事后判失败并给真实键示例。
            if v is None:
                try:
                    kcs = [data._resolve_column(rhs.file, kc) for kc in rhs.key_columns]
                    desc = dict(zip(kcs, data._resolve_key_values(rhs, bound)))
                except ExpandError:
                    desc = {}
                missed_rhs.append((ctx, str(rhs.file), list(rhs.key_columns), desc))
            return v, []
        return float(rhs), []

    constraints: list[Constraint] = []
    for con in sym.constraints:
        from .symbolic import ForEachRows

        if isinstance(con.for_each, ForEachRows):
            # 跨表关联：按数据文件的**行**遍历，把列绑定成索引变量。
            # 例：precedence.csv(predecessor_id → successor_id) 绑定成 {p}/{s}，
            # 从而能写「对每一对 (前驱, 后继) 生成一条优先级约束」。
            spec = con.for_each
            rows = data.rows(spec.file, spec.keys)
            if not rows:
                raise ExpandError(
                    f"约束 {con.name}: for_each 引用的 {spec.file} 没有数据行"
                )
            for row in rows:
                bound = {n: row[k] for n, k in zip(spec.names, spec.keys)}
                name = _PLACEHOLDER.sub(lambda m: bound.get(m.group(1), m.group(0)), con.name)
                lhs = build_expr(con.lhs, bound, f"约束 {name}")
                rhs, moved = rhs_value(con.rhs, bound, f"约束 {name}")
                if rhs is None:
                    skipped.append(f"{name}:rhs")
                    continue
                if moved:
                    lhs = LinearExpr(terms=list(lhs.terms) + moved, constant=lhs.constant)
                constraints.append(
                    Constraint(name=name, sense=con.sense, lhs=lhs, rhs=rhs)
                )
        elif isinstance(con.for_each, list):
            # 多索引笛卡尔积：对 (i,t) 的每个组合生成一条约束
            import itertools

            names = con.for_each
            srcs = []
            for n in names:
                src = sym.index_names.get(n)
                if src is None:
                    raise ExpandError(
                        f"约束 {con.name}: for_each 里的 {n!r} 不是任何变量族的索引维"
                    )
                srcs.append(_dim_values(src, data))
            for combo in itertools.product(*srcs):
                bound = dict(zip(names, combo))
                name = _PLACEHOLDER.sub(
                    lambda m: bound.get(m.group(1), m.group(0)), con.name
                )
                lhs = build_expr(con.lhs, bound, f"约束 {name}")
                rhs, moved = rhs_value(con.rhs, bound, f"约束 {name}")
                if rhs is None:
                    skipped.append(f"{name}:rhs")
                    continue
                if moved:
                    lhs = LinearExpr(terms=list(lhs.terms) + moved, constant=lhs.constant)
                constraints.append(
                    Constraint(name=name, sense=con.sense, lhs=lhs, rhs=rhs)
                )
        elif con.for_each:
            src = sym.index_names.get(con.for_each)
            if src is None:
                raise ExpandError(
                    f"约束 {con.name}: for_each={con.for_each!r} 不是任何变量族的索引维"
                )
            values = _dim_values(src, data)
            for val in values:
                bound = {con.for_each: val}
                name = _PLACEHOLDER.sub(lambda m: val, con.name)
                lhs = build_expr(con.lhs, bound, f"约束 {name}")
                rhs, moved = rhs_value(con.rhs, bound, f"约束 {name}")
                if rhs is None:
                    skipped.append(f"{name}:rhs")
                    continue
                if moved:
                    lhs = LinearExpr(terms=list(lhs.terms) + moved, constant=lhs.constant)
                constraints.append(
                    Constraint(name=name, sense=con.sense, lhs=lhs, rhs=rhs)
                )
        else:
            lhs = build_expr(con.lhs, {}, f"约束 {con.name}")
            rhs, moved = rhs_value(con.rhs, {}, f"约束 {con.name}")
            if rhs is None:
                skipped.append(f"{con.name}:rhs")
                continue
            if moved:
                lhs = LinearExpr(terms=list(lhs.terms) + moved, constant=lhs.constant)
            constraints.append(
                Constraint(name=con.name, sense=con.sense, lhs=lhs, rhs=rhs)
            )

    # ---- 退化/静默丢失守卫 ----
    # ① 整条约束因「键不存在」被跳过、且一条都没建出来：不能当成正常 skip。
    #    实测 f2gap40400：key_value 写成 Task_0（列名），文件里是 0，40 条覆盖约束全被丢，
    #    最后剩一个无约束的"最小工资"模型 —— 解可行、目标 -84，与真题差 200 倍。
    if missed_rhs and not constraints:
        raise ExpandError(_missed_key_message(missed_rhs, data))
    # ② 符号模型声明了约束，展开后一条都没有 → 建模失败
    if sym.constraints and not constraints:
        raise ExpandError(
            f"展开后一条约束都没有（符号模型声明了 {len(sym.constraints)} 条，"
            "全部被跳过或展开为空）。这通常意味着数据键取不到值、索引越界、"
            "或约束体为空 —— 请检查数据引用与索引集合，不要依赖 skip 让约束消失。"
        )

    # 补齐的实例要真正加进变量声明（否则求解器不认这些变量）
    if auto_added:
        for v in variables:
            extra = auto_added.get(v.name)
            if not extra:
                continue
            order_v = families[v.name][2]
            if len(order_v) == 1:
                merged = list(v.index_set or []) + [c[0] for c in extra]
            else:
                merged = list(v.index_set or []) + [list(c) for c in extra]
            v.index_set = merged

    # 约束名去重：
    # `for_each` 展开时，若约束名模板里没放索引占位符（LLM 常见疏漏），
    # 会生成多条同名约束；而 `IR` 校验要求约束名唯一，整题会直接失败。
    # 这里就地加数字后缀，并把次数记入 assumptions（可审计，不静默吞掉）。
    deduped: list[Constraint] = []
    seen_names: dict[str, int] = {}
    dup_renamed = 0
    for c in constraints:
        if c.name in seen_names:
            seen_names[c.name] += 1
            dup_renamed += 1
            c = c.model_copy(update={"name": f"{c.name}_{seen_names[c.name]}"})
        else:
            seen_names[c.name] = 1
        deduped.append(c)

    assumptions = list(sym.assumptions)
    if data.repairs:
        uniq = sorted(set(data.repairs))
        detail = "; ".join(f"{Path(f).name}: {a!r} → {b!r}" for f, a, b in uniq[:6])
        if len(uniq) > 6:
            detail += f" 等 {len(uniq)} 处"
        assumptions.append(
            f"[展开器] 列名按归一化规则（忽略大小写/空格/连字符）匹配到实际列：{detail}"
        )
    if auto_added:
        _n = sum(len(v) for v in auto_added.values())
        _detail = {k: len(v) for k, v in auto_added.items()}
        assumptions.append(
            f"[展开器] 自动补齐了 {_n} 个变量实例（模型引用了笛卡尔积里没有的组合）："
            f"{_detail}。若这些组合本不该存在，说明索引维取值或约束条件有误。"
        )
    if dup_renamed:
        assumptions.append(
            f"[展开器] {dup_renamed} 条约束与已有约束同名，已自动加后缀重命名"
            "（原模板可能漏写了索引占位符）"
        )
    if missed_rhs:
        _ex = "; ".join(f"{ctx}（键 {desc or kcs}）"
                        for ctx, _f, kcs, desc in missed_rhs[:3])
        assumptions.append(
            f"[展开器] {len(missed_rhs)} 条约束因数据键不存在被跳过（on_missing=skip）：{_ex}"
            "。若这些约束本应存在，说明 key_value 的取值风格与文件不一致（如 Task_0 vs 0）。"
        )
    if skipped:
        assumptions.append(
            f"[展开器] {len(skipped)} 项因数据缺失/为空/索引越界被跳过"
            f"（on_empty/on_missing=skip），例：{skipped[:3]}"
        )

    return IR(
        variables=variables,
        objective=objective,
        constraints=deduped,
        description=sym.description,
        assumptions=assumptions,
    )
