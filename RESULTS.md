# 评测结果快照（协议 v37，2026-11-09）

## 口径

- 数据：MIPLIB-NL 随机抽样 **20 题**（`runs20/manifest.json`，5 批 × 4 题）。
- 模型：`deepseek-flash`，`reasoning_effort=low`，`attempts=2`、`max_rounds=3`。
- 协议：`PROTOCOL_VERSION = 37`（P0 表头 / P0.5 守卫 / P1 宽表 / P2 无表头位置列名）。
- 启动器：`run_eval.ps1`，单题预算 900 s、进程虚拟内存上限 12 GB；超限写合成结果
  （`budget_exceeded` / `memory_exceeded`），保证每题都有一行结果。
- 结果目录：`runs20_u1/`（`summary.md` / `summary.csv` 为逐题明细）。

## 通过：4 / 20

| 实例 | 目标值 | 参考值 |
|---|---|---|
| `enlight_hard` | 37.0 | 37.0 |
| `neos-3611689-kaihu` | 119.0 | 119.0 |
| `mik-250-20-75-2` | 50768.0 | 50768.0 |
| `f2gap40400` | 20772.0 | 20772.0 |

## 未通过：16 / 20

| 类型 | 数量 | 实例 |
|---|---|---|
| 解可行但目标值不符 | 6 | `drayage-25-27`、`neos-3754480-nidda`、`gen-ip002`、`csched010`、`rmatr100-p10`、`bienst2` |
| 求解超时 / 多轮失败 | 4 | `neos-5192052-neckar`（big-M 进目标，被守卫拦下）、`sing44`（69,574 实例超上限）、`graphdraw-mainerd`、`prod2` |
| 模型不可行 | 1 | `beasleyC2` |
| 内存中断（> 12 GB） | 2 | `rmatr200-p5`、`rmatr200-p10` |
| 超时中断（> 900 s） | 2 | `leo1`、`supportcase21i` |

## 关键改进（与改动前同 20 题对比）

| 指标 | 改动前 | 现在 |
|---|---|---|
| 通过 | 2（enlight_hard、mik） | **4**（+ f2gap40400、kaihu） |
| 数据阶段通过 | 6 / 20 | **13 / 20** |
| token / 估算成本 | 825,772 / ¥4.93 | 1,000,837 / ¥5.88 |

数据阶段（能否把题面 + CSV 正确展开成模型）已基本解决；剩余失败集中在
**建模语义**（目标写错/漏项、big-M 进目标、行号与标签混淆）与**规模/资源**
（展开实例数、内存、求解时长）。

## 复现

```powershell
# 20 题统一口径（已有结果会跳过）
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_eval.ps1 `
    -Manifest runs20\manifest.json -Out runs20_u1

# 再做汇总（对比基线目录）
$env:SUM_BASE="runs20"; $env:SUM_NEW="runs20_u1"
python summarize6.py
```