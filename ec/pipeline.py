"""七段流水线 —— 把工作流串成一条可以逐段观察的链。

设计目标不是「跑得快」，是**每一段都能单独看见结果**。工作台要显示的
「今天要不要动手」是这条链的产物，复盘要看的「为什么没动手」也是。

    ① 数据采集   抓快照，记录来源和时间戳（降级必须可见）
    ② 候选池     买得起 + 排雷 + 流动性
    ③ 持仓体检   已持仓的退出判断 —— 卖出永远先算
    ④ 信号扫描   候选池里的买入信号
    ⑤ 风控闸门   硬规则，AI 不可覆盖
    ⑥ AI 复核   多模型独立否决
    ⑦ 生成待办   写进只追加的待办流水账

每一段都返回 Stage，带状态、耗时、明细。任何一段失败都不静默跳过 ——
status 会是 fail，headline 会说明。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import datetime

from .config import Config
from . import ai_review, risk, strategy, universe as uni
from .dataloader import BarStore
from .journal import Journal
from .quotes import fetch_quotes, normalize_code
from .todos import (TodoStore, Todo, BUY, SELL, PENDING_REVIEW, PENDING_CONFIRM,
                    REJECTED, TodoError)

OK, WARN, SKIP, FAIL = "ok", "warn", "skip", "fail"


@dataclass
class Stage:
    no: int
    name: str
    status: str
    detail: str
    elapsed: float = 0.0
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FlowResult:
    started_at: str
    stages: list[Stage] = field(default_factory=list)
    todos: list[Todo] = field(default_factory=list)
    headline: str = ""
    headline_level: str = OK          # ok | warn | fail
    data_health: dict = field(default_factory=dict)
    demo: bool = False
    demo_relaxed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "stages": [s.to_dict() for s in self.stages],
            "todos": [t.to_dict() for t in self.todos],
            "headline": self.headline,
            "headline_level": self.headline_level,
            "data_health": self.data_health,
            "demo": self.demo,
            "demo_relaxed": self.demo_relaxed,
        }


def _quote_ts_to_dt(ts: str) -> datetime | None:
    """腾讯的时间戳格式是 20261014161450。"""
    for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except (ValueError, TypeError):
            continue
    return None


def market_phase(now: datetime | None = None) -> str:
    """当前处于交易时段的哪个阶段。

    只看星期和钟点，不维护节假日日历 —— 那种表每年要更新，调休还会错。
    节假日的影响交给下面的逻辑处理：时钟说「盘中」但数据不更新，
    会被标成「盘中但数据陈旧」，而不是直接报错。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return "休市"
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 15:
        return "盘前"
    if hm < 9 * 60 + 30:
        return "集合竞价"
    if hm <= 11 * 60 + 30:
        return "盘中"
    if hm < 13 * 60:
        return "午休"
    if hm <= 15 * 60:
        return "盘中"
    return "盘后"


# 延迟容忍：盘中要严格，非盘中无所谓（拿到的就是最近一次收盘价）
LATENCY_TOLERANCE_SEC = 90


def _data_health(quotes, now: datetime | None = None) -> dict:
    """把数据来源、时间戳、延迟、是否降级收敛成一处。

    这是「降级可以、编造不行」这条原则的落地点：每条数据都必须能回答
    「从哪来」「什么时候的」「是不是备源」。
    """
    now = now or datetime.now()
    phase = market_phase(now)
    sources = sorted({q.source for q in quotes.values()})
    stamps = [q.ts for q in quotes.values() if q.ts]
    as_of = max(stamps) if stamps else ""
    dt = _quote_ts_to_dt(as_of)
    latency = (now - dt).total_seconds() if dt else None
    degraded = bool(sources) and "tencent" not in sources

    # 只有盘中才谈「数据陈旧」。盘后拿到的本来就该是收盘价。
    if latency is None:
        freshness = "未知"
    elif phase == "盘中":
        freshness = "新鲜" if latency <= LATENCY_TOLERANCE_SEC else "陈旧"
    else:
        freshness = "收盘数据"

    return {
        "source": "/".join(sources) or "none",
        "as_of": as_of,
        "latency_sec": round(latency, 1) if latency is not None else None,
        "market_phase": phase,
        "freshness": freshness,
        "degraded": degraded,
        "n_quotes": len(quotes),
        "ok": bool(quotes) and freshness != "陈旧",
    }


def run_flow(cfg: Config, *, code: str | None = None, limit: int = 50,
             use_ai: bool = True, ai_dry_run: bool = False,
             ignore_signal: bool = False, max_todos: int = 3,
             demo: bool = False, save_todos: bool = True,
             verbose=None) -> FlowResult:
    """跑一遍完整工作流。

    demo=True 会把费用闸门放宽到能放行的程度，目的是让样板能走完全链路。
    它会在结果里标记 demo=True，绝不当成真实结论。

    save_todos=False 是**训练模式**：完整跑一遍、完整算闸门和 AI，
    但不把待办写进流水账。结果是「今天系统会怎么判」的演练记录，
    用来暴露 bug、累积拦截统计，不产生任何真实待办。
    """
    say = verbose or (lambda *_: None)
    t_start = time.time()
    res = FlowResult(started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), demo=demo)

    if demo:
        # 只放宽「成本类」三条闸门 —— 它们在小资金下是结构性不可能通过的。
        # 熔断、频次、仓位上限、标的范围这些一条都不动，因为它们和钱无关。
        # 放宽了哪几条必须写进结果，绝不能让 demo 的结果看起来像真实结论。
        relaxed = []
        if cfg.risk.max_fee_ratio_per_trade < 0.05:
            cfg.risk.max_fee_ratio_per_trade = 0.05
            relaxed.append("费用占比 0.6% → 5%")
        if cfg.risk.min_edge_multiple > 0.5:
            cfg.risk.min_edge_multiple = 0.5
            relaxed.append("风险成本比 3 倍 → 0.5 倍")
        if cfg.risk.max_required_win_rate < 0.95:
            cfg.risk.max_required_win_rate = 0.95
            relaxed.append("打平所需胜率 55% → 95%")
        res.demo_relaxed = relaxed

    store = BarStore(cfg)
    journal = Journal(cfg)
    todos = TodoStore(cfg, dry=not save_todos)
    state = journal.load_state()

    # ---------------------------------------------------------- ① 数据采集
    t0 = time.time()
    u = None
    try:
        u = uni.build_universe(cfg)
        codes = [c.code for c in u.kept]
        quotes = fetch_quotes(codes) if codes else {}
        health = _data_health(quotes)
        res.data_health = health

        detail = (f"{health['source']} · {len(quotes)} 只 · "
                  f"{health['market_phase']} · 数据时间 {health['as_of'] or '未知'}")
        if health["latency_sec"] is not None:
            detail += f" · 延迟 {health['latency_sec']:.0f}s"
        detail += f" · {health['freshness']}"
        if health["degraded"]:
            detail += " · ⚠ 已降级到备源"

        status = OK
        if health["degraded"]:
            status = WARN
        if health["freshness"] == "陈旧":
            status = WARN
            detail += "（盘中数据超过 90 秒未更新）"

        res.stages.append(Stage(1, "数据采集", status, detail, time.time() - t0, health))
        if not quotes:
            res.stages[-1].status = FAIL
            res.headline = "数据不可用，今天不出单"
            res.headline_level = FAIL
            return res
    except Exception as exc:
        res.stages.append(Stage(1, "数据采集", FAIL, f"失败：{exc}", time.time() - t0))
        res.headline = f"数据采集失败：{exc}"
        res.headline_level = FAIL
        return res

    say(f"  数据源 {res.data_health['source']}，{len(quotes)} 只，"
        f"{res.data_health['market_phase']}")

    # ---------------------------------------------------------- ② 候选池
    t0 = time.time()
    res.stages.append(Stage(
        2, "候选池过滤", OK,
        f"{len(u.kept) + len(u.dropped)} 只 → 通过 {len(u.kept)} 只"
        f"（排除 {len(u.dropped)}：买不起 / 流动性不足 / 品种排除）",
        time.time() - t0,
        {"kept": len(u.kept), "dropped": len(u.dropped)},
    ))
    say(f"  候选 {len(u.kept)} 只")

    # ---------------------------------------------------------- ③ 持仓体检（卖出优先）
    t0 = time.time()
    sell_todos: list[Todo] = []
    exit_signals = []
    for pcode, pos in state.positions.items():
        try:
            bars = store.get(pcode)
            sig = strategy.evaluate_exit(cfg, pcode, pos.name, pos.cost_price, bars, pos.t0)
            q = quotes.get(pcode) or (fetch_quotes([pcode]).get(pcode))
            if q and q.price > 0:
                sig.price = q.price
            if sig.action == strategy.SELL:
                exit_signals.append(sig)
        except Exception as exc:
            say(f"    {pcode} 退出判断失败：{exc}")

    if not state.positions:
        detail = "空仓，无持仓需要体检"
        st = SKIP
    elif exit_signals:
        detail = f"持仓 {len(state.positions)} 只，{len(exit_signals)} 只触发退出条件"
        st = WARN
    else:
        detail = f"持仓 {len(state.positions)} 只，均未触发退出条件"
        st = OK
    res.stages.append(Stage(3, "持仓体检", st, detail, time.time() - t0,
                            {"positions": len(state.positions),
                             "exits": len(exit_signals)}))
    say(f"  {detail}")

    # ---------------------------------------------------------- ④ 信号扫描
    t0 = time.time()
    buy_signals = []
    if code:
        target = normalize_code(code)
        cand = next((c for c in u.kept + u.dropped if c.code == target), None)
        if cand is None:
            res.stages.append(Stage(4, "信号扫描", FAIL,
                                    f"候选池里没有 {target}", time.time() - t0))
            res.headline = f"找不到 {target}"
            res.headline_level = FAIL
            return res
        bars = store.get(target)
        sig = strategy.evaluate(cfg, cand, bars)
        if sig is None:
            res.stages.append(Stage(4, "信号扫描", FAIL,
                                    f"{target} 历史数据不足", time.time() - t0))
            res.headline = f"{target} 数据不足"
            res.headline_level = FAIL
            return res
        buy_signals = [sig]
        detail = (f"指定标的 {target}，动作 {sig.action}"
                  + ("（策略未给买入信号，将走强制路径）" if sig.action != strategy.BUY else ""))
    else:
        pool = u.kept[:limit]
        buy_signals = strategy.scan(cfg, pool, store.get)
        detail = (f"扫描 {len(pool)} 只 → {len(buy_signals)} 个买入信号"
                  if buy_signals else
                  f"扫描 {len(pool)} 只 → 没有标的满足全部买入条件")

    res.stages.append(Stage(4, "信号扫描",
                            OK if buy_signals else SKIP, detail, time.time() - t0,
                            {"scanned": len(u.kept[:limit]), "signals": len(buy_signals),
                             "fetched": store.fetches, "cached": store.hits}))
    say(f"  {detail}")

    # ---------------------------------------------------------- ⑤⑥⑦ 逐候选推进
    acc = journal.to_account_state(state,
                                   {c: q.price for c, q in quotes.items()})

    candidates: list[tuple] = []
    for sig in buy_signals:
        decision = risk.evaluate_entry(cfg, acc, sig.code, sig.name, sig.price,
                                       atr_value=sig.atr, in_pool=True, t0=sig.t0)
        # 指定了代码却没给信号时，留痕而不是默默出单
        if code and sig.action != strategy.BUY and not ignore_signal:
            from .risk import Check, BLOCK
            why = "；".join(sig.reasons) or "策略未给出买入信号"
            decision.checks.insert(0, Check(
                "策略信号", BLOCK,
                f"该标的动作为 {sig.action}，不是买入信号。原因：{why}。"
                f"加 --ignore-signal 表示这是你的决定，不是系统的建议。"))
            decision.allowed = False
        candidates.append((sig, decision))

    # 卖出优先：先落卖出待办，再落买入
    created: list[Todo] = []
    for sig in exit_signals:
        try:
            t = todos.create(
                kind=SELL, code=sig.code, name=sig.name, price=sig.price,
                shares=state.positions[sig.code].shares if sig.code in state.positions else 0,
                notional=0.0,
                signal=sig.to_dict(),
                risk={"allowed": True, "checks": [{
                    "name": "卖出不受闸门限制", "level": "ok",
                    "detail": "风控只拦买入；止损在任何情况下都必须能执行"}]},
                ai={"decision": "skip", "summary": "卖出不经 AI 复核，退出规则是确定性的"},
                data_health=res.data_health,
                initial_state=PENDING_CONFIRM,
                reason="退出条件触发：" + "；".join(sig.reasons[:2]),
            )
            created.append(t)
        except TodoError as exc:
            say(f"    卖出待办创建失败：{exc}")

    ai_ran = 0
    for sig, decision in candidates:
        if len(created) >= max_todos:
            break
        if not decision.allowed:
            reason = "；".join(c.detail for c in decision.blocks[:2])
            try:
                t = todos.create(
                    kind=BUY, code=sig.code, name=sig.name, price=sig.price,
                    shares=decision.shares, notional=decision.notional,
                    signal=sig.to_dict(), risk=decision.to_dict(),
                    ai={}, economics=_economics(cfg, sig, decision),
                    data_health=res.data_health,
                    initial_state=REJECTED, reason=f"风控拦截：{reason}",
                )
                created.append(t)
            except TodoError as exc:
                say(f"    拦截待办创建失败：{exc}")
            continue

        # AI 复核
        board = None
        if use_ai and cfg.ai.enabled:
            ctx = _ai_context(cfg, sig, decision, store, quotes)
            board = ai_review.review(cfg, ctx, dry_run=ai_dry_run)
            ai_ran += 1
        state_to = PENDING_CONFIRM
        why = "风控与 AI 复核均通过"
        if board is not None and board.decision == "block":
            state_to = REJECTED
            why = f"AI 否决：{board.summary}"

        try:
            t = todos.create(
                kind=BUY, code=sig.code, name=sig.name, price=sig.price,
                shares=decision.shares, notional=decision.notional,
                signal=sig.to_dict(), risk=decision.to_dict(),
                ai=board.to_dict() if board else
                   {"decision": "skip", "summary": "未启用 AI 复核"},
                economics=_economics(cfg, sig, decision),
                data_health=res.data_health,
                initial_state=state_to, reason=why,
            )
            created.append(t)
        except TodoError as exc:
            say(f"    待办创建失败：{exc}")

    # 闸门段的汇总
    n_block = sum(1 for t in created if t.state == REJECTED and t.blocked)
    n_ai = sum(1 for t in created if t.state == REJECTED and not t.blocked)
    res.stages.append(Stage(5, "风控闸门",
                            WARN if n_block else OK,
                            f"{len(candidates)} 个候选 → 拦截 {n_block}，放行 "
                            f"{len(candidates) - n_block}",
                            0.0, {"blocked": n_block,
                                  "passed": len(candidates) - n_block}))
    res.stages.append(Stage(6, "AI 复核",
                            SKIP if not use_ai else (WARN if n_ai else OK),
                            ("已跳过（--no-ai）" if not use_ai else
                             f"复核 {ai_ran} 个候选 → AI 否决 {n_ai}"),
                            0.0, {"ran": ai_ran, "vetoed": n_ai}))
    res.stages.append(Stage(7, "生成待办", OK,
                            f"新增 {len(created)} 条待办"
                            f"（卖出 {len(exit_signals)}，买入 {len(created) - len(exit_signals)}）",
                            0.0, {"created": len(created)}))

    res.todos = created
    res.headline, res.headline_level = _headline(created, candidates, exit_signals,
                                                 bool(buy_signals))
    say(f"  总耗时 {time.time() - t_start:.1f}s")
    return res


def _economics(cfg: Config, sig, decision) -> dict:
    n = decision.notional
    rt = cfg.fees.round_trip(n) if n else 0.0
    slip_one_way = n * (cfg.fees.slippage_bps / 10000.0) if n else 0.0
    comm_one_way = (max(n * cfg.fees.commission_rate, cfg.fees.min_commission)
                    if n else 0.0)
    return {
        "shares": decision.shares,
        "notional": round(n, 2),
        "buy_fee": round(cfg.fees.one_way(n), 2) if n else 0.0,
        "round_trip_cost": round(rt, 2),
        "breakeven_pct": round(rt / n, 6) if n else 0.0,
        "breakeven_price": round(sig.price * (1 + rt / n), 3) if n else 0.0,
        "stop": sig.stop, "target": sig.target,
        "required_win_rate": round(decision.required_win_rate, 4),
        "risk_amount": round(max(0.0, sig.price - sig.stop) * decision.shares, 2)
        if decision.shares else 0.0,
        # 费用明细。只给总额会让模型自己重算时对不上 —— 实测 Claude 就
        # 因为发现「差额 0.18 元」判定「费用算术自相矛盾」，那其实是滑点，
        # 只是原来没在简报里说明。
        "fee_commission_one_way": round(comm_one_way, 2),
        "fee_slippage_one_way": round(slip_one_way, 2),
        "fee_slippage_bps": cfg.fees.slippage_bps,
        "fee_min_commission_bound": cfg.fees.is_min_commission_bound(n) if n else False,
    }


def _ai_context(cfg: Config, sig, decision, store: BarStore, quotes) -> dict:
    bars = store.get(sig.code)
    closes = [round(b.close, 3) for b in bars[-20:]]
    eco = _economics(cfg, sig, decision)
    return {
        "account": {"total_capital": cfg.capital, "cash": cfg.cash,
                    "market_value": 0.0, "month_round_trips": 0},
        "fees": {"commission_rate": cfg.fees.commission_rate,
                 "min_commission": cfg.fees.min_commission,
                 "stamp_duty_rate": cfg.fees.stamp_duty_rate,
                 "slippage_bps": cfg.fees.slippage_bps},
        "signal": {**sig.to_dict(),
                   "ma_fast_win": cfg.strategy.ma_fast,
                   "ma_slow_win": cfg.strategy.ma_slow,
                   "mom_window": cfg.strategy.mom_window,
                   "pct_window": cfg.strategy.pct_window,
                   "shares": decision.shares,
                   "notional": round(decision.notional, 2),
                   "recent_closes": closes},
        "economics": {
            "buy_cost": eco["buy_fee"], "round_trip_cost": eco["round_trip_cost"],
            "fee_ratio": eco["breakeven_pct"], "breakeven_pct": eco["breakeven_pct"],
            "risk_amount": eco["risk_amount"],
            "risk_cost_multiple": (round(eco["risk_amount"] / eco["round_trip_cost"], 2)
                                   if eco["round_trip_cost"] else 0.0),
            "commission_one_way": eco["fee_commission_one_way"],
            "slippage_one_way": eco["fee_slippage_one_way"],
            "slippage_bps": eco["fee_slippage_bps"],
            "min_commission_bound": eco["fee_min_commission_bound"],
            "required_win_rate": eco["required_win_rate"],
        },
        "checks": [c.to_dict() for c in decision.checks],
        "reasons": sig.reasons or ["中期趋势向上、动量为正、价格不在历史高位、波动适中"],
        "warnings": sig.warnings,
    }


def _headline(created: list[Todo], candidates: list, exits: list,
              had_signals: bool) -> tuple[str, str]:
    """一句话结论 —— 工作台首页最大的那行字。

    规则：有未完成的待办就喊出来（卖出优先），没有就明确说「不用动手」，
    并且要区分「没信号」和「有信号但被拦下」—— 后者是系统在干活，不是没事发生。
    """
    sells = [t for t in created if t.kind == SELL and t.is_open]
    buys = [t for t in created if t.kind == BUY and t.is_open]

    if sells:
        extra = f"，另有 {len(buys)} 条买入待确认" if buys else ""
        return f"⚠️ 有 {len(sells)} 条卖出待办，先处理卖出{extra}", WARN
    if buys:
        return f"有 {len(buys)} 条买入待办待确认", WARN

    # 到这里说明没有任何未完成的待办
    if created:
        n_block = sum(1 for t in created if t.blocked)
        if n_block == len(created):
            return f"今天 {n_block} 个候选全部被拦下，不需要动手（原因见待办）", OK
        return f"今天 {len(created)} 条待办已关闭，不需要动手", OK
    if candidates:
        return f"今天没有可执行的信号（{len(candidates)} 个候选被风控拦下）", OK
    if not had_signals:
        return "今天不需要操作 —— 没有标的满足买入条件", OK
    return "今天没有需要你动手的事", OK
