"""超跌筛选：找「跌下来的、但趋势还没坏」的标的。

为什么方向是「逢跌」而不是「追涨」
--------------------------------
两个独立来源都指向同一件事：

  1. 我自己在 40 只 ETF 上回测趋势跟随（买在 MA20 之上、动量向上），
     全部跑输买入持有。胜率只有 25% 左右。
  2. 开源项目 `etf-dca-strategy`（25 轮双周期验证）的定投打分是
     **逆向指标，高分 = 超跌** —— 跌得多的时候买更多。

为什么 ETF 比个股更适合逢跌买
------------------------------
指数跌下去大概率会回来（成分股会换、会有新的龙头）；
个股跌下去可能永远回不来（退市、业绩塌方、造假）。
**低价股尤其如此** —— 便宜的往往是有问题的。
所以「跌得多」在 ETF 上是机会，在个股上更多是警号。

「稳」的关键：区分回调与破位
----------------------------
这一层是这个模块的核心。同样跌 15%，两种含义完全不同：

    回调：还在 MA60 上方，只是从高点退下来   → 趋势没坏，可以接
    破位：已经跌破 MA60，且持续走弱          → 趋势坏了，别接

稳的做法是**只买回调，不买破位**。追「跌得最狠的」不是稳，是赌。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .config import Config
from .indicators import sma, rsi, price_percentile
from .quotes import Bar

# 判定阈值。都是可解释的，不是拟合出来的。
PULLBACK_MIN = -0.05      # 距 250 日高点至少回撤 5% 才算「跌下来过」
PULLBACK_MAX = -0.35      # 回撤超过 35% 就不算普通回调了
DEEP_DD = -0.25           # 深度回撤
LOW_PCT = 0.20            # 价格分位低于 20% 算低位
HIGH_PCT = 0.65           # 分位高于这个就不算「跌下来」，哪怕距高点有回撤
MAX_DEV_MA60 = 0.05       # 高出 MA60 超过 5% 说明已经涨回去了，不是回调

STATE_PULLBACK = "回调"     # 趋势在，从高点退下来 —— 稳的做法选这个
STATE_BREAK = "破位"        # 跌破 MA60 —— 不碰
STATE_RANGE = "横盘"        # 没怎么跌
STATE_STRONG = "强势"       # 在高位
STATE_UNKNOWN = "数据不足"


@dataclass
class OversoldPick:
    code: str
    name: str
    price: float
    lot_cost: float
    amount_wan: float = 0.0

    dd_250: float = 0.0        # 距 250 日高点的回撤（负数）
    dd_60: float = 0.0
    pct_rank: float = 0.0      # 价格分位 0~1
    rsi: float = 50.0
    dev_ma20: float = 0.0      # 相对 MA20 的偏离
    dev_ma60: float = 0.0
    ret_20: float = 0.0
    ret_60: float = 0.0

    state: str = STATE_UNKNOWN
    score: float = 0.0         # 超跌分，越高越超跌
    tradeable: bool = False    # 稳的做法下是否可考虑
    note: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _dev(price: float, ma: float | None) -> float:
    if ma is None or ma <= 0:
        return 0.0
    return price / ma - 1.0


def evaluate(cfg: Config, code: str, name: str, price: float,
             lot_cost: float, bars: list[Bar],
             amount_wan: float = 0.0) -> OversoldPick | None:
    """对单只标的算超跌指标。数据不够返回 None，不猜。"""
    need = 70
    if len(bars) < need:
        return None

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    i = len(closes) - 1
    cur = closes[i]

    window = closes[-250:] if len(closes) >= 250 else closes
    hi250 = max(window)
    hi60 = max(closes[-60:])

    ma20 = sma(closes, 20)[i]
    ma60 = sma(closes, 60)[i]
    r = rsi(closes, 14)[i]
    pr = price_percentile(closes, 250)[i]

    pick = OversoldPick(
        code=code, name=name, price=round(cur, 3),
        lot_cost=round(lot_cost, 2), amount_wan=round(amount_wan, 0),
        dd_250=round(cur / hi250 - 1, 4) if hi250 else 0.0,
        dd_60=round(cur / hi60 - 1, 4) if hi60 else 0.0,
        pct_rank=round(pr, 4) if pr is not None else 0.0,
        rsi=round(r, 1) if r is not None else 50.0,
        dev_ma20=round(_dev(cur, ma20), 4),
        dev_ma60=round(_dev(cur, ma60), 4),
        ret_20=round(closes[i] / closes[i - 20] - 1, 4) if i >= 20 else 0.0,
        ret_60=round(closes[i] / closes[i - 60] - 1, 4) if i >= 60 else 0.0,
    )

    above_ma60 = ma60 is not None and cur > ma60
    above_ma20 = ma20 is not None and cur > ma20

    # 「涨回高位」的判断必须独立于「距高点回撤」。
    # 实测踩过：嘉实原油LOF 距 250 日高点 -20.9%（看着像跌了很多），
    # 但价格分位 93%、距 MA60 +10.9% —— 那是已经涨回去了，不是超跌。
    # 只看回撤会把它排到第三名。
    near_high = pick.pct_rank >= HIGH_PCT
    extended = pick.dev_ma60 > MAX_DEV_MA60

    # ---- 状态判定 ----
    if pick.dd_250 > -0.03:
        pick.state = STATE_STRONG
        pick.note = "还在 250 日高点附近，没跌下来，不符合『逢跌』"
    elif near_high or extended:
        pick.state = STATE_STRONG
        why = []
        if near_high:
            why.append(f"价格分位 {pick.pct_rank*100:.0f}% 仍在历史高位")
        if extended:
            why.append(f"高出 MA60 {pick.dev_ma60*100:+.1f}%，已经涨回去了")
        pick.note = "距高点虽有回撤，但" + "、".join(why) + " —— 不是超跌"
    elif above_ma60 and pick.dd_250 <= PULLBACK_MIN:
        pick.state = STATE_PULLBACK
        pick.note = "从高点回撤但仍在 MA60 上方 —— 趋势没坏，是回调"
    elif not above_ma60 and pick.dd_250 <= -0.10:
        pick.state = STATE_BREAK
        pick.note = "已跌破 MA60 且从高点回撤较多 —— 趋势走坏，别接"
    else:
        pick.state = STATE_RANGE
        pick.note = "横盘震荡，跌幅不明显"

    # ---- 超跌分：只奖励「回调」，不奖励「破位」 ----
    fall = max(0.0, -pick.dd_250)
    score = 0.0
    if pick.state == STATE_PULLBACK:
        # 跌得越多分越高，但过分深（>35%）要扣回来 —— 那不是回调了
        score = min(fall / 0.20, 1.0) * 60
        if fall > abs(PULLBACK_MAX):
            score -= (fall - abs(PULLBACK_MAX)) * 200
        if above_ma20:
            score += 8                      # 已经开始站回 MA20，起稳了
        if 0.25 <= pick.pct_rank <= 0.65:
            score += 6                      # 分位不高不低，有回升空间
        if pick.rsi < 35:
            score += 10                     # 短线超卖
        # 已经离 MA60 太远的，逐渐减分 —— 越靠近 MA60 越「安全地逢跌」
        if pick.dev_ma60 > 0.02:
            score -= (pick.dev_ma60 - 0.02) * 300
    elif pick.state == STATE_BREAK:
        # 破位的分只给一点点，让它排不到前面去
        score = min(fall / 0.30, 1.0) * 15
    elif pick.state == STATE_STRONG:
        score = 0.0
    else:
        score = min(fall / 0.20, 1.0) * 25

    pick.score = round(max(0.0, score), 1)

    # ---- 可交易性（「稳」的判定）----
    pick.tradeable = (
        pick.state == STATE_PULLBACK
        and pick.dd_250 >= PULLBACK_MAX
        and pick.amount_wan >= cfg.universe.min_avg_turnover_wan
    )
    if pick.state == STATE_PULLBACK and pick.dd_250 < PULLBACK_MAX:
        pick.warnings.append("回撤超过 35%，已经超出『回调』范畴，接近熊市")
    if pick.pct_rank <= LOW_PCT and pick.state == STATE_PULLBACK:
        pick.warnings.append("价格处于 250 日低位区间，接的时候分次建仓")

    return pick


def rank(picks: list[OversoldPick], only_tradeable: bool = True) -> list[OversoldPick]:
    """排序：先按可交易性，再按超跌分。"""
    pool = [p for p in picks if (p.tradeable or not only_tradeable)]
    pool.sort(key=lambda p: (-int(p.tradeable), -p.score))
    return pool


def state_summary(picks: list[OversoldPick]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in picks:
        out[p.state] = out.get(p.state, 0) + 1
    return out
