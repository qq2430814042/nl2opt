# -*- coding: utf-8 -*-
"""Summarize runs20: read per-instance eval JSON + manifest, emit markdown/CSV report.

Works for both the 5-batch launcher (batch*.log with [START]/[END]) and the rolling
per-instance launcher (logs/<id>.log); an instance is counted only when its result JSON
parses to a non-empty list (the same rule the launchers use for "done").
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "runs20"
LOGS = OUT / "logs"
DS = Path(r"D:\Downloads\MIPLIB-NL\dataset")

# config.yaml pricing (CNY per 1M tokens). 22:xx Beijing = peak.
PRICE = {"hit": 0.04, "miss": 2.0, "out": 8.0}

STATUS_ZH = {
    "optimal": "命中参考值",
    "suspicious": "解可行但与参考值不符",
    "suspicious_resolved": "重写后命中（原为模型错）",
    "max_rounds_exceeded": "轮次用尽仍失败",
    "unknown": "未收敛（解析/展开失败）",
    "timeout": "求解超时",
    "infeasible": "模型不可行",
    "llm_unavailable": "LLM 不可用",
    "expand_error": "展开失败",
}

# coarse failure buckets, first matching rule wins
FAIL_RULES = [
    ("规模超上限（展开被拒）", "超过上限"),
    ("数据引用/键占位符错误", "键占位符"),
    ("数据引用/键占位符错误", "没有列"),
    ("数据引用/键占位符错误", "虚构数据文件"),
    ("IR 结构不合规（Pydantic 校验失败）", "validation errors for SymbolicIR"),
    ("IR 结构不合规（Pydantic 校验失败）", "符号化理解失败"),
    ("重写触发退化保护", "符号模型退化"),
    ("目标值与参考不符（疑似参考噪声）", "目标值与参考不符"),
    ("求解超时", "状态=timeout"),
    ("模型不可行", "infeasible"),
    ("其他", ""),
]


def fail_category(errors: str) -> str:
    for name, needle in FAIL_RULES:
        if not needle or needle in errors:
            return name
    return "其他"


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def csv_rows(inst: str) -> int:
    d = DS / inst / "data"
    if not d.is_dir():
        return 0
    n = 0
    for f in d.glob("*.csv"):
        try:
            with f.open("rb") as fh:
                n += max(0, sum(1 for _ in fh) - 1)
        except OSError:
            pass
    return n


def ref_value(inst: str):
    try:
        return float(load_json(DS / inst / "instance.json").get("optimal_value"))
    except Exception:
        return None


def batch_seconds() -> dict[str, int]:
    """Durations from 5-batch logs ([END] id exit=.. seconds=N)."""
    secs: dict[str, int] = {}
    for log in OUT.glob("batch*.log"):
        txt = log.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\[END\] (\S+) exit=\S+ seconds=(\d+)", txt):
            secs[m.group(1)] = int(m.group(2))
    return secs


def log_start(inst: str):
    """Per-instance log file creation time == Start-Process time (rolling launcher)."""
    f = LOGS / f"{inst}.log"
    try:
        return datetime.fromtimestamp(f.stat().st_ctime)
    except OSError:
        return None


def seconds_for(inst: str, js: Path, known: dict[str, int]):
    if inst in known:
        return known[inst]
    st = log_start(inst)
    if st is None:
        return None
    return int(max(0, js.stat().st_mtime - st.timestamp()))


def main() -> int:
    manifest = load_json(OUT / "manifest.json")
    batch_of: dict[str, int] = {}
    order: list[str] = []
    for b in manifest:
        for name in b["instances"]:
            batch_of[name] = b["batch"]
            order.append(name)
    known = batch_seconds()

    rows = []
    for inst in order:
        js = OUT / f"{inst}.json"
        r = None
        if js.exists():
            try:
                data = load_json(js)
                if data:
                    r = data[0]
            except Exception:
                r = None
        if r is None:
            rows.append({
                "batch": batch_of[inst], "id": inst, "rows": csv_rows(inst),
                "success": None, "status": "未完成（规模/资源中断）", "attempt": "-", "rounds": "-",
                "obj": None, "ref": ref_value(inst), "gap": None, "secs": None,
                "calls": 0, "prompt": 0, "completion": 0, "total": 0, "hit": 0,
                "miss": 0, "cost": 0.0, "errors": "", "category": "",
            })
            continue

        tu = r.get("token_usage") or {}
        ref = ref_value(inst)
        obj = r.get("objective_value")
        gap = None
        if obj is not None and ref not in (None, 0):
            gap = (obj - ref) / abs(ref)
        cost = (tu.get("prompt_cache_hit_tokens", 0) / 1e6 * PRICE["hit"]
                + tu.get("prompt_cache_miss_tokens", 0) / 1e6 * PRICE["miss"]
                + tu.get("completion_tokens", 0) / 1e6 * PRICE["out"])
        rows.append({
            "batch": batch_of[inst], "id": inst, "rows": csv_rows(inst),
            "success": bool(r.get("success")),
            "status": STATUS_ZH.get(r.get("status") or "", r.get("status") or ""),
            "attempt": f"{r.get('attempt', 0)}/{r.get('attempts', 0)}",
            "rounds": r.get("rounds", 0),
            "obj": obj, "ref": ref, "gap": gap,
            "secs": seconds_for(inst, js, known),
            "calls": tu.get("calls", 0), "prompt": tu.get("prompt_tokens", 0),
            "completion": tu.get("completion_tokens", 0),
            "total": tu.get("total_tokens", 0),
            "hit": tu.get("prompt_cache_hit_tokens", 0),
            "miss": tu.get("prompt_cache_miss_tokens", 0),
            "cost": cost,
            "errors": " | ".join(r.get("errors") or []).replace("\n", " "),
        })
        rows[-1]["category"] = fail_category(rows[-1]["errors"]) if not rows[-1]["success"] else ""

    rows.sort(key=lambda x: (x["batch"], order.index(x["id"])))
    n = len(rows)
    fin = [r for r in rows if r["success"] is not None]
    ok = sum(1 for r in rows if r["success"])
    tot_tokens = sum(r["total"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    total_secs = sum(r["secs"] or 0 for r in rows)

    def fmt(x, nd=4):
        return "-" if x is None else f"{x:.{nd}f}"

    def mark(s):
        return {True: "✅", False: "❌", None: "⏳"}[s]

    lines = []
    lines.append("# runs20 汇总（20 题 / 5 批并行）\n")
    lines.append(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"- 完成评测：{len(fin)}/{n} 题；其中通过 **{ok} 题（{ok / max(1, len(fin)) * 100:.1f}%）**")
    lines.append(f"- 总 token：{tot_tokens:,}（输出 {sum(r['completion'] for r in rows):,}；"
                 f"缓存命中 {sum(r['hit'] for r in rows):,}）")
    lines.append(f"- 估算成本（deepseek-flash 高峰价，CNY）：¥{cost:.2f}")
    lines.append(f"- 单题累计耗时：{total_secs:,}s（并行，非墙钟）\n")

    lines.append("## 分批结果\n")
    lines.append("| 批 | 题数 | 通过 | 用时(s) | tokens | 成本(CNY) |")
    lines.append("|---|---|---|---|---|---|")
    for b in sorted({r["batch"] for r in rows}):
        rs = [r for r in rows if r["batch"] == b]
        sak = sum(1 for r in rs if r["success"])
        bsec = sum(r["secs"] or 0 for r in rs)
        lines.append(f"| {b} | {len(rs)} | {sak}/{len(rs)} | {bsec} | "
                     f"{sum(r['total'] for r in rs):,} | ¥{sum(r['cost'] for r in rs):.2f} |")

    lines.append("\n## 明细\n")
    lines.append("| 批 | 实例 | CSV行数 | 通过 | 状态 | 尝试 | 轮次 | 目标值 | 参考值 | 相对偏差 | 耗时(s) | tokens |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        gap = "-" if r["gap"] is None else f"{r['gap'] * 100:+.3f}%"
        secs_s = "-" if r["secs"] is None else str(r["secs"])
        lines.append(
            f"| {r['batch']} | {r['id']} | {r['rows']:,} | {mark(r['success'])} | {r['status']} "
            f"| {r['attempt']} | {r['rounds']} | {fmt(r['obj'])} | {fmt(r['ref'])} | {gap} "
            f"| {secs_s} | {r['total']:,} |"
        )

    lines.append("\n## 失败归类\n")
    buckets: dict[str, list[str]] = {}
    for r in rows:
        if r["success"] is False:
            buckets.setdefault(r["category"], []).append(r["id"])
    for name in sorted(buckets, key=lambda k: -len(buckets[k])):
        lines.append(f"- **{name}**（{len(buckets[name])}）：{'、'.join(buckets[name])}")

    lines.append("\n## 失败原因摘录\n")
    fails = [r for r in rows if r["success"] is False]
    if not fails:
        lines.append("- 无")
    for r in fails:
        lines.append(f"- **{r['id']}**：{r['errors'][:400] or '（无错误信息）'}")

    notes = OUT / "_notes.md"
    if notes.exists():
        lines.append("\n## 运行说明\n")
        lines.append(notes.read_text(encoding="utf-8").strip())

    (OUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    flat = [{k: (v.replace("\n", " ") if isinstance(v, str) else v) for k, v in r.items()}
            for r in rows]
    with (OUT / "summary.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(flat)

    print("\n".join(lines[:14]))
    print(f"\nwrote {OUT / 'summary.md'} and summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
