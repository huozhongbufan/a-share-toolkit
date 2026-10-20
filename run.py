#!/usr/bin/env python
"""etf-copilot 命令行入口。

    python run.py fee-check      先跑这个。它会告诉你 500 元本金在你券商费率下能不能交易。
    python run.py universe       构建候选池(买得起 + 排雷 + 流动性)
    python run.py scan           扫描信号,给出候选排序
    python run.py ticket         完整流程 -> 生成一张可执行/已拦截的工单
    python run.py status         看持仓、现金、权益
    python run.py fill           登记一笔真实成交
    python run.py sell           对已持仓标的判断该不该卖
    python run.py backtest       回测,回答『赚的钱够不够付手续费』
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

# 中文 Windows 控制台默认 GBK,直接 print 中文可能抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ec.config import load_config, ensure_dirs, ConfigError, Config  # noqa: E402
from ec import ai_review, backtest as bt, journal as jr, risk, strategy, ticket as tk, universe as uni  # noqa: E402
from ec.dataloader import BarStore  # noqa: E402
from ec.quotes import fetch_quotes  # noqa: E402


def hr(title: str = "") -> None:
    line = "─" * 68
    print(line)
    if title:
        print(title)
        print(line)


def _money(v: float) -> str:
    """小额的 API 花费要显示到 3 位，否则 ¥0.002 会显示成 ¥0.00。"""
    if v == 0:
        return "0.00"
    if abs(v) < 0.01:
        return f"{v:.3f}"
    if abs(v) < 1:
        return f"{v:.2f}"
    return f"{v:,.2f}"


def write_report(cfg: Config, name: str, text: str) -> str:
    os.makedirs(cfg.out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(cfg.out_dir, f"{name}-{stamp}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    latest = os.path.join(cfg.out_dir, f"{name}-latest.md")
    with open(latest, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# ---------------------------------------------------------------- fee-check

def cmd_fee_check(cfg: Config, args) -> int:
    f = cfg.fees
    r = cfg.risk

    hr("费用体检")
    print(f"本金 {cfg.capital:.2f} 元")
    print(f"佣金率 {f.commission_rate} ({f.commission_rate*10000:.2f}‱) ｜ "
          f"最低佣金 {f.min_commission:.2f} 元 ｜ 滑点 {f.slippage_bps} bp")
    print(f"印花税 {f.stamp_duty_rate}（场内 ETF 免征）｜ 过户费 {f.transfer_fee_rate}")
    print()

    print("单笔金额 → 往返费用 / 盈亏平衡涨幅")
    print(f"{'金额':>8} {'往返费用':>10} {'占本金':>8} {'盈亏平衡':>10}  {'最低佣金卡住':>10}")
    for n in (100, 200, 300, 400, 500, 1000, 2000, 5000, 20000, 50000):
        rt = f.round_trip(n)
        print(f"{n:>8} {rt:>10.2f} {rt/cfg.capital*100:>7.2f}% "
              f"{f.breakeven_pct(n)*100:>9.3f}%  {'是' if f.is_min_commission_bound(n) else '否':>10}")
    print()

    min_viable = risk.max_viable_notional(cfg)
    free_cap = risk.required_capital_for_free_trading(cfg)
    limit = r.max_fee_ratio_per_trade

    hr("结论")
    if f.min_commission <= 0:
        print("你配置的是『免五』券商。这个费率下 500 元本金可以正常交易：")
        rt429 = f.round_trip(429.0)
        print(f"  一笔 429 元的往返费用是 {rt429:.2f} 元 = {rt429/429*100:.3f}%，"
              f"远低于 {limit*100:.2f}% 的上限。")
        print("  也就是说，费用不再是主要矛盾，策略和纪律才是。")
    else:
        print(f"你配置的券商有 {f.min_commission:.0f} 元最低佣金。这是一个很硬的约束：")
        print(f"  · 单笔要做到 {min_viable:.0f} 元，费用占比才不超 {limit*100:.2f}% 的上限")
        print(f"  · 单笔要做到 {free_cap:.0f} 元，最低佣金才不再起作用")
        print(f"  · 你的本金是 {cfg.capital:.0f} 元，是前者的 {cfg.capital/min_viable*100:.0f}%，"
              f"后者的 {cfg.capital/free_cap*100:.0f}%")
        print()
        print("这意味着两种情况之一：")
        print("  1) 你的券商对『场内基金』免五 —— 那去问清楚，把 config.json 里的")
        print("     min_commission 改成 0，整个系统就活了。很多券商确实对 ETF 免五。")
        print(f"  2) 真的收 5 元 —— 那 500 元本金下每次往返先亏 {f.round_trip(cfg.capital*0.9)/cfg.capital*100:.1f}%，")
        print("     唯一理性的做法是极低频：一年做 2~4 次，只在大级别信号出现时动手。")
        print("     系统默认会拦截，这是有意的，不是 bug。")
        print()
        print(f"  如果明知费用重还是要做，把 config.json 里 risk.max_fee_ratio_per_trade")
        print(f"  从 {limit} 调到 {f.round_trip(429.0)/429.0:.4f} 左右，闸门就会放行 ——")
        print("  但请知道你在做什么。")
    print()

    # 仓位上限对可买范围的影响
    hr("仓位上限对『买得起』的影响")
    print(f"max_position_pct = {r.max_position_pct} → 单笔预算 "
          f"{cfg.capital*r.max_position_pct:.0f} 元 → 可买最高价 "
          f"{cfg.capital*r.max_position_pct/100:.3f} 元")
    for pct in (0.60, 0.70, 0.80, 0.85, 0.95, 1.00):
        budget = cfg.capital * pct
        print(f"  {pct*100:>3.0f}%  →  预算 {budget:>5.0f} 元  →  价格上限 {budget/100:.3f} 元")
    print()
    print("提醒：500 元本金下 1 手(100份)就是最小交易单位，仓位粒度极粗，")
    print("      max_position_pct 几乎不起保护作用。真正干活的是费用占比、")
    print("      月度交易频次和熔断三条。")
    return 0


# ---------------------------------------------------------------- universe

def cmd_universe(cfg: Config, args) -> int:
    hr("构建候选池")
    u = uni.build_universe(cfg, force_refresh=args.refresh)

    print(f"本金 {u.capital:.0f} 元 ｜ 1 手预算上限 {u.max_lot_cost:.2f} 元 ｜ 数据源 {u.source}")
    print(f"全市场 ETF 共 {len(u.kept) + len(u.dropped)} 只，"
          f"通过 {len(u.kept)} 只，排除 {len(u.dropped)} 只")
    print()

    print(f"{'代码':>8} {'名称':<20} {'现价':>7} {'1手成本':>9} {'成交额(万)':>10} {'T+0':>4}")
    for c in u.kept[:args.top]:
        print(f"{c.code:>8} {c.name:<20} {c.price:>7.3f} {c.lot_cost:>9.2f} "
              f"{c.amount_wan:>10.0f} {'是' if c.t0 else '否':>4}")
    if len(u.kept) > args.top:
        print(f"... 另有 {len(u.kept)-args.top} 只")

    if args.show_dropped:
        print()
        print("被排除的（前 20 条）")
        for c in u.dropped[:20]:
            print(f"  {c.code} {c.name}: {'; '.join(c.reasons)}")

    path = os.path.join(cfg.data_dir, "universe.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(u.to_dict(), fh, ensure_ascii=False, indent=2)
    print()
    print(f"完整候选池已写入 {path}")
    return 0


# ---------------------------------------------------------------- scan

def cmd_scan(cfg: Config, args) -> int:
    hr("信号扫描")
    store = BarStore(cfg)
    u = uni.build_universe(cfg, force_refresh=args.refresh)
    pool = u.kept[:args.limit]
    print(f"候选 {len(pool)} 只（按成交额取前 {args.limit}），开始拉日线…")

    signals = strategy.scan(cfg, pool, store.get)
    print(f"日线请求 {store.fetches} 次，缓存命中 {store.hits} 次")
    print()

    if not signals:
        print("没有任何标的同时满足全部买入条件。")
        print("这是正常结果 —— 大部分时间市场就是不满足条件，空仓也是仓位。")
    else:
        print(f"通过筛选的候选（{len(signals)} 只，按分数排序）：")
        print(f"{'代码':>8} {'名称':<20} {'现价':>7} {'1手':>7} {'分数':>6} "
              f"{'ATR%':>6} {'动量':>7} {'分位':>5} {'止损':>7} {'止盈':>7}")
        for s in signals[:args.top]:
            print(f"{s.code:>8} {s.name:<20} {s.price:>7.3f} {s.lot_cost:>7.1f} "
                  f"{s.score:>6.3f} {s.atr_pct*100:>5.2f}% {s.mom*100:>+6.2f}% "
                  f"{s.pct_rank*100:>4.0f}% {s.stop:>7.3f} {s.target:>7.3f}")
            for w in s.warnings:
                print(f"          ⚠ {w}")

    # 也报告一下被刷掉的原因分布,方便调参
    from collections import Counter
    reasons: Counter = Counter()
    for cand in pool:
        bars = store.get(cand.code)
        sig = strategy.evaluate(cfg, cand, bars)
        if sig and sig.action != strategy.BUY:
            for rr in sig.reasons:
                key = rr.split("(")[0].split(",")[0][:22]
                reasons[key] += 1
    if reasons:
        print()
        print("未通过的原因分布：")
        for k, v in reasons.most_common(8):
            print(f"  {v:>4} 只  {k}")
    return 0


# ---------------------------------------------------------------- ticket

def _build_context(cfg: Config, sig, decision, bars_close: list[float]) -> dict:
    shares = decision.shares
    notional = decision.notional
    buy_cost = cfg.fees.one_way(notional) if notional else 0.0
    rt = cfg.fees.round_trip(notional) if notional else 0.0
    risk_amount = max(0.0, (sig.price - sig.stop)) * shares if shares else 0.0
    return {
        "account": {
            "total_capital": cfg.capital,
            "cash": cfg.cash,
            "market_value": 0.0,
            "month_round_trips": 0,
        },
        "fees": {
            "commission_rate": cfg.fees.commission_rate,
            "min_commission": cfg.fees.min_commission,
            "stamp_duty_rate": cfg.fees.stamp_duty_rate,
            "slippage_bps": cfg.fees.slippage_bps,
        },
        "signal": {
            **sig.to_dict(),
            "ma_fast_win": cfg.strategy.ma_fast,
            "ma_slow_win": cfg.strategy.ma_slow,
            "mom_window": cfg.strategy.mom_window,
            "pct_window": cfg.strategy.pct_window,
            "shares": shares,
            "notional": round(notional, 2),
            "recent_closes": [round(c, 3) for c in bars_close[-20:]],
        },
        "economics": {
            "buy_cost": round(buy_cost, 2),
            "round_trip_cost": round(rt, 2),
            "fee_ratio": round(rt / notional, 6) if notional else 0.0,
            "breakeven_pct": round(rt / notional, 6) if notional else 0.0,
            "risk_amount": round(risk_amount, 2),
            "risk_cost_multiple": round(risk_amount / rt, 2) if rt else 0.0,
        },
        "checks": [c.to_dict() for c in decision.checks],
        "reasons": sig.reasons or ["中期趋势向上、动量为正、价格不在历史高位、波动水平适中"],
        "warnings": sig.warnings,
    }


def cmd_ticket(cfg: Config, args) -> int:
    store = BarStore(cfg)
    j = jr.Journal(cfg)
    state = j.load_state()

    hr("生成工单")

    # 有持仓 → 走卖出判断
    if state.positions and not args.force_buy:
        code = args.code or next(iter(state.positions))
        from ec.quotes import normalize_code
        code = normalize_code(code)
        pos = state.positions.get(code)
        if not pos:
            print(f"{code} 不在持仓里。")
            return 1
        bars = store.get(code)
        if args.code:
            q = fetch_quotes([code])
            price = q[code].price if code in q else bars[-1].close
        else:
            price = bars[-1].close
        sig = strategy.evaluate_exit(cfg, code, pos.name, pos.cost_price, bars, pos.t0)
        sig.price = price
        sig.lot_cost = price * 100

        print(f"持仓 {code} {pos.name} {pos.shares} 份，成本 {pos.cost_price:.3f}")
        print(f"现价 {price:.3f}，浮动盈亏 {(price-pos.cost_price)*pos.shares:+.2f} 元 "
              f"({(price/pos.cost_price-1)*100:+.2f}%)")
        print(f"判断：{sig.action}")
        for rr in sig.reasons:
            print(f"  · {rr}")
        print()
        print(f"止损位 {sig.stop:.3f} ｜ 止盈位 {sig.target:.3f}")

        if sig.action == strategy.SELL:
            notional = pos.shares * price
            fee = cfg.fees.one_way(notional)
            print()
            print(f"建议卖出 {pos.shares} 份，成交额约 {notional:.2f} 元，"
                  f"卖出费用约 {fee:.2f} 元")
            print("注意：卖出不受建仓风控闸门限制。止损任何情况下都允许执行。")
            j.record_note(f"生成卖出建议 {code}: {'; '.join(sig.reasons)}")
        else:
            print()
            print("继续持有，不需要操作。")
        return 0

    # 空仓 → 走建仓判断
    if not args.code:
        u = uni.build_universe(cfg)
        pool = u.kept[:args.limit]
        print(f"候选 {len(pool)} 只，扫描信号中…")
        signals = strategy.scan(cfg, pool, store.get)
        if not signals:
            print()
            print("今天没有符合条件的买入信号，不出单。")
            print("空仓也是仓位。没有信号就不动，这是纪律的一部分。")
            return 0
        pick = signals[0]
        print(f"最优信号：{pick.code} {pick.name} 分数 {pick.score}")
    else:
        from ec.quotes import normalize_code
        code = normalize_code(args.code)
        u = uni.build_universe(cfg)
        cand = next((c for c in u.kept if c.code == code), None)
        if not cand:
            cand = next((c for c in u.dropped if c.code == code), None)
            if cand:
                print(f"{code} 不在候选池里：{'; '.join(cand.reasons)}")
                return 1
            print(f"找不到 {code}")
            return 1
        bars = store.get(code)
        pick = strategy.evaluate(cfg, cand, bars)
        if not pick:
            print(f"{code} 数据不足，无法评估。")
            return 1
        print(f"指定标的：{pick.code} {pick.name} 动作 {pick.action}")

    bars = store.get(pick.code)
    closes = [b.close for b in bars]

    # 实时价更准,拿得到就用实时价
    try:
        q = fetch_quotes([pick.code])
        if pick.code in q and q[pick.code].price > 0:
            pick.price = q[pick.code].price
            pick.lot_cost = pick.price * 100
    except Exception:
        pass

    acc = j.to_account_state(state, {pick.code: pick.price})

    print()
    print("风控闸门：")
    decision = risk.evaluate_entry(
        cfg, acc, pick.code, pick.name, pick.price,
        atr_value=pick.atr, in_pool=True, t0=pick.t0,
    )
    # 显式指定代码时,用户可能挑了一只策略并不看好的标的。
    # 这种情况必须在闸门里留痕,而不是默默地出一张单子。
    # 保留 shares/notional 不归零,是为了让工单仍然能显示费用和盈亏平衡 —— 拦截
    # 的理由要看得见,不能只是一句『不允许』。
    if pick.action != strategy.BUY and not args.ignore_signal:
        from ec.risk import Check, BLOCK
        why = "；".join(pick.reasons) or "策略未给出买入信号"
        decision.checks.insert(0, Check(
            "策略信号", BLOCK,
            f"该标的当前动作为 {pick.action}，不是买入信号。原因：{why}。"
            f"如果你就是想买，加 --ignore-signal 表示这是你的决定，不是系统的建议。",
        ))
        decision.allowed = False
    for c in decision.checks:
        mark = {"ok": "通过", "warn": "警告", "block": "拦截"}[c.level]
        print(f"  [{mark}] {c.name}: {c.detail}")

    print()
    ctx = _build_context(cfg, pick, decision, closes)
    ctx["account"]["cash"] = round(acc.cash, 2)
    ctx["account"]["market_value"] = round(acc.market_value, 2)
    ctx["account"]["month_round_trips"] = acc.month_round_trips

    if args.no_ai:
        board = ai_review.ReviewBoard(status="unavailable", decision="allow",
                                      summary="已用 --no-ai 跳过 AI 复核。")
    else:
        print("调用多模型交叉复核（可能耗时 1~3 分钟）…")
        board = ai_review.review(cfg, ctx, dry_run=args.dry_run, only=args.only)
        for r in board.results:
            head = f"  {r.model_id}: "
            if r.status != "ok":
                print(head + f"未取得意见（{r.status}）{r.error}")
            else:
                print(head + f"{r.verdict} 把握{r.confidence:.0%} | 最大风险: {r.top_risk}")
        print(f"  结论: {board.summary}")

    t = tk.build_ticket(cfg, pick, decision, board)
    paths = tk.write_ticket(cfg, t)
    j.record_ticket(t.to_dict())

    print()
    hr(f"工单状态：{t.status}")
    print(t.status_reason)
    print()
    print(f"标的 {t.code} {t.name} ｜ 现价 {t.price:.3f} ｜ {t.shares} 份 ｜ "
          f"金额 {t.notional:.2f} 元")
    print(f"往返费用 {t.round_trip_cost:.2f} 元 ｜ 盈亏平衡涨幅 {t.breakeven_pct*100:.3f}% "
          f"｜ 盈亏平衡价 {t.breakeven_price:.3f}")
    print(f"止损 {t.stop:.3f} ｜ 止盈 {t.target:.3f} ｜ 止损时亏 {t.risk_amount:.2f} 元")
    print()
    print(f"工单已写入：{paths['latest']}")
    return 0 if t.status == tk.EXECUTABLE else 2


# ---------------------------------------------------------------- status / fill

def cmd_status(cfg: Config, args) -> int:
    j = jr.Journal(cfg)
    state = j.load_state()

    hr("账户状态")
    codes = list(state.positions)
    prices: dict[str, float] = {}
    if codes:
        try:
            q = fetch_quotes(codes)
            prices = {c: q[c].price for c in codes if c in q}
        except Exception:
            pass

    mv = sum(p.market_value(prices.get(c, p.cost_price))
             for c, p in state.positions.items())
    equity = state.cash + mv
    print(f"本金 {cfg.capital:.2f} ｜ 现金 {state.cash:.2f} ｜ 持仓市值 {mv:.2f} "
          f"｜ 总权益 {equity:.2f} （{(equity/cfg.capital-1)*100:+.2f}%）")
    print(f"峰值权益 {state.peak_equity:.2f} ｜ 累计已实现盈亏 "
          f"{state.realized_pnl_total:+.2f} ｜ 本月往返 {state.month_round_trips} 次")
    print()

    if not state.positions:
        print("当前空仓。")
    else:
        print(f"{'代码':>8} {'名称':<18} {'份数':>6} {'成本':>8} {'现价':>8} "
              f"{'市值':>8} {'浮动盈亏':>10} {'占比':>6}")
        for code, p in state.positions.items():
            px = prices.get(code, p.cost_price)
            val = p.market_value(px)
            print(f"{code:>8} {p.name:<18} {p.shares:>6} {p.cost_price:>8.3f} "
                  f"{px:>8.3f} {val:>8.2f} {p.pnl(px):>+10.2f} "
                  f"{val/equity*100 if equity else 0:>5.1f}%")

    events = j.events()
    if events:
        print()
        print(f"最近 8 条流水（共 {len(events)} 条）：")
        for ev in events[-8:]:
            et = ev.get("type")
            if et in (jr.BUY, jr.SELL):
                print(f"  {ev['at']} {et} {ev['code']} {ev['shares']}份 @ {ev['price']}")
            elif et == jr.TICKET:
                print(f"  {ev['at']} 工单 {ev.get('ticket_id')} {ev.get('action')} "
                      f"{ev.get('code')} 风控={ev.get('risk_allowed')} AI={ev.get('ai_decision')}")
            else:
                print(f"  {ev['at']} {et} {str(ev.get('text',''))[:40]}")
    return 0


def cmd_fill(cfg: Config, args) -> int:
    j = jr.Journal(cfg)
    hr("登记成交")
    code = args.code
    if not code:
        code = input("代码（如 510300）: ").strip()
    from ec.quotes import normalize_code
    code = normalize_code(code)
    side = (args.side or input("方向 buy/sell: ").strip()).upper()
    side = jr.BUY if side.startswith("B") else jr.SELL
    price = float(args.price if args.price is not None else input("成交价: ").strip())
    shares = int(args.shares if args.shares is not None else input("份数: ").strip())
    name = args.name or ""
    if not name:
        try:
            q = fetch_quotes([code])
            name = q[code].name if code in q else code
        except Exception:
            name = code

    notional = price * shares
    fee = cfg.fees.one_way(notional)
    print(f"{'买入' if side == jr.BUY else '卖出'} {code} {name} {shares} 份 @ {price}")
    print(f"成交额 {notional:.2f} 元，估算费用 {fee:.2f} 元")
    if input("确认登记？(y/N) ").strip().lower() != "y":
        print("已取消。")
        return 0

    j.record_fill(code, name, side, price, shares, fee, t0=False,
                  note=args.note or "")
    state = j.load_state()
    print(f"已登记。现金 {state.cash:.2f} 元，持仓 {len(state.positions)} 只。")
    return 0


# ---------------------------------------------------------------- backtest

def cmd_backtest(cfg: Config, args) -> int:
    hr("回测")
    store = BarStore(cfg)
    u = uni.build_universe(cfg, force_refresh=args.refresh)

    if args.codes:
        from ec.quotes import normalize_code
        wanted = [normalize_code(c) for c in args.codes]
        picks = [c for c in u.kept + u.dropped if c.code in wanted]
        missing = [c for c in wanted if c not in {p.code for p in picks}]
        if missing:
            print(f"候选池里没有: {missing}")
            return 1
    else:
        picks = u.kept[:args.limit]

    print(f"回测 {len(picks)} 只 ETF，本金 {cfg.capital:.0f} 元，"
          f"最低佣金 {cfg.fees.min_commission:.0f} 元")
    results = []
    for i, cand in enumerate(picks, 1):
        bars = store.get(cand.code, 500)
        res = bt.backtest_etf(cfg, cand.code, cand.name, bars)
        if res:
            results.append(res)
        if i % 10 == 0:
            print(f"  …{i}/{len(picks)}")

    if not results:
        print("没有可回测的标的（历史数据不足）。")
        return 1

    results.sort(key=lambda r: r.strategy_return, reverse=True)
    text = bt.render_backtest_markdown(results, cfg)
    path = write_report(cfg, "backtest", text)

    traded = [r for r in results if r.n_round_trips > 0]
    never = [r for r in results if r.n_round_trips == 0]

    print()
    if traded:
        print(f"触发过交易的 {len(traded)} 只（按策略收益排序）：")
        print(f"{'代码':>8} {'名称':<18} {'策略':>8} {'持有':>8} {'次数':>4} "
              f"{'费用':>7} {'费用/本金':>9} {'平衡点':>8} {'胜率':>5}")
        for r in traded[:args.top]:
            print(f"{r.code:>8} {r.name:<18} {r.strategy_return*100:>+7.2f}% "
                  f"{r.hold_return*100:>+7.2f}% {r.n_round_trips:>4} {r.total_fees:>7.2f} "
                  f"{r.fee_pct_of_capital*100:>8.2f}% {r.avg_breakeven_pct*100:>7.3f}% "
                  f"{r.win_rate*100:>4.0f}%")
    if never:
        names = "、".join(f"{r.code[2:]}{r.name}" for r in never[:6])
        more = f" 等 {len(never)} 只" if len(never) > 6 else ""
        print()
        print(f"整段历史里一次都没买过的 {len(never)} 只：{names}{more}")
        print("（这些显示 0.00% 不是『不赚不亏』，是策略根本没参与。）")

    avg_strat = sum(r.strategy_return for r in results) / len(results)
    avg_hold = sum(r.hold_return for r in results) / len(results)
    print()
    print(f"全样本平均：策略 {avg_strat*100:+.2f}% vs 买入持有 {avg_hold*100:+.2f}%"
          f"（共 {len(results)} 只，其中 {len(traded)} 只触发过交易）")
    if traded:
        avg_fee_pct = sum(r.fee_pct_of_capital for r in traded) / len(traded)
        avg_be = sum(r.avg_breakeven_pct for r in traded) / len(traded)
        t_strat = sum(r.strategy_return for r in traded) / len(traded)
        t_hold = sum(r.hold_return for r in traded) / len(traded)
        print(f"有过交易的标的：平均每次往返盈亏平衡 {avg_be*100:.3f}%，"
              f"累计手续费平均吃掉本金的 {avg_fee_pct*100:.2f}%")
        print(f"只看这 {len(traded)} 只：策略 {t_strat*100:+.2f}% vs 持有 {t_hold*100:+.2f}%")
        if t_strat < t_hold:
            print("→ 策略跑输买入持有。降频，或换一家对场内基金免五的券商，二选一。")
    print(f"详细报告：{path}")
    return 0


# ---------------------------------------------------------------- plan

def cmd_plan(cfg: Config, args) -> int:
    from ec import plan as pl
    from ec.quotes import normalize_code

    hr("定投计划")
    store = BarStore(cfg)
    u = uni.build_universe(cfg)

    code = normalize_code(args.code) if args.code else None
    if code:
        cand = next((c for c in u.kept if c.code == code), None)
        if cand is None:
            cand = next((c for c in u.dropped if c.code == code), None)
            if cand is None:
                print(f"候选池里没有 {code}")
                return 1
            print(f"注意：{code} 不在可买列表里（{'; '.join(cand.reasons)}），下面按它算只是给你看数字。")
    else:
        # 没指定就选池子里成交额最大的宽基 —— 宽基最适合长期持有
        broad = [c for c in u.kept if c.category == "broad"]
        if not broad:
            print("候选池里没有宽基 ETF，请用 --code 指定。")
            return 1
        cand = max(broad, key=lambda c: c.amount_wan)
        print(f"未指定标的，默认选成交额最大的宽基：{cand.code} {cand.name}")

    plan = pl.build_plan(cfg, cand.code, cand.name, cand.price,
                         monthly=args.monthly, months=args.months)

    print()
    print(f"{cand.code} {cand.name} ｜ 现价 {cand.price:.3f} ｜ 1 手 {plan.lot_cost:.2f} 元")
    print(f"初始 {plan.initial:.0f} 元 ｜ 每期追加 {plan.monthly:.0f} 元 ｜ {plan.months} 期")
    print()
    print(f"{'期数':>6} {'可用资金':>9} {'手数':>5} {'金额':>9} {'费用':>7} "
          f"{'费用占比':>8} {'平衡点':>8} {'剩余':>8}")
    for i in plan.installments:
        print(f"{i.label:>6} {i.cash_available:>9.2f} {i.lots:>5} {i.notional:>9.2f} "
              f"{i.fee:>7.2f} {i.fee_ratio*100:>7.2f}% {i.breakeven_pct*100:>7.3f}% "
              f"{i.cash_after:>8.2f}"
              + (f"  {i.note}" if i.note else ""))
    print()
    print(f"合计投入 {plan.total_invested:.2f} 元，{plan.total_shares} 份，"
          f"手续费 {plan.total_fee:.2f} 元（{plan.fee_pct_of_invested*100:.2f}%），"
          f"摊薄成本 {plan.avg_cost:.4f}")
    print()
    print(f"结论：{plan.verdict}")
    for w in plan.warnings:
        print(f"  ⚠ {w}")

    comps = []
    if not args.no_compare:
        targets = [cand] if code else [c for c in u.kept if c.category == "broad"][:args.compare]
        print()
        print("历史对照（一次性买入 vs 分批买入）：")
        print(f"{'代码':>8} {'名称':<18} {'一次性均价':>11} {'分批均价':>10} "
              f"{'一次性收益':>11} {'分批收益':>10} {'胜出':>10}")
        for c in targets:
            bars = store.get(c.code, 500)
            comp = pl.historical_dca_vs_lump(
                cfg, c.code, c.name, bars, plan.initial + plan.monthly * plan.months,
                tranches=max(2, min(args.months, 8)))
            if comp:
                comps.append(comp)
                print(f"{comp.code:>8} {comp.name:<18} {comp.lump_avg:>11.4f} "
                      f"{comp.dca_avg:>10.4f} {comp.lump_return*100:>+10.2f}% "
                      f"{comp.dca_return*100:>+9.2f}% {comp.winner:>10}")

    text = pl.render_plan_markdown(plan, comps, cfg)
    path = write_report(cfg, "plan", text)
    print()
    print(f"计划已写入 {path}")
    return 0


# ---------------------------------------------------------------- flow

_STATUS_MARK = {"ok": "ok  ", "warn": "warn", "skip": "skip", "fail": "FAIL"}


def cmd_flow(cfg: Config, args) -> int:
    from ec import pipeline
    from ec.todos import TodoStore

    hr(f"工作流 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.demo:
        print("⚠ DEMO 模式：仅放宽了成本类闸门以便演示全链路。")
        print("  熔断、交易频次、仓位上限、标的范围这些与资金无关的闸门一条都没动。")
        for r in ("费用占比 0.6% → 5%", "风险成本比 3 倍 → 0.5 倍", "打平所需胜率 55% → 95%"):
            print(f"    · 放宽 {r}")
        print("  这个结果不是真实结论，不能用来判断策略好坏。")
    print()

    def say(msg):
        print(msg)

    res = pipeline.run_flow(
        cfg, code=args.code, limit=args.limit,
        use_ai=not args.no_ai, ai_dry_run=args.dry_run,
        ignore_signal=args.ignore_signal, max_todos=args.max_todos,
        demo=args.demo, verbose=say,
    )

    print()
    total = len(res.stages)
    for s in res.stages:
        mark = _STATUS_MARK.get(s.status, s.status)
        print(f"[{s.no}/{total}] {s.name:<12} {mark:<5} {s.detail}")

    print()
    print("─" * 68)
    print(f"  {res.headline}")
    print("─" * 68)

    if res.todos:
        print()
        print("本次产生的待办：")
        for t in res.todos:
            flag = "🔴 卖出" if t.kind == "卖出" else "🟢 买入"
            print(f"  {t.id}  {flag}  {t.code} {t.name}  "
                  f"{t.shares} 份  {t.notional:.2f} 元  → {t.state}")
            for tr in t.history:
                print(f"        {tr.frm} → {tr.to}  ({tr.actor}) {tr.reason[:60]}")

        store = TodoStore(cfg)
        print()
        print("未完成待办队列（卖出永远排在买入前面）：")
        for t in store.open_todos():
            print(f"  [{t.state}] {t.id} {'卖出' if t.is_sell else '买入'} "
                  f"{t.code} {t.name}")
        print()
        print("下一步可以：")
        first = res.todos[0]
        print(f"  python run.py todos                              # 看队列")
        if first.state == "待确认":
            print(f"  python run.py todos {first.id} --to 待执行        # 确认要执行")
            print(f"  python run.py todos {first.id} --abandon \"原因\"   # 放弃（必须填原因）")
    return 0


def cmd_todos(cfg: Config, args) -> int:
    from ec.todos import TodoStore, TodoError, TERMINAL

    store = TodoStore(cfg)

    # 有 --to / --abandon / --fill 就是推进某一条
    if args.id:
        try:
            if args.abandon:
                t = store.transition(args.id, "已放弃", actor="human",
                                     reason=args.abandon)
                print(f"{t.id} → {t.state}（原因：{t.abandon_reason}）")
            elif args.fill:
                price, shares = args.fill
                cur = store.get(args.id)
                if cur is None:
                    print(f"没有这条待办：{args.id}", file=sys.stderr)
                    return 1
                # 已经在下单后的状态就直接登记；还在「待执行」先补一步「待登记」
                if cur.state == "待执行":
                    store.transition(args.id, "待登记", actor="human",
                                     reason="已在券商 App 下单")
                t = store.record_fill(args.id, price=price, shares=shares,
                                      fee=args.fee or 0.0)
                print(f"{t.id} → {t.state}  成交 {shares} 份 @ {price}  "
                      f"费用 {args.fee or 0:.2f}")
                print(f"   注意：费用这里要是**券商实际扣的钱**，不是估算值。"
                      f"填错了后面的盈亏统计会偏。")
            elif args.to:
                t = store.transition(args.id, args.to, actor="human",
                                     reason=args.reason or "")
                print(f"{t.id} → {t.state}")
            else:
                t = store.get(args.id)
                if t is None:
                    print(f"没有这条待办：{args.id}")
                    return 1
                _print_todo(t)
        except TodoError as exc:
            print(f"状态机拒绝：{exc}", file=sys.stderr)
            return 1
        return 0

    # 列表
    hr("待办队列")
    open_t = store.open_todos()
    if open_t:
        print(f"未完成 {len(open_t)} 条（卖出优先）：")
        print(f"  {'ID':<14}{'类型':<6}{'状态':<8}{'标的':<22}{'份数':>6}{'金额':>10}")
        for t in open_t:
            print(f"  {t.id:<14}{t.kind:<6}{t.state:<8}"
                  f"{t.code + ' ' + t.name[:12]:<22}{t.shares:>6}{t.notional:>10.2f}")
    else:
        print("没有未完成的待办。")

    closed = store.closed_todos()
    if closed:
        print()
        print(f"已完成/已关闭 {len(closed)} 条（最近 10 条）：")
        for t in closed[:10]:
            tail = f"  原因：{t.abandon_reason}" if t.abandon_reason else ""
            print(f"  {t.id:<14}{t.state:<8}{t.code} {t.name[:14]}{tail}")

    st = store.stats()
    print()
    print(f"合计 {st['total']} 条：未完成 {st['open']} ｜ 已成交 {st['done']} ｜ "
          f"已否决 {st['rejected']} ｜ 已放弃 {st['abandoned']}")
    if st["abandon_reasons"]:
        print()
        print("放弃原因分布（最能反映你在什么情况下会违背系统）：")
        for reason, n in st["abandon_reasons"]:
            print(f"  {n:>3} 次  {reason}")

    if open_t:
        print()
        print(f"推进某一条：python run.py todos <ID> --to <状态>")
        print(f"            python run.py todos <ID> --abandon \"原因\"")
        print(f"            python run.py todos <ID> --fill 价格 份数 --fee 费用")
    return 0


def _print_todo(t) -> None:
    print(f"待办 {t.id}   {t.kind} {t.code} {t.name}")
    print(f"  状态      {t.state}")
    print(f"  价格/份数 {t.price:.3f} × {t.shares} = {t.notional:.2f} 元")
    if t.economics:
        e = t.economics
        print(f"  费用      {e.get('buy_fee', 0):.2f} 元（往返 "
              f"{e.get('round_trip_cost', 0):.2f}）")
        print(f"  盈亏平衡  {float(e.get('breakeven_pct', 0)) * 100:.3f}% "
              f"→ {e.get('breakeven_price', 0):.3f} 元")
        print(f"  止损/止盈 {e.get('stop')} / {e.get('target')}")
        print(f"  打平胜率  {float(e.get('required_win_rate', 0)) * 100:.0f}%")
    if t.data_health:
        h = t.data_health
        print(f"  数据来源  {h.get('source')}  数据时间 {h.get('as_of')}"
              + ("   ⚠ 已降级" if h.get("degraded") else ""))
    if t.signal:
        print(f"  策略      MA{'' if not t.signal.get('ma_fast') else ''}"
              f"{t.signal.get('ma_fast')} / MA{t.signal.get('ma_slow')}  "
              f"ATR%={float(t.signal.get('atr_pct') or 0) * 100:.2f}%  "
              f"动量={float(t.signal.get('mom') or 0) * 100:+.2f}%  "
              f"分位={float(t.signal.get('pct_rank') or 0) * 100:.0f}%")
    if t.risk:
        print("  风控闸门：")
        for c in t.risk.get("checks", []):
            mark = {"ok": "通过", "warn": "警告", "block": "拦截"}.get(c.get("level"), "?")
            print(f"    [{mark}] {c.get('name')}: {c.get('detail')}")
    if t.ai and t.ai.get("results"):
        print("  AI 复核：")
        for r in t.ai["results"]:
            if r.get("status") != "ok":
                print(f"    {r.get('model_id')}: 未取得意见（{r.get('error', '')[:50]}）")
            else:
                print(f"    {r.get('model_id')}: {r.get('verdict')} "
                      f"把握{float(r.get('confidence') or 0) * 100:.0f}% "
                      f"| {r.get('top_risk', '')[:60]}")
    if t.history:
        print("  轨迹：")
        for tr in t.history:
            print(f"    {tr.at}  {tr.frm} → {tr.to}  ({tr.actor}) {tr.reason[:70]}")


# ---------------------------------------------------------------- find

def cmd_find(cfg: Config, args) -> int:
    """用本地小模型在同花顺代码表里按自然语言检索。"""
    from ec import ths_scout

    hr("同花顺检索（本地小模型 · 零成本 · 数据不出本机）")

    if args.renames:
        rows = ths_scout.resolve_rename(args.query)
        if not rows:
            print(f"历史名称表里没有匹配「{args.query}」的记录。")
            print("（同花顺只在 stockname_*_1.txt 里保留曾用名，不是所有改名都有记录）")
            return 0
        print(f"「{args.query}」在历史名称表里找到 {len(rows)} 条：")
        print(f"  {'代码':<10}{'曾用名':<16}{'现名':<22}{'类别'}")
        for r in rows:
            print(f"  {r['code']:<10}{r['old_name']:<16}"
                  f"{r['current_name'] or '（不在可交易表）':<22}{r['kind']}")
        return 0

    res = ths_scout.search(cfg, args.query, top_k=args.top,
                           use_llm=not args.no_llm)

    print(f"查询      {res.query}")
    print(f"本地表    {res.n_table} 只（同花顺 stockname，离线）")
    print(f"关键词    {res.keywords or '（无）'}"
          + (f"   类别限定 {res.kind_filter}" if res.kind_filter else ""))
    print(f"预筛      {res.n_prescreen} 个候选")
    print()

    if not res.hits:
        print("没有找到。")
        print(f"  {res.note}")
        return 0

    print(f"{'代码':<10}{'名称':<22}{'类别':<6}{'现价':>8}"
          f"{'相关度':>7}  {'依据':<6}说明")
    for h in res.hits:
        tag = "命中" if h.matched else "推断"
        price = f"{h.price:.3f}" if h.price else "—"
        # 价格来自实时行情才可信；来自本地表的是旧的，标出来
        src = "" if h.source == "实时" else "  ←本地表"
        print(f"{h.code:<10}{h.name:<22}{h.kind:<6}{price:>8}"
              f"{h.score:>7.2f}  {tag:<6}{h.why[:34]}{src}")

    print()
    print(f"  {res.note}")
    if any(not h.matched for h in res.hits):
        print("  「推断」= 名称里没有直接出现查询词，是模型按语义挑的，请自己判断。")
    print()
    print("下一步：")
    print(f"  python run.py ticket --code {res.hits[0].code[2:]}    # 对这只出工单")
    return 0


# ---------------------------------------------------------------- look

def cmd_look(cfg: Config, args) -> int:
    """截图 + 本地视觉模型，读同花顺界面。"""
    from ec import screen

    if args.windows:
        hr("有窗口的进程")
        for w in screen.list_windows():
            print(f"  {str(w.get('ProcessName', '')):<24} "
                  f"PID {w.get('Id', ''):<8} {str(w.get('MainWindowTitle', ''))[:60]}")
        return 0

    hr("同花顺界面读取（截屏 + 本地视觉模型）")

    model, why = screen.vision_available(cfg)
    print(f"视觉模型  {model or '（没有）'}"
          + ("" if model else f"  ← {why}"))
    if not model:
        return 1

    target = "screen" if args.screen else "window"
    try:
        cap = screen.capture(target=target, process=args.process)
    except screen.ScreenError as exc:
        print(f"截图失败：{exc}")
        return 1

    print(f"截图      {cap.width}x{cap.height}  {cap.bytes // 1024} KB  "
          f"{cap.captured_at}")
    if cap.window_title:
        print(f"窗口      「{cap.window_title}」")
    print()

    # --ask：对整张图问一个自由问题
    if args.ask:
        try:
            text = screen.ask_vision(cfg, cap.path, args.ask, num_predict=args.max_tokens)
        except screen.ScreenError as exc:
            print(f"视觉模型失败：{exc}")
            return 1
        print("── 模型回答 " + "─" * 48)
        print(text.strip())
        print("─" * 60)
        print(f"\n图片留着备查：{cap.path}")
        return 0

    # 默认：读自选股表
    try:
        items = screen.read_watchlist(cfg, cap)
    except screen.ScreenError as exc:
        print(f"视觉模型失败：{exc}")
        return 1

    if not items:
        print("没在截图里找到自选股表格。")
        print("可能原因：当前不是「自选股」页面、窗口被遮挡、或表格不在窗口可见区域内。")
        print(f"图片：{cap.path}（可以自己打开看看）")
        return 0

    items = screen.verify_with_api(items)
    print(f"{'代码':<10}{'名称':<20}{'接口名称':<24}{'接口价':>10}{'涨幅':>8}  核对")
    print("─" * 92)
    n_bad = 0
    for it in items:
        if it.parse_error:
            print(f"{it.raw_code:<10}{it.name[:18]:<20}⚠ {it.parse_error}")
            n_bad += 1
            continue
        mark = "OK" if (it.verified and not it.mismatch) else (
            "不一致" if it.mismatch else "未核对")
        if it.mismatch:
            n_bad += 1
        px = f"{it.api_price:.3f}" if it.verified else "—"
        pct = f"{it.api_pct:+.2f}%" if it.verified else "—"
        print(f"{it.code:<10}{it.name[:18]:<20}{(it.api_name or '—')[:22]:<24}"
              f"{px:>10}{pct:>8}  {mark}")

    print()
    print(f"共 {len(items)} 行，{n_bad} 行需要注意。")
    if any(it.mismatch for it in items):
        print("差异明细：")
        for it in items:
            if it.mismatch:
                print(f"  {it.code}：{it.mismatch}")
    print()
    print("重要：截图读出的数字**不用于交易决策**。上面的「接口价/涨幅」才是事实。")
    print("      截图的用途是「知道有哪些标的」，不是「拿到准确报价」。")
    return 0


# ---------------------------------------------------------------- dip

def cmd_dip(cfg: Config, args) -> int:
    """找「跌下来但趋势没坏」的标的，并把手续费现实摆出来。"""
    from ec import cheap, oversold
    from ec.dataloader import BarStore

    hr("超跌筛选（回调 ≠ 破位）")
    print(f"上限 ¥{args.max_price} ｜ 成交额 ≥ {args.min_amount:,.0f} 万 ｜ "
          f"最低佣金 ¥{cfg.fees.min_commission:.0f}")
    print()

    quotes = cheap.fetch_all_quotes(cfg, force=args.refresh,
                                    verbose=lambda m: print(m))
    kinds = None if args.kind == "all" else (
        ("ETF", "LOF", "基金") if args.kind == "etf" else ("股票",))
    cands = cheap.screen(cfg, max_price=args.max_price,
                         min_amount_wan=args.min_amount,
                         kinds=kinds, quotes=quotes, verbose=lambda m: print(m))

    if not cands:
        print("没有符合价格和流动性条件的标的。")
        return 0

    store = BarStore(cfg)
    picks = []
    for n, s in enumerate(cands, 1):
        try:
            bars = store.get(s.code, 400)
        except Exception:
            continue
        p = oversold.evaluate(cfg, s.code, s.name, s.price, s.lot_cost, bars,
                              s.amount_wan)
        if p:
            picks.append(p)
        if n % 150 == 0:
            print(f"  …{n}/{len(cands)}")

    print()
    print("状态分布（这是「稳不稳」的核心）：")
    total = max(1, len(picks))
    for k, v in sorted(oversold.state_summary(picks).items(), key=lambda x: -x[1]):
        print(f"  {k:<6} {v:>4} 只  ({v/total*100:.0f}%)")

    good = oversold.rank(picks, only_tradeable=True)
    print()
    if not good:
        print("没有符合「回调」条件的标的。")
        print("这很正常 —— 大部分时间市场就是不符合条件，空仓也是仓位。")
        return 0

    print(f"可考虑（回调 · 趋势未坏 · 不在高位）：{len(good)} 只")
    print(f"{'代码':<10}{'名称':<20}{'现价':>7}{'1手':>8}{'距高点':>9}"
          f"{'分位':>6}{'RSI':>5}{'距MA60':>8}{'分':>6}{'回本需涨':>9}")
    print("─" * 100)
    for p in good[:args.top]:
        rt = cfg.fees.round_trip(p.lot_cost)
        print(f"{p.code:<10}{p.name[:18]:<20}{p.price:>7.3f}{p.lot_cost:>8.0f}"
              f"{p.dd_250*100:>8.1f}%{p.pct_rank*100:>5.0f}%{p.rsi:>5.0f}"
              f"{p.dev_ma60*100:>+7.1f}%{p.score:>6.1f}"
              f"{rt/p.lot_cost*100:>8.2f}%")

    # 手续费现实 —— 这一段比上面的选股结果重要
    print()
    print("─" * 100)
    worst = max(cfg.fees.round_trip(p.lot_cost) / p.lot_cost for p in good)
    best = min(cfg.fees.round_trip(p.lot_cost) / p.lot_cost for p in good)
    print(f"手续费现实：这 {len(good)} 只的往返成本在 {best*100:.2f}% ~ {worst*100:.2f}% 之间。")
    print(f"  A 股 ETF 日均波动约 1~2%，要涨这么多才回本。")
    if best > 0.02:
        print(f"  根因：最低佣金 {cfg.fees.min_commission:.0f} 元卡在小额单上。")
        print(f"  换成免五券商，这一项会降到 0.1% 级别 —— 这是唯一真正的解药。")

    rep = cheap.fee_report(cfg, cfg.capital, min(args.positions, len(good)),
                           good[0].lot_cost, args.daily_target)
    print()
    print(f"目标「每天 ¥{args.daily_target:.0f}」（每月 ¥{rep.target_monthly:.0f}）"
          f"在本金 ¥{cfg.capital:,.0f} 下的现实：")
    print(f"  月需涨幅 {rep.monthly_return_needed*100:.2f}%  "
          f"→ 年化 {rep.annual_return_needed*100:.0f}%")
    print(f"  {rep.verdict}")
    for n in rep.notes:
        print(f"  · {n}")
    return 0


# ---------------------------------------------------------------- budget

def cmd_budget(cfg: Config, args) -> int:
    """API 花费台账与预算。"""
    from ec import budget as bud

    if args.reset:
        import os
        g = bud.BudgetGuard(cfg)
        if os.path.exists(g.path):
            backup = g.path + ".bak"
            os.replace(g.path, backup)
            print(f"台账已归档到 {backup}（没有删除，需要可恢复）")
        else:
            print("台账是空的。")
        return 0

    g = bud.BudgetGuard(cfg)
    r = g.report()
    bc = cfg.ai.budget

    hr("API 花费")
    if not bc.enabled:
        print("⚠ 预算管控已关闭（ai.budget.enabled = false）")
        print()
    print(f"今日    ¥{r['today']:.2f} / ¥{r['daily_limit']:.2f}"
          f"   （已用 {r['today_pct']*100:.0f}%）")
    print(f"本月    ¥{r['month']:.2f} / ¥{r['monthly_limit']:.2f}"
          f"   （已用 {r['month_pct']*100:.0f}%）")
    print(f"累计    ¥{r['total']:.2f}")
    print()
    print(f"规则：复核花费不得超过这笔交易的 "
          f"{r['cost_ratio']*100:.2f}%")

    if r["by_model"]:
        print()
        print("本月各模型：")
        for m, d in sorted(r["by_model"].items(),
                           key=lambda x: -x[1]["cost_cny"]):
            print(f"  {m:<10} {d['calls']:>3} 次   ¥{_money(d['cost_cny'])}")
    else:
        print()
        print("本月还没有任何花费。")

    print()
    print("各模型单次成本与「值得叫它」的最小交易金额：")
    for m in cfg.ai.reviewers:
        cost = bud.model_cost_cny(cfg, m.id)
        need = bud.min_notional_for(cfg, m.id)
        print(f"  {m.id:<10} ¥{_money(cost)}/次   "
              + (f"交易金额需 ≥ ¥{need:.0f}" if need > 1 else "任意金额"))
        if cost > 0:
            d = g.can_afford(cost, cfg.capital * 0.95)
            if not d.allowed:
                print(f"             当前本金 ¥{cfg.capital:.0f} 下：⏭ 会跳过")

    if r["days_with_spend"]:
        print()
        print(f"按目前节奏：每个有花费的日子平均 ¥{r['avg_per_spend_day']:.2f}，"
              f"月度预计 ¥{r['projected_month']:.2f}")
    return 0


# ---------------------------------------------------------------- longplan

def cmd_longplan(cfg: Config, args) -> int:
    """长期定投计划：用整手约束和真实手续费重算一遍。"""
    from ec import longplan, ths
    from ec.quotes import fetch_quotes

    hr("长期定投计划 · 真实约束版")

    if args.schedule:
        sched = [float(x) for x in args.schedule.split(",")]
    else:
        # 默认用「银行股分红定投」那份计划的节奏
        sched = [100, 300, 400, 500, 500, 700, 900, 1000, 1000, 1000, 1000, 1000, 1000]
        print("未指定 --schedule，使用计划原版节奏：")
    print("月投节奏:", " → ".join(f"{x:.0f}" for x in sched))
    print(f"共 {len(sched)} 年，累计投入 ¥{sum(sched)*12:,.0f}")
    print(f"股息率假设 {args.yield_pct:.1f}% ｜ 股价不变 ｜ 分红全部再投入")
    print()

    if args.codes:
        targets = []
        try:
            uni = ths.tradable_universe()
        except Exception:
            uni = {}
        q = fetch_quotes(args.codes)
        for c in args.codes:
            from ec.quotes import normalize_code
            code = normalize_code(c)
            x = q.get(code)
            if x and x.price > 0:
                targets.append((code, x.name, x.price))
    else:
        q = cheap_all_quotes(cfg) if args.refresh else None
        targets = args_default_targets(cfg, q)

    if not targets:
        print("没有可用的标的。用 --codes 指定，例如 --codes 601288,601398")
        return 1

    print(f"{'标的':<24}{'理想复利':>12}{'实际(¥5)':>12}{'差距':>9}"
          f"{'手续费':>10}{'闲置现金':>10}")
    print("─" * 84)
    rows = []
    for code, name, price in targets:
        r = longplan.compare(cfg, code=code, name=name, price=price,
                             monthly_by_year=sched, start_age=args.start_age,
                             dividend_yield=args.yield_pct / 100)
        real = r["real"]
        rows.append((code, name, price, r, real))
        print(f"{code} {name[:12]:<11}{r['ideal']:>12,.0f}{real.final_value:>12,.0f}"
              f"{real.gap_pct*100:>8.1f}%{real.total_fees:>10,.0f}"
              f"{real.final_cash_idle:>10,.0f}")

    print()
    print("─" * 84)
    print("前 5 年（大学阶段，钱最少、影响最大）：")
    print(f"{'标的':<24}{'5年投入':>10}{'5年市值':>10}{'5年手续费':>11}{'第1年':>10}")
    print("─" * 70)
    for code, name, price, _r, _real in rows:
        five = longplan.simulate(cfg, code=code, name=name, price=price,
                                 monthly_by_year=sched[:5],
                                 start_age=args.start_age,
                                 dividend_yield=args.yield_pct / 100)
        first = five.rows[0] if five.rows else None
        print(f"{code} {name[:12]:<11}{five.total_cash:>10,.0f}"
              f"{five.final_value:>10,.0f}{five.total_fees:>11,.0f}"
              f"{(str(first.lots)+' 手') if first and first.lots else '买不了':>10}")

    print()
    print("说明：")
    print("  · 「理想复利」是多数计划表的算法——假设每一块钱都能立刻复利。")
    print("  · 「实际」按 100 股整手买入、每次委托收一次佣金，算上闲置现金。")
    print("  · 差距为正说明整手约束让小额资金没法立刻变成股票，反而少付了佣金；")
    print("    差距为负说明手续费吃掉了收益。")
    return 0


def cheap_all_quotes(cfg: Config):
    from ec.cheap import fetch_all_quotes
    return fetch_all_quotes(cfg)


def args_default_targets(cfg: Config, _q=None):
    """没指定 --codes 时，用计划里推荐的那几只用实时价。"""
    from ec.quotes import fetch_quotes
    want = [("sh601288", "农业银行"), ("sh601398", "工商银行"),
            ("sh601988", "中国银行"), ("sh600036", "招商银行"),
            ("sh512800", "银行ETF华宝")]
    q = fetch_quotes([c for c, _ in want])
    out = []
    for code, fallback in want:
        x = q.get(code)
        if x and x.price > 0:
            out.append((code, x.name, x.price))
    return out


# ---------------------------------------------------------------- train

def cmd_train(cfg: Config, args) -> int:
    """训练模式：完整跑一遍流水线，只记录不落待办。"""
    from ec import pipeline, train

    hr("训练模式（不产生待办）")
    if args.demo:
        print("⚠ demo：会放宽成本类闸门，用来演练完整链路。结果不是真实结论。")
    else:
        print("完整跑一遍流水线，闸门一条不放宽。")
    print("结果只写 data/train_log.jsonl，真实待办队列一个字节都不动。")
    print()

    import time
    t0 = time.time()
    try:
        res = pipeline.run_flow(
            cfg, code=args.code, limit=args.limit,
            use_ai=not args.no_ai, max_todos=args.max_todos,
            demo=args.demo, save_todos=False,
            ignore_signal=args.ignore_signal,
            verbose=(lambda m: print(m)) if args.verbose else None,
        )
    except Exception as exc:
        print(f"✗ 流水线抛异常：{type(exc).__name__}: {exc}")
        print("  这也是有价值的记录 —— 把上面这行发给我。")
        return 1
    elapsed = time.time() - t0

    if args.demo and res.demo_relaxed:
        print()
        print("⚠ demo 模式：这几条闸门被放宽了，结果不是真实结论")
        for x in res.demo_relaxed:
            print(f"    · {x}")

    print()
    for s in res.stages:
        mark = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "·"}.get(s.status, "?")
        print(f"  {mark} [{s.no}/7] {s.name:<10} {s.status:<5} {s.detail}")

    print()
    print(f"  结论：{res.headline}")
    print(f"  耗时：{elapsed:.1f}s")

    rec = train.record(cfg, res, elapsed)
    print()
    if rec["gate_blocks"]:
        print("  本次拦截的闸门：")
        for k, v in rec["gate_blocks"].items():
            print(f"    · {k}  ×{v}")
    else:
        print("  本次没有闸门拦截。")
    print()
    print(f"  已记入 data/train_log.jsonl（第 {len(train.entries(cfg))} 次）")
    print("  python run.py train-report    # 看累积统计")
    return 0


def cmd_train_report(cfg: Config, args) -> int:
    """训练累积统计 —— 这才是训练模式真正的产出。"""
    from ec import train

    r = train.report(cfg)
    hr("训练累积统计")

    if not r.get("runs"):
        print("还没有训练记录。先跑：python run.py train")
        return 0

    print(f"已训练 {r['days']} 天，共 {r['runs']} 次"
          f"（{r['first_day']} ~ {r['last_day']}）")
    print(f"平均耗时 {r['elapsed_avg']}s，最长 {r['elapsed_max']}s")
    print()

    print("数据源：")
    for s, c in sorted(r["sources"].items(), key=lambda x: -x[1]):
        print(f"  {s:<12} {c} 次")
    if r["degraded"]:
        print(f"  ⚠ 降级 {r['degraded']} 次")
    if r["stale"]:
        print(f"  ⚠ 数据陈旧 {r['stale']} 次")
    print()

    print(f"候选：{r['runs_with_todo']}/{r['runs']} 次跑出了待办"
          f"（共 {r['total_todos']} 条，全部未落盘）")
    print()

    if r["gate_blocks"]:
        print("闸门拦截排行（跨全部训练）：")
        total = sum(r["gate_blocks"].values())
        for k, v in r["gate_blocks"].items():
            pct = v / total * 100 if total else 0
            bar = "█" * int(pct / 4)
            print(f"  {k:<26} {v:>4} 次 {pct:>5.1f}% {bar}")
        print()
        top = next(iter(r["gate_blocks"]))
        print(f"  最常拦的是「{top}」——这就是路 B 现在跑不起来的原因。")
        print()

    print("各阶段健康度：")
    for name, d in r["stage_stat"].items():
        parts = " ".join(f"{k}:{v}" for k, v in sorted(d.items()))
        print(f"  {name:<12} {parts}")
    print()

    if r["errors"]:
        print("✗ 异常（阶段失败，最近的在前）：")
        for e, d in r["errors"].items():
            print(f"  {d}  {e[:110]}")
        print()
        print("  ↑ 这些才是真正的问题：真实数据下才会暴露的 bug。")
    else:
        print("✓ 没有阶段失败过。")

    if r["warnings"]:
        print()
        print("· 常态告警（不是 bug，是系统在正常工作）：")
        for w, d in list(r["warnings"].items())[:5]:
            print(f"  {d}  {w[:110]}")
    return 0


# ---------------------------------------------------------------- probe

def cmd_probe(cfg: Config, args) -> int:
    """实测费率探针：把真实扣费输进来，看它改写了哪些门槛。

    为什么要这个命令
    ----------------
    ¥5 最低佣金这件事，问了几天都没答案，而它决定了整个路 B 能不能跑。
    花 ¥10 做一次真实买卖，能换来确定的数字 —— 这个命令就是把
    「一次交易」换算成「系统里所有门槛的新数值」。
    """
    price = args.price
    shares = args.shares
    notional = price * shares
    buy_fee = args.buy_fee
    sell_fee = args.sell_fee

    hr("实测费率探针")

    print(f"你这一笔：{args.code or '标的'}  {shares} 份 × ¥{price:.3f} = ¥{notional:.2f}")
    print(f"  买入佣金 ¥{buy_fee:.2f}   卖出佣金 ¥{sell_fee:.2f}")
    print()

    # ---- 实测成本 ----
    rt_actual = buy_fee + sell_fee
    rt_pct = rt_actual / notional if notional else 0
    assumed_rt = cfg.fees.round_trip(notional)
    assumed_pct = assumed_rt / notional if notional else 0

    # 只比佣金部分。配置里的 round_trip 还含滑点（不会出现在佣金栏），
    # 直接比总额会显示一个假的「偏差」。
    commission_expect = max(notional * cfg.fees.commission_rate,
                            cfg.fees.min_commission)

    print("─" * 72)
    print("① 这一笔的真实成本")
    print(f"  实测往返（佣金）    ¥{rt_actual:.2f}   = {rt_pct*100:.2f}%")
    print(f"  配置假设往返        ¥{assumed_rt:.2f}   = {assumed_pct*100:.2f}%")
    print(f"    其中佣金应为      ¥{commission_expect:.4f}（单边）")
    print(f"    另外含滑点        ¥{assumed_rt - commission_expect*2:.4f}"
          f"（{cfg.fees.slippage_bps} bps × 2，佣金栏看不到）")
    comm_diff = buy_fee - commission_expect
    if abs(comm_diff) < 0.01:
        print(f"  → ✓ 佣金对得上（差 ¥{comm_diff:+.4f}）")
    else:
        print(f"  → ⚠ 佣金偏差 ¥{comm_diff:+.4f}，配置需要修正")

    # ---- 判定到底免不免五 ----
    print()
    print("─" * 72)
    print("② 判定：券商到底收不收最低佣金")
    rate_only = notional * cfg.fees.commission_rate
    if buy_fee >= cfg.fees.min_commission - 0.5:
        verdict = "不免五"
        detail = (f"买入佣金 ¥{buy_fee:.2f} ≈ 最低佣金 ¥{cfg.fees.min_commission:.0f}。"
                  f"配置是对的，一个字都不用改。")
    elif buy_fee <= rate_only * 3 + 0.05:
        verdict = "免五"
        detail = (f"买入佣金 ¥{buy_fee:.2f} ≈ 按费率算的 ¥{rate_only:.4f}，"
                  f"**没有触发最低佣金**。")
    else:
        verdict = "有中间档"
        detail = (f"买入佣金 ¥{buy_fee:.2f}，既不是 ¥{cfg.fees.min_commission:.0f}，"
                  f"也不是纯按费率（¥{rate_only:.4f}）。"
                  f"可能有个更低的固定下限，需要再测一笔不同金额的来定位。")

    print(f"  结论：{verdict}")
    print(f"  {detail}")
    print(f"  （按费率算应该是 ¥{rate_only:.4f}）")

    # ---- 免五前后门槛对照 ----
    print()
    print("─" * 72)
    print("③ 如果换成免五券商，门槛会变成什么样")

    gate = cfg.risk.max_fee_ratio_per_trade

    def need_notional(min_comm: float) -> float:
        """费用占比 ≤ gate 所需的最小单笔金额。"""
        if gate <= 0:
            return 0.0
        rate = cfg.fees.commission_rate * 2 + cfg.fees.slippage_bps * 2 / 10000
        # round_trip(n)/n = max(rate*n, 2*min_comm)/n + slip
        # 当 n 很小时佣金是 2*min_comm，需要 2*min_comm/n ≤ gate
        by_floor = (2 * min_comm) / gate if min_comm > 0 else 0.0
        by_rate = 0.0 if rate <= gate else float("inf")
        return max(by_floor, by_rate)

    n_now = need_notional(cfg.fees.min_commission)
    n_free = need_notional(0.0)

    def row(label: str, a: str, b: str) -> None:
        print(f"  {label:<28} {a:>16} {b:>16}")

    print(f"  {'项目':<28} {'现在（¥5）':>16} {'免五':>16}")
    print("  " + "-" * 64)
    row("你这一笔的往返成本",
        f"¥{rt_actual:.2f} ({rt_pct*100:.2f}%)",
        f"¥{notional*(cfg.fees.commission_rate*2 + cfg.fees.slippage_bps*2/10000):.3f} "
        f"({(cfg.fees.commission_rate*2 + cfg.fees.slippage_bps*2/10000)*100:.3f}%)")
    row(f"费用闸门放行所需单笔金额",
        f"¥{n_now:,.0f}" if n_now else "任意",
        "任意金额")
    # 单边佣金 = 金额 × 费率 达到最低佣金时的金额。
    # 这里容易算错：不是 2×min/rate，是 min/rate —— 单边就够阈值了。
    row("最低佣金不再约束的临界额",
        f"¥{cfg.fees.min_commission/cfg.fees.commission_rate:,.0f}",
        "不适用")
    row("1 手 ¥100 的标的往返成本",
        f"{cfg.fees.round_trip(100)/100*100:.2f}%", "0.05%")

    # ---- 这一笔在系统里的处境 ----
    print()
    print("─" * 72)
    print("④ 你这一笔在现有闸门下会怎样")
    if rt_pct <= gate:
        print(f"  ✓ 往返 {rt_pct*100:.2f}% ≤ 闸门 {gate*100:.1f}%，可以通过")
    else:
        print(f"  ✗ 往返 {rt_pct*100:.2f}% > 闸门 {gate*100:.1f}%，会被风控拦下")
        print(f"    要过闸门，单笔金额至少 ¥{n_now:,.0f}")
        print(f"    你现在这一笔 ¥{notional:.2f}，差 {n_now/notional:.0f} 倍")
    if n_free == 0:
        print(f"  → 免五之后，任意金额都能过闸门（这才是路 B 真正的解冻条件）")

    # ---- 写入配置 ----
    print()
    print("─" * 72)
    if verdict == "免五" and not args.apply:
        print("配置里 min_commission 还是 "
              f"{cfg.fees.min_commission:.0f}，会拦掉所有小额单。")
        print("要改成 0 就加 --apply（会先备份 config.json）")
    else:
        print(f"配置无需改动：min_commission = {cfg.fees.min_commission:.0f}")

    if args.apply:
        if verdict != "免五":
            print("✗ 实测不是免五，拒绝改配置。")
            return 1
        path = args.config or "config.json"
        import os
        import re
        if not os.path.exists(path):
            print(f"✗ 找不到 {path}")
            return 1
        raw = open(path, encoding="utf-8").read()
        new_raw, n = re.subn(r'("min_commission"\s*:\s*)[0-9.]+',
                             r'\g<1>0', raw, count=1)
        if n == 0:
            print("✗ 没在配置里找到 min_commission，手改吧。")
            return 1
        backup = path + ".bak"
        open(backup, "w", encoding="utf-8").write(raw)
        open(path, "w", encoding="utf-8").write(new_raw)
        print(f"✓ 已把 min_commission 改为 0（原文件备份到 {backup}）")
        print("  再跑一次 python run.py probe 确认门槛都变了。")
    return 0


# ---------------------------------------------------------------- ask

def cmd_ask(cfg: Config, args) -> int:
    """用本地小模型 + 实时行情工具回答自然语言问题。"""
    from ec import tools

    hr("问行情（本地模型 + 实时工具）")
    print(f"问题：{args.question}")
    print(f"模型：{args.model}   数据源：腾讯实时快照")
    print()

    r = tools.ask(cfg, args.question, model=args.model,
                  trace=(lambda m: print(m)) if args.verbose else None)

    print()
    if r.get("error"):
        print(f"✗ {r['error']}")
        return 1

    print("答案：")
    for line in (r["answer"] or "（空）").splitlines():
        print("  " + line)

    print()
    print(f"耗时 {r['elapsed']}s，{r['rounds']} 轮，调用工具 {len(r['calls'])} 次")
    for c in r["calls"]:
        rows = c.get("result") or []
        if isinstance(rows, list):
            for row in rows:
                if row.get("error"):
                    print(f"  ✗ {row.get('query')}: {row['error']}")
                else:
                    print(f"  · {row['code']} {row['name']} "
                          f"{row['price']} {row['pct']:+.2f}% "
                          f"成交 {row['amount_wan']:,}万  {row['data_time']} "
                          f"({row.get('data_age_sec')}秒前)")
    return 0


# ---------------------------------------------------------------- ths

def cmd_ths(cfg: Config, args) -> int:
    """把同花顺拉起来并摆成小窗，之后随时可读。"""
    import subprocess

    hr("同花顺小窗")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "tools", "ths-small-window.ps1")
    if not os.path.exists(script):
        print(f"✗ 找不到 {script}")
        return 1

    # 先报当前状态。用 screen.list_windows() 而不是自己拼 PowerShell ——
    # 自己拼的那版因为引号嵌套 + 编码问题，明明同花顺在跑却报「没在跑」。
    from ec import screen

    procs = []
    try:
        for w in screen.list_windows():
            if str(w.get("ProcessName", "")).startswith("hexin"):
                procs.append(w)
    except Exception:
        pass

    if procs:
        print("同花顺在跑：")
        for p in procs:
            print(f"  PID {p.get('Id')}  「{p.get('MainWindowTitle')}」")
    else:
        print("同花顺没在跑。")

    if args.status:
        if not procs:
            print("\n要启动就运行： python run.py ths")
        return 0

    print()
    print("摆成小窗中（不会抢焦点）…")
    argv = [_pwsh_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", script, "-Corner", args.corner,
            "-Width", str(args.width), "-Height", str(args.height)]
    if procs:
        argv.append("-NoStart")
    res = subprocess.run(argv, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=120)
    for line in (res.stdout or "").splitlines():
        if line.strip():
            print("  " + line.strip())

    if args.capture:
        print()
        from ec import screen
        try:
            cap = screen.capture(target="window", process="hexin",
                                 out_path=os.path.join(cfg.out_dir, "ths-window.png"))
            print(f"✓ 试抓一张：{cap.width}x{cap.height}  "
                  f"{cap.bytes//1024} KB  「{cap.window_title}」")
            print(f"  {cap.path}")
        except Exception as exc:
            print(f"✗ 试抓失败：{exc}")
    return 0


def _pwsh_exe() -> str:
    import shutil
    for cand in ("pwsh", "powershell"):
        p = shutil.which(cand)
        if p:
            return p
    return "powershell.exe"


# ---------------------------------------------------------------- serve / pet / up

def cmd_serve(cfg: Config, args) -> int:
    """本地 Web 工作台。"""
    from ec import webapp

    host = args.host
    token = args.token or ""
    lan_url = ""

    if host not in ("127.0.0.1", "localhost"):
        # 绑到局域网：没有口令就自动生成一个，别让同 WiFi 的人裸看。
        if not token:
            import secrets
            token = secrets.token_hex(4)
        import socket as _socket
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            lan_url = f"http://{ip}:{args.port}/?token={token}"
        except Exception:
            lan_url = f"http://<电脑局域网IP>:{args.port}/?token={token}"

    print(f"工作台：http://127.0.0.1:{args.port}")
    if lan_url:
        print(f"手机（同一 WiFi）：{lan_url}")
    print("Ctrl+C 退出。")
    try:
        webapp.serve(cfg, host=host, port=args.port, token=token)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


def cmd_pet(cfg: Config, args) -> int:
    """瑶瑶桌宠。"""
    from ec import pet

    print("瑶瑶来了。右键：状态 / 问行情 / 退出；双击问她行情。")
    try:
        pet.run(cfg)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_workbench(cfg: Config, args) -> int:
    """同花顺风格的桌面工作台（原生窗口软件）。启动时自动拉起同花顺。"""
    from ec import workbench

    # 工作台每 60 秒要读同花顺窗口，所以启动工作台 = 同时拉起同花顺。
    # 异步启动（不等同花顺加载完），工作台先出来，同花顺随后摆成小窗。
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "tools", "ths-small-window.ps1")
    if os.path.exists(script):
        try:
            import subprocess
            subprocess.Popen(
                [_pwsh_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", script, "-Corner", "bottomright",
                 "-Width", "1350", "-Height", "620"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            print("同花顺拉起中（会摆成右下角小窗）…")
        except Exception as exc:
            print(f"同花顺拉起失败（{exc}），工作台仍会启动，只是自选股读不到。")

    print("工作台启动。自选股每 60 秒自动读同花顺窗口。")
    try:
        workbench.run(cfg)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_pet3d(cfg: Config, args) -> int:
    """瑶瑶 3D 桌宠（官方 PMX 建模 + OpenGL）。"""
    import subprocess

    venv_py = os.path.join(r"C:\Users\user\dev\项目\mmd-assets",
                           ".venv", "Scripts", "python.exe")
    if not os.path.exists(venv_py):
        print("✗ 找不到素材链 venv 的 Python（需要 PySide6 + numpy）：")
        print(f"  {venv_py}")
        print("  用 2D 版：python run.py pet")
        return 1
    print("瑶瑶 3D 桌宠启动（官方 PMX 建模 · OpenGL · 透明窗口）。")
    print("右键：退出。拖动：移动位置。")
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run([venv_py, os.path.join(here, "ec", "pet3d.py")],
                       cwd=here)
    return r.returncode


def cmd_up(cfg: Config, args) -> int:
    """一条命令拉起整套工作台：同花顺小窗 + 模型常驻 + 桌宠 + Web 服务。"""
    import subprocess
    import sys

    from ec import warm

    hr("启动工作台")

    # ① 同花顺小窗
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "tools", "ths-small-window.ps1")
    print("① 同花顺小窗 …")
    if os.path.exists(script):
        try:
            r = subprocess.run(
                [_pwsh_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", script, "-Corner", args.corner,
                 "-Width", "1350", "-Height", "620"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120)
            for line in (r.stdout or "").splitlines():
                if "摆到" in line or "窗口标题" in line:
                    print("   " + line.strip())
        except Exception as exc:
            print(f"   跳过（{exc}），不影响其余部分")
    else:
        print("   脚本缺失，跳过")

    # ② 模型常驻
    print("② 模型常驻 …")
    try:
        warm.pin(["qwen3.5:4b"], verbose=lambda m: print("   " + m))
    except Exception as exc:
        print(f"   跳过（{exc}）")

    # ③ 桌宠（后台进程）
    if not args.no_pet:
        print("③ 瑶瑶桌宠（后台）…")
        try:
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "pet"],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            print("   已启动")
        except Exception as exc:
            print(f"   跳过（{exc}）")

    # ③b Web 服务（可选，手机访问用，后台进程）
    if getattr(args, "web", False):
        print(f"③b Web 服务（后台）→ http://127.0.0.1:{args.port}")
        try:
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "serve",
                 "--port", str(args.port)],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            print(f"   跳过（{exc}）")

    # ④ 桌面工作台（阻塞到最后）。手机要看再加 --web 起网页服务。
    print("④ 桌面工作台…")
    print()
    try:
        from ec import workbench
        workbench.run(cfg)
    except KeyboardInterrupt:
        print("\n已停止。桌宠如果想关，右键瑶瑶 → 退出。")
    return 0


# ---------------------------------------------------------------- dock

def cmd_dock(cfg: Config, args) -> int:
    """贴着同花顺的常驻状态面板。"""
    from ec import dock as _dock

    if args.check:
        hit = _dock.find_ths()
        ths = _dock.rect_of(hit[0]) if hit else None
        sw, sh = _dock.screen_size()
        print(f"屏幕       {sw}x{sh}（逻辑像素）")
        if ths:
            x, y, w, h = ths
            print(f"同花顺     hwnd={hit[0]}  ({x},{y})  {w}x{h}")
            print(f"            「{hit[1]}」")
        else:
            print("同花顺     没在跑")
        geo = _dock.pick_geometry(ths, args.width)
        print(f"面板位置   {geo[4]}：({geo[0]},{geo[1]})  {geo[2]}x{geo[3]}")
        return 0

    print("面板启动。关掉面板点右上角 ✕。")
    print(f"  宽度 {args.width}px，每 {args.interval}s 刷新一次")
    print("  「刷新」按钮在右下角。拖动标题栏可以自己摆位置。")
    try:
        _dock.run(cfg, width=args.width, interval=args.interval)
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------- warm

def cmd_warm(cfg: Config, args) -> int:
    """把本地模型钉在内存里，别闲着就卸载。"""
    from ec import warm

    hr("本地模型常驻")

    if args.unload:
        print("释放内存中的模型…")
        r = warm.unpin(verbose=print)
        for k, v in (r.get("mem") or {}).items():
            print(f"  {k}: {v}")
        return 0 if r.get("ok") else 1

    mem = warm.memory()
    print(f"内存    可用 {mem.get('avail_gb')} GB / 总 {mem.get('total_gb')} GB"
          f"（占用 {mem.get('percent')}%）")

    cur = warm.loaded()
    if cur:
        print("当前加载：")
        for m in cur:
            tag = "已钉住" if m["pinned"] else "会过期"
            print(f"  {m['name']:<18} {m['size_gb']:>5.2f} GB  {tag}"
                  f"  expires {m['expires_at']}")
    else:
        print("当前加载：无（都在磁盘上，调用要等 4~5 秒）")
    print()

    if args.list:
        print("可用档位：")
        for k, ms in warm.PRESETS.items():
            gb = sum(warm.SIZE_GB.get(m, 5.0) for m in ms)
            print(f"  --set {k:<8} {', '.join(ms):<20} 约 {gb:.1f} GB")
        return 0

    models = args.models or warm.PRESETS.get(args.set or "chat", ["qwen2.5:7b"])
    print(f"要钉住：{', '.join(models)}")
    r = warm.pin(models, verbose=print)

    print()
    if not r.get("ok"):
        print(f"✗ {r.get('error') or '加载失败'}")
        for x in r.get("results") or []:
            if not x["ok"]:
                print(f"  {x['model']}: {x.get('error')}")
        return 1

    after = warm.loaded()
    print("现在内存里：")
    for m in after:
        print(f"  {m['name']:<18} {m['size_gb']:>5.2f} GB  "
              f"{'已钉住（永不卸载）' if m['pinned'] else '会过期'}")
    mem2 = warm.memory()
    print(f"\n剩余可用 {mem2.get('avail_gb')} GB")
    print("\n下次调用不用等了。释放用： python run.py warm --unload")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="etf-copilot — 面向小资金 A 股 ETF 的半自动投资助手",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default=None, help="配置文件路径，默认 ./config.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("fee-check", help="费用体检（先跑这个）")
    sp.set_defaults(func=cmd_fee_check)

    sp = sub.add_parser("universe", help="构建候选池")
    sp.add_argument("--top", type=int, default=30)
    sp.add_argument("--refresh", action="store_true", help="强制刷新 ETF 列表缓存")
    sp.add_argument("--show-dropped", action="store_true")
    sp.set_defaults(func=cmd_universe)

    sp = sub.add_parser("scan", help="扫描买入信号")
    sp.add_argument("--top", type=int, default=15)
    sp.add_argument("--limit", type=int, default=50, help="按成交额取前 N 只做深度分析")
    sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(func=cmd_scan)

    def add_ticket_args(sp):
        sp.add_argument("--code", default=None, help="指定标的；不填则自动选分数最高的")
        sp.add_argument("--limit", type=int, default=50)
        sp.add_argument("--no-ai", action="store_true", help="跳过 AI 交叉复核")
        sp.add_argument("--dry-run", action="store_true", help="AI 复核走占位结果，不真实调用")
        sp.add_argument("--only", nargs="*", default=None, help="只用指定的模型 id 复核")
        sp.add_argument("--force-buy", action="store_true", help="有持仓时也生成建仓工单")
        sp.add_argument("--ignore-signal", action="store_true",
                        help="策略没给买入信号时也出单（这是你的决定，不是系统的建议）")

    sp = sub.add_parser("ticket", help="生成交易工单")
    add_ticket_args(sp)
    sp.set_defaults(func=cmd_ticket)

    sp = sub.add_parser("status", help="账户与持仓")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("fill", help="登记一笔真实成交")
    sp.add_argument("--code")
    sp.add_argument("--side", choices=["buy", "sell", "BUY", "SELL"])
    sp.add_argument("--price", type=float)
    sp.add_argument("--shares", type=int)
    sp.add_argument("--name")
    sp.add_argument("--note")
    sp.set_defaults(func=cmd_fill)

    sp = sub.add_parser("sell", help="对已持仓标的判断该不该卖（等同 ticket）")
    add_ticket_args(sp)
    sp.set_defaults(func=cmd_ticket)

    sp = sub.add_parser("backtest", help="回测")
    sp.add_argument("--codes", nargs="*", default=None)
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--top", type=int, default=15)
    sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("plan", help="定投计划（按手数排期，含一次性 vs 分批历史对照）")
    sp.add_argument("--code", default=None, help="不填则默认选成交额最大的宽基 ETF")
    sp.add_argument("--monthly", type=float, default=0.0, help="每期追加金额，默认 0（只投本金）")
    sp.add_argument("--months", type=int, default=12)
    sp.add_argument("--compare", type=int, default=4, help="历史对照的标的数量")
    sp.add_argument("--no-compare", action="store_true")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("flow", help="跑一遍完整工作流（数据→候选池→信号→风控→AI→待办）")
    sp.add_argument("--code", default=None, help="指定标的；不填则自动扫描")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--no-ai", action="store_true", help="跳过 AI 复核")
    sp.add_argument("--dry-run", action="store_true", help="AI 走占位结果，不真实调用")
    sp.add_argument("--ignore-signal", action="store_true",
                    help="策略没给买入信号也继续（你的决定，不是系统的建议）")
    sp.add_argument("--max-todos", type=int, default=3)
    sp.add_argument("--demo", action="store_true",
                    help="放宽费用闸门以便演示全链路。结果会标记为 DEMO，不可当结论")
    sp.set_defaults(func=cmd_flow)

    sp = sub.add_parser("todos", help="待办队列：查看 / 推进状态 / 登记成交")
    sp.add_argument("id", nargs="?", default=None, help="待办 ID；不填则列出队列")
    sp.add_argument("--to", default=None, help="推进到某状态")
    sp.add_argument("--reason", default="", help="推进原因")
    sp.add_argument("--abandon", default=None, help="放弃并填原因（必填）")
    sp.add_argument("--fill", nargs=2, type=float, metavar=("价格", "份数"),
                    help="登记成交")
    sp.add_argument("--fee", type=float, default=0.0, help="券商实际扣的费用")
    sp.set_defaults(func=cmd_todos)

    sp = sub.add_parser("find", help="用本地小模型在同花顺代码表里检索")
    sp.add_argument("query", help="自然语言查询，如「茅台」「防守型」「光伏」")
    sp.add_argument("--top", type=int, default=8)
    sp.add_argument("--no-llm", action="store_true", help="只用确定性预筛，不调本地模型")
    sp.add_argument("--renames", action="store_true",
                    help="查历史曾用名（用旧名找现在的代码）")
    sp.set_defaults(func=cmd_find)

    sp = sub.add_parser("look", help="截图 + 本地视觉模型读同花顺界面")
    sp.add_argument("--screen", action="store_true", help="抓整屏而不是窗口")
    sp.add_argument("--process", default=None, help="进程名，默认 hexin（同花顺）")
    sp.add_argument("--ask", default=None, help="对整张截图问一个自由问题")
    sp.add_argument("--max-tokens", type=int, default=800)
    sp.add_argument("--windows", action="store_true", help="列出有窗口的进程")
    sp.set_defaults(func=cmd_look)

    sp = sub.add_parser("dip", help="超跌筛选：找回调不破位的标的，并把手续费现实摆出来")
    sp.add_argument("--max-price", type=float, default=5.0, help="单价上限，默认 5 元")
    sp.add_argument("--min-amount", type=float, default=5000.0, help="日成交额下限（万元）")
    sp.add_argument("--kind", choices=["etf", "stock", "all"], default="etf")
    sp.add_argument("--top", type=int, default=12)
    sp.add_argument("--positions", type=int, default=4, help="打算持几只")
    sp.add_argument("--daily-target", type=float, default=5.0, help="目标日收益（元）")
    sp.add_argument("--refresh", action="store_true", help="强制刷新全市场行情")
    sp.set_defaults(func=cmd_dip)

    sp = sub.add_parser("budget", help="API 花费台账与预算")
    sp.add_argument("--reset", action="store_true",
                    help="归档台账（不删除，改名为 .bak）")
    sp.set_defaults(func=cmd_budget)

    sp = sub.add_parser("longplan", help="长期定投计划：按整手约束和真实手续费重算")
    sp.add_argument("--schedule", default=None,
                    help="月投节奏，逗号分隔，如 100,300,400,500,500")
    sp.add_argument("--codes", nargs="*", default=None,
                    help="标的代码，如 601288 601398；不填用计划推荐的银行股")
    sp.add_argument("--yield-pct", type=float, default=5.0, help="股息率假设（%%）")
    sp.add_argument("--start-age", type=int, default=18)
    sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(func=cmd_longplan)

    sp = sub.add_parser("train", help="训练模式：跑完整流水线但不落待办，用于磨合系统")
    sp.add_argument("--code", default=None)
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--max-todos", type=int, default=3)
    sp.add_argument("--no-ai", action="store_true", help="跳过 AI 复核（快很多）")
    sp.add_argument("--demo", action="store_true",
                    help="放宽成本类闸门，用来演练完整链路（结果不是真实结论）")
    sp.add_argument("--ignore-signal", action="store_true",
                    help="即使策略没给买入信号也强制走完流程（演练用）")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_train)

    sp = sub.add_parser("train-report", help="训练累积统计：闸门拦截排行、阶段健康度、异常清单")
    sp.set_defaults(func=cmd_train_report)

    sp = sub.add_parser("probe", help="实测费率探针：输入真实扣费，看它改写哪些门槛")
    sp.add_argument("--buy-fee", type=float, required=True, help="买入实际佣金（元）")
    sp.add_argument("--sell-fee", type=float, default=0.0, help="卖出实际佣金（元）")
    sp.add_argument("--price", type=float, required=True, help="成交价")
    sp.add_argument("--shares", type=int, default=100, help="成交份数，默认 100")
    sp.add_argument("--code", default=None)
    sp.add_argument("--apply", action="store_true",
                    help="确认为免五后，把 min_commission 改成 0（先备份）")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("ask", help="问行情：本地小模型 + 实时工具，不用你查代码")
    sp.add_argument("question", help="自然语言问题，例如 \"农业银行现在多少钱\"")
    sp.add_argument("--model", default="qwen2.5:7b",
                    help="默认 qwen2.5:7b（实测它会调工具；r1 不会）")
    sp.add_argument("--verbose", action="store_true", help="显示工具调用过程")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("ths", help="把同花顺拉起来摆成小窗，之后随时可读（不抢焦点）")
    sp.add_argument("--status", action="store_true", help="只看状态，不动窗口")
    sp.add_argument("--corner", default="bottomright",
                    choices=["topleft", "topright", "bottomleft", "bottomright"])
    sp.add_argument("--width", type=int, default=1350,
                    help="窗口宽度，默认 1350（实测刚好放下涨幅/现价/涨跌）")
    sp.add_argument("--height", type=int, default=620)
    sp.add_argument("--capture", action="store_true", help="摆好后试抓一张验证")
    sp.set_defaults(func=cmd_ths)

    sp = sub.add_parser("dock", help="贴着同花顺的常驻状态面板（零依赖 tkinter）")
    sp.add_argument("--width", type=int, default=380)
    sp.add_argument("--interval", type=int, default=60, help="自动刷新秒数")
    sp.add_argument("--check", action="store_true", help="只算位置，不启动面板")
    sp.set_defaults(func=cmd_dock)

    sp = sub.add_parser("warm", help="把本地模型钉在内存里，别闲着就卸载")
    sp.add_argument("--set", default=None, choices=["chat", "vision", "find"],
                    help="预设档位，默认 chat（qwen2.5:7b）")
    sp.add_argument("--models", nargs="*", default=None,
                    help="直接指定模型名，如 qwen3.5:4b")
    sp.add_argument("--list", action="store_true", help="看有哪些档位和当前状态")
    sp.add_argument("--unload", action="store_true", help="释放内存里的模型")
    sp.set_defaults(func=cmd_warm)

    sp = sub.add_parser("serve", help="启动本地 Web 工作台")
    sp.add_argument("--port", type=int, default=8800)
    sp.add_argument("--host", default="127.0.0.1",
                    help="默认只本机。手机访问用 --host 0.0.0.0")
    sp.add_argument("--token", default="",
                    help="局域网口令。不填则绑非本机地址时自动生成")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("pet", help="瑶瑶桌宠 2D 版（素材帧，透明置顶，可问行情）")
    sp.set_defaults(func=cmd_pet)

    sp = sub.add_parser("pet3d", help="瑶瑶桌宠 3D 版（官方 PMX 建模 + OpenGL）")
    sp.set_defaults(func=cmd_pet3d)

    sp = sub.add_parser("workbench", help="同花顺风格的桌面工作台（原生软件窗口）")
    sp.set_defaults(func=cmd_workbench)

    sp = sub.add_parser("up", help="一条命令拉起整套：同花顺小窗+模型+桌宠+工作台")
    sp.add_argument("--port", type=int, default=8800)
    sp.add_argument("--no-pet", action="store_true", help="不要桌宠")
    sp.add_argument("--web", action="store_true",
                    help="额外起 Web 服务（手机访问用），默认只开桌面工作台")
    sp.add_argument("--corner", default="bottomright",
                    choices=["topleft", "topright", "bottomleft", "bottomright"])
    sp.set_defaults(func=cmd_up)

    args = p.parse_args()
    try:
        cfg = load_config(args.config)
        ensure_dirs(cfg)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
