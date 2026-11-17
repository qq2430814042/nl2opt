# -*- coding: utf-8 -*-
"""对比两组结果（默认 runs20 基线 vs runs6_p0），输出 summary.md。

用环境变量指定目录：SUM_BASE=... SUM_NEW=...（默认 runs20 / runs6_p0）。
对比维度：是否通过、状态、尝试/轮次、目标值、token、耗时、第一条错误，
并标注数据引用类错误是否消失。
"""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = Path(os.environ.get("SUM_BASE", ROOT / "runs20"))
NEW = Path(os.environ.get("SUM_NEW", ROOT / "runs6_p0"))
DS = Path(r"D:\Downloads\MIPLIB-NL\dataset")

PRICE = {"hit": 0.04, "miss": 2.0, "out": 8.0}   # 高峰价（deepseek-flash）

DATA_ERR_MARKS = ("没有列", "键占位符", "列名对不上", "占位符")


def load_first(p: Path):
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return data[0] if data else None
    except Exception:
        return None


def ref_value(inst: str):
    try:
        return float(json.loads((DS / inst / "instance.json").read_text(encoding="utf-8"))["optimal_value"])
    except Exception:
        return None


def secs_from_logs(folder: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for log in folder.glob("batch*.log"):
        txt = log.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\[END\] (\S+) exit=\S+ seconds=(\d+)", txt):
            out[m.group(1)] = int(m.group(2))
    return out


def baseline_secs() -> dict[str, int]:
    f = BASE / "summary.csv"
    if not f.exists():
        return {}
    out: dict[str, int] = {}
    with f.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                out[row["id"]] = int(row["secs"])
            except (KeyError, TypeError, ValueError):
                pass
    return out


def cost_of(tu: dict) -> float:
    return (tu.get("prompt_cache_hit_tokens", 0) / 1e6 * PRICE["hit"]
            + tu.get("prompt_cache_miss_tokens", 0) / 1e6 * PRICE["miss"]
            + tu.get("completion_tokens", 0) / 1e6 * PRICE["out"])


def first_err(r) -> str:
    errs = (r or {}).get("errors") or []
    if not errs:
        return ""
    return str(errs[0]).replace("\n", " ")[:150]


def data_stage_ok(r) -> bool:
    """数据阶段通过 = 至少有一轮成功展开（orchestrator 会留下 final_ir）。"""
    if not r:
        return False
    if r.get("final_ir"):
        return True
    for step in r.get("trajectory") or []:
        if isinstance(step, dict) and step.get("expand_meta"):
            return True
    return False


def is_data_err(r) -> bool:
    if not r:
        return False
    joined = " ".join(str(e) for e in (r.get("errors") or []))
    return any(m in joined for m in DATA_ERR_MARKS)


def main() -> int:
    manifest = json.loads((NEW / "manifest.json").read_text(encoding="utf-8"))
    order: list[tuple[int, str]] = []
    for b in manifest:
        for inst in b["instances"]:
            order.append((b["batch"], inst))
    new_secs = secs_from_logs(NEW)
    old_secs = baseline_secs()

    rows = []
    for batch, inst in order:
        old = load_first(BASE / f"{inst}.json")
        new = load_first(NEW / f"{inst}.json")
        ou = (old or {}).get("token_usage") or {}
        nu = (new or {}).get("token_usage") or {}
        rows.append({
            "batch": batch, "id": inst,
            "old_ok": None if old is None else bool(old.get("success")),
            "old_status": (old or {}).get("status", "未完成"),
            "old_err": first_err(old),
            "old_obj": (old or {}).get("objective_value"),
            "old_data_ok": data_stage_ok(old),
            "old_secs": old_secs.get(inst),
            "old_tokens": ou.get("total_tokens", 0),
            "old_cost": cost_of(ou),
            "new_ok": None if new is None else bool(new.get("success")),
            "new_status": (new or {}).get("status", "未完成"),
            "new_att": (new or {}).get("attempt"),
            "new_rounds": (new or {}).get("rounds"),
            "new_obj": (new or {}).get("objective_value"),
            "new_err": first_err(new),
            "new_data_ok": data_stage_ok(new),
            "new_secs": new_secs.get(inst),
            "new_tokens": nu.get("total_tokens", 0),
            "new_cost": cost_of(nu),
            "ref": ref_value(inst),
        })

    def mark(s):
        return {True: "✅", False: "❌", None: "⏳"}[s]

    done = [r for r in rows if r["new_ok"] is not None]
    ok = sum(1 for r in rows if r["new_ok"])
    old_data_ok = [r["id"] for r in rows if r["old_data_ok"]]
    new_data_ok = [r["id"] for r in rows if r["new_data_ok"]]
    still_data = [r["id"] for r in rows if r["new_ok"] is not None and not r["new_data_ok"]]
    solved_wrong = [r["id"] for r in rows if r["new_status"] == "suspicious"]
    tot_tokens = sum(r["new_tokens"] for r in rows)
    tot_cost = sum(r["new_cost"] for r in rows)
    base_tokens = sum(r["old_tokens"] for r in rows)

    lines = []
    n = len(rows)
    lines.append(f"# {NEW.name} 汇总（对比 {BASE.name}）\n")
    lines.append(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"- 完成：{len(done)}/{n}；通过：**{ok}/{n}**；解可行但与参考值不符：{len(solved_wrong)} 题")
    lines.append(f"- **数据阶段通过**：{BASE.name} {len(old_data_ok)}/{n} → {NEW.name} "
                 f"**{len(new_data_ok)}/{n}**（仍卡住的：{('、'.join(still_data)) or '无'}）")
    lines.append(f"- token：{tot_tokens:,}（{BASE.name} 同 {n} 题 {base_tokens:,}）；"
                 f"估算成本：¥{tot_cost:.2f}")
    lines.append("")

    lines.append("## 逐题对比\n")
    lines.append(f"| 批 | 实例 | {BASE.name} 状态 | {NEW.name} 状态 | 数据阶段 | 通过 | 尝试 | 轮次 | 目标值 | 参考值 | 首错 | 耗时(s) | tokens |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        ds = "✔" if r["new_data_ok"] else "✘"
        lines.append(
            f"| {r['batch']} | {r['id']} | {r['old_status']} "
            f"| {r['new_status']} | {ds} | {mark(r['new_ok'])} "
            f"| {r['new_att'] if r['new_att'] is not None else '-'} | {r['new_rounds'] if r['new_rounds'] is not None else '-'} "
            f"| {r['new_obj'] if r['new_obj'] is not None else '-'} | {r['ref'] if r['ref'] is not None else '-'} "
            f"| {r['new_err'][:60] or '-'} | {r['new_secs'] if r['new_secs'] is not None else '-'} | {r['new_tokens']:,} |"
        )

    lines.append("\n## 结论\n")
    lines.append("- **数据阶段**：`✔` 表示轨迹里至少成功展开过一次（列名/文件/占位符都通过了）。")
    lines.append("- **通过**：目标值与参考值在容差内一致（1e-4 相对误差）。"
                 "「解可行但与参考不符」表示模型自洽但建模/口径有误。")
    lines.append("- **本轮改动**：P1 列模板/列名索引/行序单元格，P2 无表头位置列名；"
                 "宽表类题目的失败原因见上表「首错」列。")
    for r in rows:
        if r["new_status"] == "未完成":
            lines.append(f"- ⚠ {r['id']} 未产出结果，重跑会自动跳过已完成项。")

    notes = NEW / "_notes.md"
    if notes.exists():
        lines.append("\n## 运行说明\n")
        lines.append(notes.read_text(encoding="utf-8").strip())

    (NEW / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    import csv as _csv

    def _flat(v):
        return v.replace("\n", " ") if isinstance(v, str) else v

    with (NEW / "summary.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows([{k: _flat(v) for k, v in r.items()} for r in rows])

    print("\n".join(lines[:10]))
    print(f"\nwrote {NEW / 'summary.md'} and summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
