"""P1/P2：列模板、列名索引、行序索引 + 单元格、无表头位置列名。

背景（runs6_p05 实测）：
- prod2 / gen-ip002 / kaihu 卡在「无表头 / 宽表」：模型要么写 `column: "{j}"` 被拒，
  要么把整族约束留空（目标退化成 0/常数）。
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nl2opt.dataset import load_miplibnl_instance  # noqa: E402
from nl2opt.expand import DataStore, expand  # noqa: E402
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
        w.writerow(["Employee_ID", "Task_0", "Task_1"])
        w.writerow(["0", "5", "7"])
        w.writerow(["1", "3", "9"])
    # 无表头（首行即数据）
    with (d / "vals.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["C0001", "10", "0"])
        w.writerow(["C0002", "20", "1"])
    return DataStore(d)


def test_headerless_synthetic_columns(store: DataStore):
    """无表头文件用位置列名 col_1..col_n，且首行按数据读。"""
    assert store.is_headerless("vals.csv") is True
    assert store.column_names("vals.csv") == ["col_1", "col_2", "col_3"]
    assert store.row_count("vals.csv") == 2
    assert store.column("vals.csv", "col_2") == ["10", "20"]
    assert store.column("vals.csv", "col_1") == ["C0001", "C0002"]


def test_column_template(store: DataStore):
    """`column: "Task_{t}"` 按 for_each 绑定替换。"""
    sym = SymbolicIR(
        variables=[
            SymVar(name="z", var_type=VarType.binary,
                   dims=[IndexSource(kind="list", values=["0", "1"])]),
        ],
        objective=SymObjective(sense=Sense.maximize, expr=SymExpr(terms=[
            SymTerm(var="z_{i}", coef=1.0),
        ])),
        constraints=[SymConstraint(
            name="c_{i}", sense=Sense.le,
            for_each="i",
            lhs=SymExpr(terms=[SymTerm(
                var="z_{i}",
                ref=DataRef(file="lines.csv", column="Task_{i}",
                            key_column="Employee_ID", key_value="0"),
            )]),
            rhs=100.0,
        )],
    )
    ir = expand(sym, store)
    coefs = {t.var: t.coef for c in ir.constraints for t in c.lhs.terms}
    assert coefs == {"z_0": 5.0, "z_1": 7.0}, coefs


def test_index_source_columns(store: DataStore):
    """`kind: "columns"` 用列名做索引（可加 prefix）。"""
    sym = SymbolicIR(
        variables=[SymVar(name="w", var_type=VarType.binary, dims=[
            IndexSource(kind="columns", file="lines.csv", prefix="Task_"),
        ])],
        objective=SymObjective(sense=Sense.maximize, expr=SymExpr(terms=[
            SymTerm(var="w_{i}", coef=1.0),
        ])),
        constraints=[],
    )
    ir = expand(sym, store)
    assert set(ir.all_var_instances) == {"w_Task_0", "w_Task_1"}


def test_row_index_and_cell(store: DataStore):
    """`kind: "rows"` + DataRef.row 取单元格（1 起）。"""
    sym = SymbolicIR(
        variables=[SymVar(name="y", var_type=VarType.binary, dims=[
            IndexSource(kind="rows", file="vals.csv"),
        ])],
        objective=SymObjective(sense=Sense.maximize, expr=SymExpr(terms=[
            SymTerm(var="y_{i}", coef=1.0),
        ])),
        constraints=[SymConstraint(
            name="c_{i}", sense=Sense.le,
            for_each="i",
            lhs=SymExpr(terms=[SymTerm(var="y_{i}", coef=1.0)]),
            rhs=DataRef(file="vals.csv", column="col_2", row="{i}"),
        )],
    )
    ir = expand(sym, store)
    assert set(ir.all_var_instances) == {"y_1", "y_2"}
    rhs_map = {c.name: c.rhs for c in ir.constraints}
    assert rhs_map == {"c_1": 10.0, "c_2": 20.0}, rhs_map


def test_header_digest_lists_synthetic_columns(tmp_path: Path):
    """无表头文件在 prompt 里给出 col_1..col_n，而不是数据值。"""
    root = tmp_path / "inst"
    data = root / "data"
    data.mkdir(parents=True)
    with (data / "vals.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["C0001", "10", "0"])
        w.writerow(["C0002", "20", "1"])
    (root / "instance.json").write_text(json.dumps({
        "abstract_problem": "min something {n}",
        "parameters": {"n": 2},
        "files": {"vals": {"path": "data/vals.csv", "description": "data rows"}},
        "optimal_value": 1.0,
    }, ensure_ascii=False), encoding="utf-8")

    nl = load_miplibnl_instance(root).samples[0].nl_text
    assert "无表头（首行即数据）" in nl
    assert "col_1" in nl and "col_3" in nl
    assert "C0001" not in nl, "数据值不能进 prompt"
