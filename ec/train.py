"""训练模式：每天跑一遍完整流水线，只记录不落待办。

为什么要这个
------------
用户的本金 ¥500 + ¥5 最低佣金下，费用闸门几乎天天拦——**没有真实交易可做**。
但他的判断是：**路 B 照跑，当成训练，把系统跑熟、把 bug 跑出来。**

这个判断是对的，理由有三条：

  1. **闸门拦截统计本身是资产。** 跑 60 天后能回答「哪条闸门最常拦」，
     这比任何理论推演都有说服力。
  2. **bug 只在真实数据下暴露。** 字段解析、数据源降级、时区、停牌，
     这些不跑就永远不知道。
  3. **等本金到位时系统已经磨合好了。** 不用那时候才开始踩坑。

三条设计约束
------------
1. **绝不写待办流水账。** 走 `TodoStore(dry=True)`，训练记录单独存
   `data/train_log.jsonl`。真实账本永远只有真发生过的事。
2. **完整跑，不放宽闸门。** 和 `--demo` 相反：demo 是为了让候选通过，
   训练是为了看真实结论。闸门一条都不动。
3. **失败也要记。** 数据源挂了、阶段 FAIL，都是最有价值的记录，
   不能因为「今天没成功」就不记。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from .config import Config

LOG = "train_log.jsonl"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _gate_blocks(todo) -> list[str]:
    """从待办的风控明细里取出「拦了哪些闸门」。"""
    checks = (todo.risk or {}).get("checks") or []
    out = []
    for c in checks:
        if not isinstance(c, dict):
            continue
        if c.get("level") == "block":
            out.append(str(c.get("name") or "未知闸门"))
    return out


def record(cfg: Config, result, elapsed: float = 0.0) -> dict:
    """把一次训练结果压缩成一条记录并追加。返回这条记录。

    注意区分 `fail` 和 `warn`：
      · fail = 阶段真的失败了（数据源挂了、解析报错）→ 这是异常
      · warn = 有东西被拦下了（闸门拦截、AI 否决）→ **这是系统在正常工作**，
        不能算异常。早期版本把两者混在一起，导致「闸门拦下一个候选」
        被报成「出现过异常」，完全是误导。
    """
    stages = []
    errors = []
    warnings = []
    for s in result.stages:
        stages.append({
            "no": s.no, "name": s.name, "status": s.status,
            "detail": (s.detail or "")[:300],
            "elapsed": round(float(s.elapsed or 0), 2),
        })
        line = f"[{s.name}] {(s.detail or '')[:200]}"
        if s.status == "fail":
            errors.append(line)
        elif s.status == "warn":
            warnings.append(line)

    blocks: dict[str, int] = {}
    todo_rows = []
    for t in result.todos:
        names = _gate_blocks(t)
        for n in names:
            blocks[n] = blocks.get(n, 0) + 1
        todo_rows.append({
            "id": t.id, "kind": t.kind, "code": t.code, "name": t.name,
            "state": t.state, "blocked": bool(getattr(t, "blocked", False)),
            "gate_blocks": names,
        })

    dh = result.data_health or {}
    rec = {
        "date": _now()[:10],
        "at": _now(),
        "started_at": result.started_at,
        "elapsed": round(float(elapsed), 2),
        "headline": result.headline,
        "headline_level": result.headline_level,
        "data": {
            "source": dh.get("source"),
            "as_of": dh.get("as_of"),
            "market_phase": dh.get("market_phase"),
            "freshness": dh.get("freshness"),
            "degraded": dh.get("degraded"),
            "latency_sec": dh.get("latency_sec"),
        },
        "stages": stages,
        "todos": todo_rows,
        "gate_blocks": blocks,
        "errors": errors,
        "warnings": warnings,
        "demo": result.demo,
    }

    os.makedirs(cfg.data_dir, exist_ok=True)
    path = os.path.join(cfg.data_dir, LOG)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def entries(cfg: Config) -> list[dict]:
    path = os.path.join(cfg.data_dir, LOG)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def report(cfg: Config) -> dict:
    """累积统计。这才是训练模式真正的产出。"""
    rows = entries(cfg)
    if not rows:
        return {"days": 0, "runs": 0}

    days = sorted({r.get("date", "") for r in rows})
    n = len(rows)

    # 闸门拦截分布 —— 最有价值的一块
    gate: dict[str, int] = {}
    for r in rows:
        for k, v in (r.get("gate_blocks") or {}).items():
            gate[k] = gate.get(k, 0) + int(v or 0)

    # 阶段健康度
    stage_stat: dict[str, dict[str, int]] = {}
    for r in rows:
        for s in r.get("stages") or []:
            name = s.get("name") or "?"
            d = stage_stat.setdefault(name, {})
            d[s.get("status") or "?"] = d.get(s.get("status") or "?", 0) + 1

    # 数据源
    degraded = sum(1 for r in rows if (r.get("data") or {}).get("degraded"))
    stale = sum(1 for r in rows
                if (r.get("data") or {}).get("freshness") == "陈旧")
    sources: dict[str, int] = {}
    for r in rows:
        s = (r.get("data") or {}).get("source") or "?"
        sources[s] = sources.get(s, 0) + 1

    # 候选频率
    runs_with_todo = sum(1 for r in rows if (r.get("todos") or []))
    total_todos = sum(len(r.get("todos") or []) for r in rows)

    # 错误清单（去重，保留最近出现的）
    err_seen: dict[str, str] = {}
    for r in rows:
        for e in r.get("errors") or []:
            err_seen[e] = r.get("date", "")

    warn_seen: dict[str, str] = {}
    for r in rows:
        for w in r.get("warnings") or []:
            warn_seen[w] = r.get("date", "")

    elapsed = [r.get("elapsed") or 0 for r in rows if r.get("elapsed")]

    return {
        "days": len(days),
        "first_day": days[0] if days else "",
        "last_day": days[-1] if days else "",
        "runs": n,
        "runs_with_todo": runs_with_todo,
        "total_todos": total_todos,
        "gate_blocks": dict(sorted(gate.items(), key=lambda x: -x[1])),
        "stage_stat": stage_stat,
        "degraded": degraded,
        "stale": stale,
        "sources": sources,
        "errors": dict(sorted(err_seen.items(), key=lambda x: x[1],
                              reverse=True)[:15]),
        "warnings": dict(sorted(warn_seen.items(), key=lambda x: x[1],
                                reverse=True)[:10]),
        "elapsed_avg": round(sum(elapsed) / len(elapsed), 1) if elapsed else 0,
        "elapsed_max": round(max(elapsed), 1) if elapsed else 0,
    }
