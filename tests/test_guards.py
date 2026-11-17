"""P0.5 守卫测试：静默丢约束 / 目标退化成常数。

背景（runs6_p0 实测）：
- f2gap40400 把列名 `Task_0` 当键值（文件里是 `0`），40 条覆盖约束被 `on_missing=skip`
  全部静默丢掉，最后剩一个无约束的"最小工资"模型 —— 解可行但目标 -84（参考 20772）。
- neos-3611689-kaihu 的目标写成了常数 37（0 个变量项），却被当成正常模型去求解。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nl2opt.expand import DataStore, ExpandError, expand  # noqa: E402
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
    with (d / "jobs.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["job_id", "dur"])
        w.writerow(["J1", "3"])
        w.writerow(["J2", "4"])
    with (d / "vals.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["task", "v"])
        w.writerow(["0", "10"])
        w.writerow(["1", "20"])
    return DataStore(d)


def _sym(rhs_key: str, sense: Sense = Sense.ge) -> SymbolicIR:
    return SymbolicIR(
        variables=[SymVar(name="x", var_type=VarType.binary, dims=[
            IndexSource(kind="csv", file="jobs.csv", column="job_id"),
        ])],
        objective=SymObjective(sense=Sense.minimize, expr=SymExpr(terms=[
            SymTerm(var="x_{i}", coef=1.0),
        ])),
        constraints=[SymConstraint(
            name="c_{i}", sense=sense,
            lhs=SymExpr(terms=[SymTerm(var="x_{i}", coef=1.0)]),
            rhs=DataRef(file="vals.csv", column="v",
                        key_column="task", key_value=rhs_key),
        )],
    )


def test_all_constraints_lost_raises_with_real_keys(store: DataStore):
    """整族约束因键风格不一致被跳过 → 报错，并给出真实键取值示例。"""
    with pytest.raises(ExpandError) as ei:
        expand(_sym("Task_0"), store)
    msg = str(ei.value)
    assert "整条被跳过" in msg
    assert "实际取值示例" in msg
    assert "'0'" in msg, "要提示文件里的真实取值风格"


def test_partial_key_miss_is_tolerated(store: DataStore):
    """只有个别约束查不到键 → 允许（记入 assumptions），不判死。"""
    sym = _sym("0")
    # 追加一条键不存在的约束：不应整体失败
    sym.constraints.append(SymConstraint(
        name="c2", sense=Sense.ge,
        lhs=SymExpr(terms=[SymTerm(var="x_{i}", coef=1.0)]),
        rhs=DataRef(file="vals.csv", column="v", key_column="task", key_value="Task_9"),
    ))
    ir = expand(sym, store)
    assert len(ir.constraints) == 1
    assert any("因数据键不存在被跳过" in a for a in ir.assumptions)


def test_constant_objective_raises(store: DataStore):
    """目标没有变量项（纯常数）→ 判为建模失败。"""
    sym = SymbolicIR(
        variables=[SymVar(name="x", var_type=VarType.binary, dims=[
            IndexSource(kind="csv", file="jobs.csv", column="job_id"),
        ])],
        objective=SymObjective(sense=Sense.minimize,
                               expr=SymExpr(terms=[], constant=37.0)),
        constraints=[SymConstraint(
            name="c", sense=Sense.le,
            lhs=SymExpr(terms=[SymTerm(var="x_{i}", coef=1.0)]),
            rhs=1.0,
        )],
    )
    with pytest.raises(ExpandError) as ei:
        expand(sym, store)
    assert "不含任何变量项" in str(ei.value)
