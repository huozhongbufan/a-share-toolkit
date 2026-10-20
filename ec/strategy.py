"""策略层：趋势跟随 + 估值分位过滤。

为什么是这个策略，而不是预测涨跌：
  - 本金 500 元，1 手就是全部仓位，不可能分散，所以只能做『大方向』
  - 股票型 ETF 是 T+1，短线做不了
  - 费用极重（往返可能 2%），必须低频

所以规则很朴素：只在中期趋势向上、且价格不在历史高位时买；趋势走坏就走。
分数只用于排序，不用于预测收益。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .config import Config
from .indicators import sma, atr, atr_pct, rsi, momentum, price_percentile
from .quotes import Bar
from .universe import Candidate

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"
AVOID = "AVOID"


@dataclass
class Signal:
    code: str
    name: str
    action: str
    price: float
    score: float = 0.0
    ma_fast: float = 0.0
    ma_slow: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0
    rsi: float = 0.0
    mom: float = 0.0
    pct_rank: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    t0: bool = False
    lot_cost: float = 0.0
    amount_wan: float = 0.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _round(v: float | None, nd: int = 3) -> float:
    return round(v, nd) if v is not None else 0.0


def evaluate(cfg: Config, cand: Candidate, bars: list[Bar]) -> Signal | None:
    """对单只 ETF 出信号。数据不够就返回 None，不猜。"""
    need = max(cfg.strategy.ma_slow, cfg.strategy.pct_window // 4, 30) + 5
    if len(bars) < need:
        return None

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]

    ma_f = sma(closes, cfg.strategy.ma_fast)
    ma_s = sma(closes, cfg.strategy.ma_slow)
    a = atr(highs, lows, closes, 14)
    ap = atr_pct(highs, lows, closes, 14)
    r = rsi(closes, 14)
    m = momentum(closes, cfg.strategy.mom_window)
    pr = price_percentile(closes, cfg.strategy.pct_window)

    i = len(closes) - 1
    if None in (ma_f[i], ma_s[i], a[i], ap[i], m[i], pr[i]):
        return None

    close = closes[i]
    sig = Signal(
        code=cand.code,
        name=cand.name,
        action=AVOID,
        price=close,
        ma_fast=_round(ma_f[i]),
        ma_slow=_round(ma_s[i]),
        atr=_round(a[i], 4),
        atr_pct=_round(ap[i], 5),
        rsi=_round(r[i] or 0.0, 1),
        mom=_round(m[i], 5),
        pct_rank=_round(pr[i], 4),
        t0=cand.t0,
        lot_cost=round(cand.lot_cost, 2),
        amount_wan=round(cand.amount_wan, 0),
    )

    uptrend = close > ma_f[i] and ma_f[i] > ma_s[i]
    above_slow = close > ma_s[i]
    momentum_ok = m[i] > 0
    not_expensive = pr[i] <= cfg.strategy.max_price_percentile
    vol_ok = cfg.strategy.min_atr_pct <= ap[i] <= cfg.strategy.max_atr_pct

    if not uptrend:
        sig.reasons.append("均线未成多头排列" if close <= ma_f[i] else "MA20 未站上 MA60")
    if not momentum_ok:
        sig.reasons.append(f"{cfg.strategy.mom_window}日动量为负")
    if not not_expensive:
        sig.reasons.append(f"价格处于{cfg.strategy.pct_window}日{pr[i]*100:.0f}%分位(偏贵)")
    if ap[i] < cfg.strategy.min_atr_pct:
        sig.reasons.append(f"波动过低({ap[i]*100:.2f}%),不值得承担费用")
    elif ap[i] > cfg.strategy.max_atr_pct:
        sig.reasons.append(f"波动过高({ap[i]*100:.2f}%),接近赌博")
    if sig.amount_wan < cfg.universe.min_avg_turnover_wan:
        sig.warnings.append("成交额偏低,冲击成本可能变大")
    if cand.spread_pct > 0.003:
        sig.warnings.append(f"买卖价差{cand.spread_pct*100:.2f}%,偏宽")

    # 止损止盈位无条件算出来。即使是 AVOID 的信号,用户也需要看到
    # 『如果我非要买,该在哪里认错』—— 这个数字本身就是劝阻材料。
    sig.stop = round(close - cfg.risk.stop_loss_atr * a[i], 3)
    sig.target = round(close + cfg.risk.take_profit_atr * a[i], 3)
    if not above_slow:
        sig.warnings.append("价格在 MA60 下方,趋势未确认")

    if uptrend and momentum_ok and not_expensive and vol_ok:
        sig.action = BUY
        # 打分：动量 50% + 趋势强度 30% + 便宜程度 20%。只为排序，不预测收益。
        trend_strength = (close - ma_s[i]) / ma_s[i]
        sig.score = round(
            0.5 * min(m[i] / 0.15, 1.0)
            + 0.3 * min(max(trend_strength / 0.10, 0.0), 1.0)
            + 0.2 * (1.0 - pr[i]),
            4,
        )
    return sig


def scan(cfg: Config, candidates: list[Candidate], bar_loader) -> list[Signal]:
    """扫描整个候选池，返回按分数排序的 BUY 信号。"""
    out: list[Signal] = []
    for cand in candidates:
        try:
            bars = bar_loader(cand.code)
        except Exception:
            continue
        sig = evaluate(cfg, cand, bars)
        if sig and sig.action == BUY:
            out.append(sig)
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def evaluate_exit(cfg: Config, code: str, name: str, entry_price: float, bars: list[Bar],
                  t0: bool = False) -> Signal:
    """对已持仓标的判断是否该卖。

    注意：退出判断不走建仓风控闸门。任何情况下都必须能卖出，
    风控闸门只拦买入，绝不拦止损。
    """
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    ma_f = sma(closes, cfg.strategy.ma_fast)
    a = atr(highs, lows, closes, 14)
    i = len(closes) - 1
    close = closes[i]

    sig = Signal(
        code=code, name=name, action=HOLD, price=close,
        ma_fast=_round(ma_f[i] if ma_f[i] is not None else 0.0),
        atr=_round(a[i] if a[i] is not None else 0.0, 4),
        t0=t0,
    )
    if a[i] is None:
        sig.warnings.append("ATR 数据不足,无法算止损位")
        return sig

    stop = entry_price - cfg.risk.stop_loss_atr * a[i]
    target = entry_price + cfg.risk.take_profit_atr * a[i]
    sig.stop = round(stop, 3)
    sig.target = round(target, 3)
    pnl = (close - entry_price) / entry_price if entry_price else 0.0

    n = max(1, cfg.strategy.exit_confirm_days)
    below = 0
    for j in range(i, max(-1, i - n), -1):
        if ma_f[j] is not None and closes[j] < ma_f[j]:
            below += 1
        else:
            break

    if below >= n:
        sig.action = SELL
        sig.reasons.append(
            f"连续 {n} 天收盘跌破 MA{cfg.strategy.ma_fast},趋势走坏")
    if close <= stop:
        sig.action = SELL
        sig.reasons.append(f"触及止损 {stop:.3f}({cfg.risk.stop_loss_atr}×ATR)")
    if close >= target:
        sig.action = SELL
        sig.reasons.append(f"触及止盈 {target:.3f}({cfg.risk.take_profit_atr}×ATR)")
    if not sig.reasons:
        sig.reasons.append(f"趋势仍在,继续持有(浮动盈亏 {pnl*100:+.2f}%)")
    return sig
