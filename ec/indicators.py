"""技术指标。全部纯函数，输入 list[float]，输出同长度 list（不足处为 None）。

不用 numpy/pandas：一是避免安装麻烦，二是回测里一眼能看出每个数怎么来的。
"""
from __future__ import annotations

from typing import Sequence

Num = float | None


def sma(values: Sequence[float], window: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    if window <= 0:
        return out
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= window:
            total -= values[i - window]
        if i >= window - 1:
            out[i] = total / window
    return out


def ema(values: Sequence[float], span: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    if span <= 0 or not values:
        return out
    k = 2.0 / (span + 1.0)
    prev: float | None = None
    for i, v in enumerate(values):
        prev = v if prev is None else (v - prev) * k + prev
        out[i] = prev
    return out


def true_range(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> list[float]:
    out: list[float] = []
    for i in range(len(close)):
        if i == 0:
            out.append(high[i] - low[i])
            continue
        pc = close[i - 1]
        out.append(max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc)))
    return out


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], window: int = 14) -> list[Num]:
    tr = true_range(high, low, close)
    return sma(tr, window)


def atr_pct(high: Sequence[float], low: Sequence[float], close: Sequence[float], window: int = 14) -> list[Num]:
    """ATR 占价格的比例 —— 用来判断这只 ETF『波动够不够吃肉 / 是不是在赌』。"""
    a = atr(high, low, close, window)
    return [(a[i] / close[i]) if (a[i] is not None and close[i]) else None for i in range(len(close))]


def rsi(values: Sequence[float], window: int = 14) -> list[Num]:
    out: list[Num] = [None] * len(values)
    if len(values) <= window:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, window + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / window, losses / window
    out[window] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(window + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (window - 1) + max(d, 0.0)) / window
        avg_l = (avg_l * (window - 1) + max(-d, 0.0)) / window
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def momentum(values: Sequence[float], window: int = 20) -> list[Num]:
    """N 日收益率。"""
    out: list[Num] = [None] * len(values)
    for i in range(window, len(values)):
        base = values[i - window]
        out[i] = (values[i] - base) / base if base else None
    return out


def price_percentile(values: Sequence[float], window: int = 250) -> list[Num]:
    """当前价在过去 window 天里的分位（0 = 最低，1 = 最高）。

    这是估值的粗糙代理：对宽基 ETF 够用，对行业 ETF 只能当参考。
    高分位意味着『贵』，是拒绝买入的理由之一，不是卖出理由。
    """
    out: list[Num] = [None] * len(values)
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        seg = values[lo:i + 1]
        if len(seg) < max(20, window // 4):
            continue
        cur = values[i]
        below = sum(1 for v in seg if v <= cur)
        out[i] = below / len(seg)
    return out


def max_drawdown(values: Sequence[float]) -> float:
    """最大回撤（负数）。"""
    peak = float("-inf")
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v - peak) / peak)
    return worst


def annualized_return(values: Sequence[float], bars_per_year: int = 244) -> float:
    if len(values) < 2 or values[0] <= 0:
        return 0.0
    total = values[-1] / values[0]
    years = len(values) / bars_per_year
    if years <= 0:
        return 0.0
    return total ** (1.0 / years) - 1.0


def annualized_vol(values: Sequence[float], bars_per_year: int = 244) -> float:
    if len(values) < 3:
        return 0.0
    rets = [(values[i] - values[i - 1]) / values[i - 1]
            for i in range(1, len(values)) if values[i - 1]]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return (var ** 0.5) * (bars_per_year ** 0.5)


def sharpe(values: Sequence[float], rf: float = 0.0, bars_per_year: int = 244) -> float:
    vol = annualized_vol(values, bars_per_year)
    if vol <= 0:
        return 0.0
    return (annualized_return(values, bars_per_year) - rf) / vol
