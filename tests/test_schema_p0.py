"""P0：表头摘要 + 列名归一化 + 展开前列名预检。

背景（runs20 实测）：6 题失败在「数据引用/键占位符错误」，根因是
① 列名靠猜（`Line ID` vs `Line_ID`）；② 无表头文件把数据行当列名；
③ 占位符写进 `column`；④ 一轮只能修一个列名笔误。
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nl2opt.dataset import load_miplibnl_instance  # noqa: E402
from nl2opt.expand import (  # noqa: E402
    DataStore,
    ExpandError,
    check_data_refs,
    expand,
    load_data_dir,
)
from nl2opt.ir import Sense, VarType  # noqa: E402
from nl2opt.symbolic import (  # noqa: E402
    DataRef,
    IndexSource,
    SymConstraint,
    SymExpr,
    SymObjective,
    SymbolicIR,
    SymTerm,
    SymVar,
)


@pytest.fixture()
def store(tmp_path: Path) -> DataStore:
    d = tmp_path / "data"
    d.mkdir()
    with (d / "lines.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Line_ID", "Big_M_Capacity"])
        w.writerow(["L1", "10"])
        w.writerow(["L2", "20"])
    with (d / "matrix.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["C0000000", "8.558", "3.789"])   # 首行是数据，不是表头
    return DataStore(d)


def test_column_normalization_repairs_unique_match(store: DataStore):
    """`Line ID` / `line-id` 归一化后唯一命中 `Line_ID`，并记入 repairs。"""
    assert store.column("lines.csv", "Line ID") == ["L1", "L2"]
    assert store.column("lines.csv", "line-id") == ["L1", "L2"]
    assert store.repairs == [
        ("lines.csv", "Line ID", "Line_ID"),
        ("lines.csv", "line-id", "Line_ID"),
    ]


def test_column_normalization_ambiguous_is_error(tmp_path: Path):
    """归一化后命中多列 → 判为歧义，不猜。"""
    d = tmp_path / "data"
    d.mkdir()
    with (d / "t.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Line ID", "line_id"])   # 两列归一化相同
        w.writerow(["a", "b"])
    store = DataStore(d)
    with pytest.raises(ExpandError) as ei:
        store.column("t.csv", "Line_ID")
    assert "共 2 列" in str(ei.value)


def test_missing_column_error_has_suggestion_and_truncation(tmp_path: Path):
    """未命中时错误要带最接近候选，且宽表列名列表要截断。"""
    d = tmp_path / "data"
    d.mkdir()
    cols = ["col_%03d" % i for i in range(300)]
    with (d / "wide.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerow(["1"] * len(cols))
    store = DataStore(d)
    with pytest.raises(ExpandError) as ei:
        store.column("wide.csv", "col_1")
    msg = str(ei.value)
    assert "共 300 列" in msg
    assert "最接近的列名" in msg


def test_column_template_with_unbound_placeholder_is_clear(store: DataStore):
    """列模板可用（P1），但占位符未绑定时要给出明确错误。"""
    sym = SymbolicIR(
        variables=[SymVar(name="x", var_type=VarType.binary, dims=[
            IndexSource(kind="csv", file="lines.csv", column="Line ID"),
        ])],
        objective=SymObjective(sense=Sense.maximize, expr=SymExpr(terms=[
            SymTerm(var="x_{i}", coef=1.0),
        ])),
        constraints=[SymConstraint(
            name="c_{i}", sense=Sense.le,
            lhs=SymExpr(terms=[SymTerm(
                var="x_{i}",
                ref=DataRef(file="lines.csv", column="Task_{nope}",
                            key_column="Line_ID", key_value="{i}"),
            )]),
            rhs=100.0,
        )],
    )
    with pytest.raises(ExpandError) as ei:
        expand(sym, store)
    assert "未绑定" in str(ei.value) and "nope" in str(ei.value)


def test_check_data_refs_reports_all_columns_at_once(store: DataStore):
    """多个列名错误要一次列全（而不是报第一个）。"""
    sym = SymbolicIR(
        variables=[SymVar(name="x", var_type=VarType.binary, dims=[
            IndexSource(kind="csv", file="lines.csv", column="Line IDE"),
        ])],
        objective=SymObjective(sense=Sense.maximize, expr=SymExpr(terms=[
            SymTerm(var="x_{i}", coef=1.0,
                    ref=DataRef(file="lines.csv", column="BigMCapacity",
                                key_column="Line_ID", key_value="{i}")),
        ])),
        constraints=[SymConstraint(
            name="c_{i}", sense=Sense.le,
            lhs=SymExpr(terms=[SymTerm(var="x_{i}", coef=1.0)]),
            rhs=DataRef(file="lines.csv", column="nope_col",
                        key_column="Line_ID", key_value="{i}"),
        )],
    )
    with pytest.raises(ExpandError) as ei:
        check_data_refs(sym, store)
    msg = str(ei.value)
    assert "Line IDE" in msg and "BigMCapacity" in msg and "nope_col" in msg


def test_header_digest_in_prompt(tmp_path: Path):
    """表头进入 nl_text；无表头文件只标注，不泄露数据值。"""
    root = tmp_path / "inst"
    data = root / "data"
    data.mkdir(parents=True)
    with (data / "lines.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Line_ID", "Fixed_Cost_Bonus"])
        w.writerow(["L1", "5"])
    with (data / "matrix.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["C0000000", "8.558", "3.789"])
        w.writerow(["C0000001", "1.0", "2.0"])
    (root / "instance.json").write_text(json.dumps({
        "abstract_problem": "make something {n}",
        "parameters": {"n": 2},
        "files": {
            "lines": {"path": "data/lines.csv", "description": "line attributes"},
            "matrix": {"path": "data/matrix.csv", "description": "wide matrix"},
        },
        "optimal_value": 1.0,
    }, ensure_ascii=False), encoding="utf-8")

    ds = load_miplibnl_instance(root)
    nl = ds.samples[0].nl_text
    assert "Line_ID" in nl and "Fixed_Cost_Bonus" in nl
    assert "无表头（首行即数据）" in nl
    assert "col_1" in nl and "col_3" in nl
    assert "8.558" not in nl, "无表头文件的数据值不能进 prompt"
