# a-share-toolkit

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)]()
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)]()
[![No auto-trading](https://img.shields.io/badge/auto--trading-never-red.svg)](#what-it-does-not-do)

小资金、半自动、纪律优先的 A 股工具链。**零第三方依赖**（纯标准库）。

> A zero-dependency A-share toolkit for small accounts.
>
> The enemy of a small account is not stock picking — it is **friction**.
> With a ¥5 minimum commission, a ¥1,000 round trip loses ~1% before the price
> even moves. This repo encodes that arithmetic as a **hard gate**: orders that
> cannot clear it are refused, with the exact shortfall printed.
>
> Three rules, enforced in code: **degrade but never fabricate**, **append only,
> never rewrite**, and **abstention is not consent** (an AI that times out or is
> skipped by the budget gate never counts as approval).
>
> ```bash
> git clone https://github.com/huozhongbufan/a-share-toolkit
> cd a-share-toolkit
> cp config.example.json config.json     # edit fees / limits
> python run.py status                   # account state from the ledger
> python run.py dip                      # scan for oversold opportunities
> python run.py serve                    # local web workbench
> ```
>
> **No auto-trading.** The last mile is a human keypress — by design, not by omission.

---

一套**小资金、半自动、自用**的 A 股工具链，纯 Python 标准库（零第三方依赖）。
不是荐股工具，也不是自动交易机器人——核心是**用纪律替代盯盘**。

代码来自一个真实在跑的项目，**踩过的坑和设计取舍都写在注释和 docs/ 里**。

---

## 它解决什么

| 问题 | 这里的做法 |
|---|---|
| 没有行情 API 权限 | 腾讯/新浪公开行情 + 同花顺本地代码表（7.6k 标的，离线零请求） |
| 想读同花顺界面但不想被抢焦点 | `PrintWindow` 截图 + 本地视觉模型（`ec/screen.py`） |
| ¥5 最低佣金让小资金无法交易 | **费用闸门**：单笔费用占比超阈值直接拦（`ec/risk.py`） |
| 手工回填成交价会错 | **只追加流水账**，状态由重放得出（`ec/journal.py`） |
| 想用大模型又不被它坑 | 四层分工：规则 → 本地小模型（只检索）→ API 大模型（**只能否决**）→ 人 |
| 同花顺风格的桌面工作台 | tkinter 原生窗口 + ttk.Treeview（`ec/workbench.py`） |
| 网页/手机版 | stdlib `http.server` + 手写前端（`ec/webapp.py` + `web/`） |
| 本地模型总被卸载 | `keep_alive: -1` 钉住显存（`ec/warm.py`） |

## 三条硬规矩（写在代码注释里）

1. **降级可以，编造不行** —— 数据源挂了标「已降级」，拿不到就拒绝出单
2. **只追加，不修改** —— 流水账永不回头改，状态永远可重算
3. **弃权 ≠ 同意** —— AI 超时/报错/因预算跳过，都不计入赞成票

## 快速看看结构

```
ec/quotes.py     行情（腾讯/新浪/同花顺 HTTP）
ec/ths.py        同花顺本地代码表 + 单只精确查询
ec/ths_scout.py  本地小模型把自然语言检索成候选（代码永远来自本地表）
ec/screen.py     PrintWindow 截同花顺窗口 + 本地视觉模型读数
ec/risk.py       风控闸门（费用占比 / 仓位 / 熔断 / 频次）
ec/journal.py    只追加流水账
ec/todos.py      工单（待办）
ec/budget.py     AI 花费台账（按笔交易金额反推可花多少）
ec/ai_review.py  多家大模型并行复核（只能否决）
ec/pipeline.py   七段式流水线
ec/backtest.py   回测（含费用）
ec/workbench.py  同花顺风格桌面工作台（K线自绘）
ec/webapp.py     本地 Web 工作台（stdlib http.server）
ec/warm.py       本地模型常驻显存
```

## 关键概念：费用闸门

小资金交易的敌人不是选股，是**摩擦成本**。以 ¥5 最低佣金为例：

| 单笔金额 | 买卖往返成本占比 |
|---|---|
| ¥100 | ~10% |
| ¥1,000 | ~1.0% |
| ¥1,667 | 0.6%（打平线） |
| ¥20,000 | 最低佣金不再约束 |

所以小资金在收费券商上做短线**数学上不成立**。闸门就是把这个事实写进代码：
不满足就拒绝，并明确告诉你还差多少。

## 用法

```bash
python run.py --help          # 看所有命令
python run.py status          # 账户/现金/累计
python run.py dip             # 扫描跌出来的机会
python run.py ticket sh515250 # 看这一笔在闸门下会怎样
python run.py flow            # 跑一遍流水线（出工单）
python run.py workbench       # 桌面工作台（同花顺风格）
python run.py serve           # Web 工作台
python run.py warm            # 把本地模型钉进显存
```

## 依赖

**核心零依赖**（Python 3.10+）。可选：

- `ollama`（本地模型：检索 / 读图）
- `PySide6 + numpy`（仅 3D 桌宠用，见另一个仓库）

## 不做什么

- **不做自动下单**。最后一公里是人按键，这是设计而不是偷懒
- **不预测价格**。只做「这笔划不划算」的判断
- **不做全自动**。半自动的「半」是风控点

## 许可

MIT（代码）。行情数据版权归各数据源。**不构成投资建议。**
