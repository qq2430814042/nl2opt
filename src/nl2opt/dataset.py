"""数据集：统一加载器（JSONL / MIPLIB-NL 实例目录）。

两种输入形态：

1. **JSONL**（每行一条）——适合自建小样例集：
```
{"id": "...", "problem_type": "milp", "nl_text": "...", "expected": {...}}
```

2. **MIPLIB-NL 实例目录** —— 当前主用数据集，见 `load_miplibnl_instance()`。
   每个实例是 `instance.json` + `data/*.csv`，数值在文件里而不在题面，
   因此走「符号化 IR → 展开」链路（`symbolic.py` / `expand.py`）。

> 早期还有一个「内置样例集」，
> 已随「换用 MIPLIB-NL」删除 —— 新数据集每题都靠 LLM 建模，不存在内置答案。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from .csvschema import looks_like_data, read_first_row, synthetic_columns

# 「表头摘要」只读每个文件的第一行（列名），行数据/取值/行数一律不读。
HEADER_MAX_COLS = 12


def _header_digest_lines(files: dict[str, Any], base: Path) -> list[str]:
    """为【数据文件表头】生成逐文件说明（只列列名/位置列名，不给行数据）。"""
    lines: list[str] = []
    for spec in files.values():
        if not isinstance(spec, dict):
            continue
        raw = str(spec.get("path", "") or "")
        if not raw:
            continue
        name = Path(raw.replace("\\", "/")).name
        cand = [base / raw, base / name]
        if base.parent != base:
            cand.append(base.parent / raw)
        path = next((c for c in cand if c.is_file()), None)
        if path is None:
            continue
        cells = read_first_row(path)
        if not cells:
            continue
        if looks_like_data(cells):
            cols = synthetic_columns(len(cells))
            shown = ", ".join(cols[:HEADER_MAX_COLS])
            if len(cols) > HEADER_MAX_COLS:
                shown += f", …（共 {len(cols)} 列）"
            else:
                shown += f"（{len(cols)} 列）"
            lines.append(
                f"- `{raw}`：⚠ 无表头（首行即数据），共 {len(cells)} 列；"
                f"位置列名 `col_1`…`col_{len(cells)}`：{shown}"
            )
            continue
        shown = ", ".join(cells[:HEADER_MAX_COLS])
        if len(cells) > HEADER_MAX_COLS:
            shown += f", …（共 {len(cells)} 列）"
        else:
            shown += f"（{len(cells)} 列）"
        lines.append(f"- `{raw}`：{shown}")
    return lines



class Sample(BaseModel):
    """单条样例（训练/评测通用）。"""

    id: str
    nl_text: str
    problem_type: str = Field(default="milp", description="lp | milp（仅供参考）")
    expected: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)
    trusted_reference: bool = Field(
        default=True,
        description="参考答案是否可信。内置样例(有黄金IR)为 True；外部数据集为 False，"
        "此时若解可行但目标值不符，容忍为'疑似标注错误'而非触发重写。",
    )


class Dataset:
    """样例集合，可切片、可迭代。"""

    def __init__(self, samples: list[Sample], name: str = "") -> None:
        self.samples = samples
        self.name = name

    def __len__(self) -> int:
        return len(self.samples)

    def __iter__(self) -> Iterator[Sample]:
        return iter(self.samples)

    def __getitem__(self, idx: int | slice) -> Sample | "Dataset":
        if isinstance(idx, slice):
            return Dataset(self.samples[idx], name=self.name)
        return self.samples[idx]


def load_jsonl(path: str | Path, name: str = "") -> Dataset:
    """从 JSONL 文件加载样例集。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"数据集不存在: {p}")
    samples: list[Sample] = []
    with p.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                data = json.loads(line)
                samples.append(Sample.model_validate(data))
            except Exception as exc:
                raise ValueError(f"{p} 第 {lineno} 行解析失败: {exc}") from exc
    return Dataset(samples, name=name or str(p))


def load_any(path: str | Path | None) -> Dataset:
    """智能加载：目录含 instance.json → MIPLIB-NL；目录下多实例 → 整批；否则按 JSONL。

    `path=None` 会报错 —— 内置样例集已删除，不再有「默认数据集」这种东西。
    """
    if path is None:
        raise ValueError(
            "必须显式指定数据集路径。内置样例集已删除（换用 MIPLIB-NL），"
            "请用 --data 指向 JSONL 文件或 MIPLIB-NL 实例目录，"
            "例如：--data data/MIPLIB-NL/dataset/b-ball"
        )
    p = Path(path)
    if p.is_dir():
        if (p / "instance.json").exists():
            return load_miplibnl_instance(p)
        if any((d / "instance.json").exists() for d in p.iterdir() if d.is_dir()):
            return load_miplibnl_dataset(p)
    return load_jsonl(p)


# ---------------------------------------------------------------- MIPLIB-NL

def load_miplibnl_instance(instance_dir: str | Path, name: str = "") -> "Dataset":
    """加载**单个** MIPLIB-NL 实例目录（`instance.json` + `data/`）。

    产出 1 条 `Sample`，关键字段：
    - `nl_text`：`abstract_problem`，并把 `{param}` 占位符替换成实际值
      （LLM 需要知道规模，否则无法定索引集；数值本身仍在 CSV 里）；
    - `extra["data_dir"]`：数据目录绝对路径 —— `expand()` 从这里读 CSV；
    - `extra["file_schema"]`：各数据文件的列说明（**不给全量数据**，只给 schema）；
    - `expected["objective_value"]`：`optimal_value`；
    - `extra["miplibnl"]`：原始元信息，便于追溯。
    """
    d = Path(instance_dir)
    ij = d / "instance.json"
    if not ij.exists():
        raise FileNotFoundError(f"缺少 instance.json: {d}")
    meta = json.loads(ij.read_text(encoding="utf-8"))

    nl = str(meta.get("abstract_problem") or "")
    params = meta.get("parameters") or {}
    # 占位符替换：{n} / {time_limit} 等 → 实际值（LLM 需要知道规模）
    for k, v in params.items():
        nl = nl.replace("{" + str(k) + "}", str(v))

    files = meta.get("files") or {}
    schema_lines: list[str] = []
    for key, spec in files.items():
        if not isinstance(spec, dict):
            continue
        schema_lines.append(
            f"- {key}: 文件 `{spec.get('path', '?')}`\n  {spec.get('description', '')}"
        )
    if schema_lines:
        nl = nl.rstrip() + "\n\n【数据文件清单（数值在文件里，不在题面）】\n" + "\n".join(schema_lines)

    # 表头摘要（列名逐字可见，行数据不可见）。
    # 动机：instance.json 的 files 只有散文描述，模型只能「猜」列名 ——
    # 实测把 `Line_ID` 写成 `Line ID`、把数据行当成列名（`可用列: ['8.558...']`）。
    # 列名属于 schema，不是行数据；这里只读第一行，不读行数/取值。
    header_base = d / "data" if (d / "data").is_dir() else d
    header_lines = _header_digest_lines(files, header_base)
    if header_lines:
        nl = nl.rstrip() + (
            "\n\n【数据文件表头（列名；写 column/key_column 时必须逐字照抄，"
            "行数据不提供）】\n" + "\n".join(header_lines)
        )

    # 规模参数必须让 LLM 看到（否则它无法确定索引集大小）。
    # 题面里有些是 `{n}` 占位符（已替换），有些是裸写的 `N1`（如 b-ball），
    # 所以统一再附一张参数表，避免两种写法漏掉一种。
    if params:
        plines = [f"- {k} = {v}" for k, v in params.items()]
        nl = nl.rstrip() + "\n\n【实例参数（规模与系数）】\n" + "\n".join(plines)

    data_dir = d / "data"

    # ⚠️ 刻意**不**读取任何 CSV 行数据（行数 / 列值 / 取值范围都不读）。
    # 例外：上面【数据文件表头】只读第一行的列名 —— 列名是 schema，不是数据。
    # 其余仍只看 `instance.json` 里除 `optimal_value` 之外的字段：
    #   abstract_problem（题面）+ files（文件名/路径/描述）+ parameters（参数）。
    # 这是评测的公平性边界：数据长什么样，应该由模型自己通过 DataRef 去读，
    # 而不是由框架提前替它总结（那等于把「看一眼数据」的便宜也送了）。

    extra: dict = {
        "data_dir": str(data_dir.resolve()) if data_dir.exists() else str(d.resolve()),
        "data_files": {k: (v or {}).get("path") for k, v in files.items() if isinstance(v, dict)},
        "file_schema": files,
        "file_header_digest": header_lines,   # 便于追溯：模型实际看到的列名摘要
        "parameters": params,
        "miplibnl": {
            "instance": d.name,
            "source": "MIPLIB-NL (ICML 2026)",
        },
    }
    if data_dir.exists():
        extra["data_file_names"] = sorted(p.name for p in data_dir.glob("*") if p.is_file())

    expected: dict = {}
    opt = meta.get("optimal_value")
    if opt is not None:
        try:
            expected["objective_value"] = float(opt)
        except (TypeError, ValueError):
            expected["objective_value"] = opt

    return Dataset(
        [Sample(
            id=d.name,
            nl_text=nl,
            problem_type="milp",
            expected=expected,
            extra=extra,
            trusted_reference=False,   # 与原论文口径一致：未逐题独立核验
        )],
        name=name or d.name,
    )


def load_miplibnl_dataset(root: str | Path, name: str = "") -> "Dataset":
    """加载 MIPLIB-NL 根目录下的**全部**实例（每个子目录一个实例）。"""
    r = Path(root)
    if not r.is_dir():
        raise NotADirectoryError(f"不是目录: {r}")
    subdirs = sorted(d for d in r.iterdir() if d.is_dir() and (d / "instance.json").exists())
    if not subdirs:
        raise FileNotFoundError(f"{r} 下没有含 instance.json 的子目录")
    samples: list[Sample] = []
    for d in subdirs:
        samples.extend(load_miplibnl_instance(d).samples)
    return Dataset(samples, name=name or f"miplibnl:{r.name}")
