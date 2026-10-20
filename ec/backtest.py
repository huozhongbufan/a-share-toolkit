"""回测。这个模块存在的唯一目的是回答一个问题:

    这套规则赚的钱,够不够付手续费?

对 500 元本金 + 5 元最低佣金的账户,答案很可能是『不够』。回测会把这件事
用数字摆出来,而不是靠感觉。

三条防自欺的规则:
  1. **信号在收盘后产生,下一根 K 线开盘价成交**。A 股半自动流程本来就是这样,
     顺便杜绝了用当天收盘价买入这种未来函数。
  2. **同一根 K 线同时触及止损和止盈时,按止损算**。悲观假设,不给策略贴金。
  3. **买入费用和卖出费用都记在交易上**,净盈亏是扣完两边的数。只扣一边会
     让回测结果比实盘好看一倍。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, asdict

from .config import Config
from .indicators import (sma, atr, atr_pct, momentum, price_percentile,
                         max_drawdown, annualized_return, sharpe)
from .quotes import Bar


@dataclass
class Trade:
    entry_date: str
    entry_price: float      # 实际成交价,不含费用
    exit_date: str
    exit_price: float
    shares: int
    buy_fee: float
    sell_fee: float
    gross_pnl: float        # 未扣费用的价差盈亏
    net_pnl: float          # 扣掉双边费用后的真实盈亏
    reason: str

    @property
    def total_fee(self) -> float:
        return self.buy_fee + self.sell_fee

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_fee"] = round(self.total_fee, 2)
        return d


@dataclass
class BacktestResult:
    code: str
    name: str
    start: str
    end: str
    bars: int
    initial_capital: float
    final_equity: float
    strategy_return: float
    strategy_annual: float
    hold_return: float
    hold_annual: float
    strategy_mdd: float
    hold_mdd: float
    sharpe: float
    trades: list[Trade] = field(default_factory=list)
    total_fees: float = 0.0
    gross_pnl_sum: float = 0.0
    fee_pct_of_capital: float = 0.0
    win_rate: float = 0.0
    avg_hold_days: float = 0.0
    avg_breakeven_pct: float = 0.0
    note: str = ""

    @property
    def n_round_trips(self) -> int:
        return len(self.trades)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["trades"] = [t.to_dict() for t in self.trades]
        d["n_round_trips"] = self.n_round_trips
        return d


def _precompute(cfg: Config, bars: list[Bar]) -> dict:
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    return {
        "closes": closes, "highs": highs, "lows": lows,
        "ma_f": sma(closes, cfg.strategy.ma_fast),
        "ma_s": sma(closes, cfg.strategy.ma_slow),
        "atr": atr(highs, lows, closes, 14),
        "atr_pct": atr_pct(highs, lows, closes, 14),
        "mom": momentum(closes, cfg.strategy.mom_window),
        "pct": price_percentile(closes, cfg.strategy.pct_window),
    }


def _entry_ok(cfg: Config, ind: dict, i: int) -> bool:
    c, mf, ms = ind["closes"][i], ind["ma_f"][i], ind["ma_s"][i]
    ap, mo, pr = ind["atr_pct"][i], ind["mom"][i], ind["pct"][i]
    if None in (mf, ms, ap, mo, pr):
        return False
    s = cfg.strategy
    return (c > mf and mf > ms and mo > 0
            and pr <= s.max_price_percentile
            and s.min_atr_pct <= ap <= s.max_atr_pct)


def _days(a: str, b: str) -> int:
    try:
        return (_dt.datetime.strptime(b, "%Y-%m-%d")
                - _dt.datetime.strptime(a, "%Y-%m-%d")).days
    except ValueError:
        return 0


def backtest_etf(cfg: Config, code: str, name: str, bars: list[Bar],
                 capital: float | None = None) -> BacktestResult | None:
    cap = capital if capital is not None else cfg.capital
    warmup = max(cfg.strategy.ma_slow, 20) + 2
    if len(bars) < warmup + 30:
        return None

    ind = _precompute(cfg, bars)
    closes = ind["closes"]

    cash = cap
    shares = 0
    entry_price = 0.0
    entry_date = ""
    entry_fee = 0.0
    trades: list[Trade] = []
    equity_curve: list[float] = []
    pending_buy = False
    pending_sell = ""

    def close_position(px: float, day: str, reason: str) -> None:
        """按 px 平掉全部持仓,双边费用完整记账。"""
        nonlocal cash, shares, entry_price, entry_fee
        notional = shares * px
        sell_fee = cfg.fees.one_way(notional)
        cash += notional - sell_fee
        gross = (px - entry_price) * shares
        trades.append(Trade(
            entry_date=entry_date, entry_price=round(entry_price, 4),
            exit_date=day, exit_price=round(px, 4), shares=shares,
            buy_fee=round(entry_fee, 2), sell_fee=round(sell_fee, 2),
            gross_pnl=round(gross, 2),
            net_pnl=round(gross - entry_fee - sell_fee, 2),
            reason=reason,
        ))
        shares, entry_price, entry_fee = 0, 0.0, 0.0

    for i in range(warmup, len(bars)):
        bar = bars[i]

        # ---- 昨天收盘后挂的单,今天开盘成交 ----
        if pending_buy and shares == 0:
            px = bar.open
            lots = int(cash // (px * 100))
            if lots > 0:
                n = lots * 100
                notional = n * px
                fee = cfg.fees.one_way(notional)
                if notional + fee <= cash:
                    cash -= notional + fee
                    shares = n
                    entry_price = px
                    entry_fee = fee
                    entry_date = bar.date
            pending_buy = False
        elif pending_sell and shares > 0:
            close_position(bar.open, bar.date, pending_sell)
            pending_sell = ""

        # ---- 盘中触发止损 / 止盈 ----
        if shares > 0 and ind["atr"][i] is not None:
            stop = entry_price - cfg.risk.stop_loss_atr * ind["atr"][i]
            target = entry_price + cfg.risk.take_profit_atr * ind["atr"][i]
            if bar.low <= stop:
                # 跳空低开时按开盘价成交,比按止损价成交更悲观
                close_position(min(stop, bar.open), bar.date, "止损")
            elif bar.high >= target:
                close_position(max(target, bar.open), bar.date, "止盈")

        # ---- 收盘后产生信号,留给明天开盘 ----
        if shares == 0:
            if _entry_ok(cfg, ind, i):
                pending_buy = True
        else:
            # 需要连续 exit_confirm_days 天收在 MA_fast 下方才离场。
            # 设 1 会被日常波动反复扫出去,每一次扫出去都是一次真实的手续费。
            n = max(1, cfg.strategy.exit_confirm_days)
            below = 0
            for j in range(i, max(-1, i - n), -1):
                mf_j = ind["ma_f"][j]
                if mf_j is not None and closes[j] < mf_j:
                    below += 1
                else:
                    break
            if below >= n:
                pending_sell = f"连续{n}天跌破MA{cfg.strategy.ma_fast}"

        equity_curve.append(cash + shares * closes[i])

    if shares > 0:
        close_position(closes[-1], bars[-1].date, "回测结束清仓")
        equity_curve[-1] = cash

    final = cash
    total_fees = sum(t.total_fee for t in trades)
    gross_sum = sum(t.gross_pnl for t in trades)
    wins = [t for t in trades if t.net_pnl > 0]
    hold = closes[-1] / closes[warmup] - 1.0

    avg_days = (sum(_days(t.entry_date, t.exit_date) for t in trades) / len(trades)
                if trades else 0.0)
    avg_be = (sum(cfg.fees.breakeven_pct(t.entry_price * t.shares) for t in trades)
              / len(trades) if trades else 0.0)

    note = ""
    if not trades:
        note = ("整段历史里没有出现过一次符合条件的买入信号。"
                "这本身是个结论:要么规则太严,要么这段时间不适合它。")
    elif total_fees > abs(final - cap) and final < cap:
        note = ("累计手续费超过了净亏损 —— 亏掉的钱主要是交给券商的,不是市场拿走的。")
    elif final < cap and hold > 0:
        note = f"策略亏损而买入持有赚了 {hold*100:.1f}%,说明择时在这只标的上是负贡献。"

    return BacktestResult(
        code=code, name=name,
        start=bars[warmup].date, end=bars[-1].date,
        bars=len(bars) - warmup,
        initial_capital=round(cap, 2),
        final_equity=round(final, 2),
        strategy_return=round(final / cap - 1.0, 6),
        strategy_annual=round(annualized_return(equity_curve), 6),
        hold_return=round(hold, 6),
        hold_annual=round(annualized_return(closes[warmup:]), 6),
        strategy_mdd=round(max_drawdown(equity_curve), 6),
        hold_mdd=round(max_drawdown(closes[warmup:]), 6),
        sharpe=round(sharpe(equity_curve), 3),
        trades=trades,
        total_fees=round(total_fees, 2),
        gross_pnl_sum=round(gross_sum, 2),
        fee_pct_of_capital=round(total_fees / cap, 6),
        win_rate=round(len(wins) / len(trades), 4) if trades else 0.0,
        avg_hold_days=round(avg_days, 1),
        avg_breakeven_pct=round(avg_be, 6),
        note=note,
    )


def render_backtest_markdown(results: list[BacktestResult], cfg: Config) -> str:
    L: list[str] = []
    traded = [r for r in results if r.n_round_trips > 0]
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731

    L.append("# 回测报告")
    L.append("")
    L.append(f"本金 {cfg.capital:.0f} 元 ｜ 佣金率 {cfg.fees.commission_rate} ｜ "
             f"最低佣金 {cfg.fees.min_commission:.0f} 元 ｜ 滑点 {cfg.fees.slippage_bps} bp")
    L.append(f"策略参数：MA{cfg.strategy.ma_fast}/MA{cfg.strategy.ma_slow} ｜ "
             f"动量 {cfg.strategy.mom_window} 日 ｜ "
             f"分位上限 {cfg.strategy.max_price_percentile} ｜ "
             f"离场确认 {cfg.strategy.exit_confirm_days} 天 ｜ "
             f"止损 {cfg.risk.stop_loss_atr}×ATR ｜ 止盈 {cfg.risk.take_profit_atr}×ATR")
    L.append("")
    L.append("信号在收盘后产生、下一根 K 线开盘成交；同时触及止损止盈时按止损计。")
    L.append("")

    # ---- 结论先行 ----
    L.append("## 结论")
    L.append("")
    L.append(f"回测标的 {len(results)} 只，其中 {len(traded)} 只在这段时间里真的触发过交易，"
             f"{len(results)-len(traded)} 只一次都没买过。")
    L.append("")
    if results:
        L.append(f"- 策略平均收益 **{avg([r.strategy_return for r in results])*100:+.2f}%**")
        L.append(f"- 同期买入持有平均 **{avg([r.hold_return for r in results])*100:+.2f}%**")
        L.append(f"- 策略平均最大回撤 {avg([r.strategy_mdd for r in results])*100:.2f}%，"
                 f"买入持有 {avg([r.hold_mdd for r in results])*100:.2f}%")
    if traded:
        L.append(f"- 有过交易的 {len(traded)} 只：平均 "
                 f"{avg([r.n_round_trips for r in traded]):.1f} 次往返，"
                 f"平均胜率 {avg([r.win_rate for r in traded])*100:.0f}%，"
                 f"平均每次往返的盈亏平衡点是 {avg([r.avg_breakeven_pct for r in traded])*100:.3f}%")
        L.append(f"- 这 {len(traded)} 只的累计手续费 {sum(r.total_fees for r in traded):.2f} 元，"
                 f"相当于每只本金 {cfg.capital:.0f} 元的 "
                 f"{avg([r.fee_pct_of_capital for r in traded])*100:.2f}%")
        L.append("")
        if avg([r.strategy_return for r in traded]) < avg([r.hold_return for r in traded]):
            L.append("**策略跑输了买入持有。** 这是小资金 + 高固定费用账户的典型结果：")
            L.append("每次交易都要先跨过一笔固定成本，交易越多次，跨过的次数越多。")
            L.append("要么降低交易频率，要么换一家对场内基金免五的券商。")
        else:
            L.append("**策略跑赢了买入持有。** 但要注意回测区间只有一段，"
                     "样本外的表现没有保证。")
        L.append("")

    L.append("## 全部标的")
    L.append("")
    L.append("| 标的 | 区间 | 策略收益 | 买入持有 | 策略回撤 | 持有回撤 | 往返次数 | 累计费用 | 费用/本金 | 胜率 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        L.append(
            f"| {r.code[2:]} {r.name} | {r.start[2:]}~{r.end[2:]} | "
            f"{r.strategy_return*100:+.2f}% | {r.hold_return*100:+.2f}% | "
            f"{r.strategy_mdd*100:.2f}% | {r.hold_mdd*100:.2f}% | {r.n_round_trips} | "
            f"{r.total_fees:.2f} | {r.fee_pct_of_capital*100:.2f}% | {r.win_rate*100:.0f}% |"
        )
    L.append("")
    L.append("> 每个标的都是一次独立的 {:.0f} 元回测，费用列不能跨标的相加。".format(cfg.capital))
    L.append("")
    L.append("## 逐只点评")
    L.append("")
    for r in results:
        L.append(f"### {r.code[2:]} {r.name}")
        L.append("")
        L.append(f"- 区间 {r.start} ~ {r.end}，共 {r.bars} 个交易日")
        L.append(f"- 策略 {r.initial_capital:.0f} → {r.final_equity:.2f} 元"
                 f"（{r.strategy_return*100:+.2f}%，年化 {r.strategy_annual*100:+.1f}%）")
        L.append(f"- 同期买入持有 {r.hold_return*100:+.2f}%（年化 {r.hold_annual*100:+.1f}%）")
        L.append(f"- 最大回撤：策略 {r.strategy_mdd*100:.2f}% vs 持有 {r.hold_mdd*100:.2f}%")
        L.append(f"- 往返 {r.n_round_trips} 次，平均持仓 {r.avg_hold_days:.0f} 天，"
                 f"胜率 {r.win_rate*100:.0f}%")
        L.append(f"- 累计手续费 {r.total_fees:.2f} 元 = 本金的 {r.fee_pct_of_capital*100:.2f}%；"
                 f"每次往返盈亏平衡需要 {r.avg_breakeven_pct*100:.3f}%")
        if r.gross_pnl_sum:
            L.append(f"- 价差毛盈亏 {r.gross_pnl_sum:+.2f} 元，"
                     f"扣掉手续费后 {r.gross_pnl_sum - r.total_fees:+.2f} 元")
        if r.note:
            L.append(f"- **{r.note}**")
        if r.trades:
            L.append("")
            L.append("| 买入日 | 买入价 | 卖出日 | 卖出价 | 份数 | 双边费用 | 净盈亏 | 原因 |")
            L.append("|---|---|---|---|---|---|---|---|")
            for t in r.trades:
                L.append(f"| {t.entry_date} | {t.entry_price:.3f} | {t.exit_date} | "
                         f"{t.exit_price:.3f} | {t.shares} | {t.total_fee:.2f} | "
                         f"{t.net_pnl:+.2f} | {t.reason} |")
        L.append("")
    return "\n".join(L)
