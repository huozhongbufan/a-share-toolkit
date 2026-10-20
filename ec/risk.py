"""风控硬闸门。

这是整个系统里最重要的一个模块，也是唯一一条设计红线：

    AI 只能在这里投反对票,不能绕过任何一条规则,也不能加大仓位。

每条规则都是纯函数,输入是数字,输出是『过 / 不过 + 原因』。
规则命中就是拒绝出单,没有『但是这次特殊』。

最容易被忽略的一条是 fee_ratio:对 500 元本金 + 5 元最低佣金,
往返费用是 10 元 = 本金的 2%。做 10 次就是 20%,策略再好也赚不回来。
所以费用必须当成一等风控指标,而不是事后扣减。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

from .config import Config

OK = "ok"
BLOCK = "block"
WARN = "warn"


@dataclass
class Check:
    name: str
    level: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.level != BLOCK

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RiskDecision:
    allowed: bool
    checks: list[Check] = field(default_factory=list)
    shares: int = 0
    notional: float = 0.0
    est_cost: float = 0.0
    breakeven_pct: float = 0.0
    required_win_rate: float = 0.0   # 按止损净亏/止盈净赚倒推的盈亏平衡胜率
    gap_loss: float = 0.0            # T+1 隔夜跳空穿透止损时的估计亏损
    gap_loss_pct: float = 0.0

    @property
    def blocks(self) -> list[Check]:
        return [c for c in self.checks if c.level == BLOCK]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.level == WARN]

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "shares": self.shares,
            "notional": round(self.notional, 2),
            "est_cost": round(self.est_cost, 2),
            "breakeven_pct": round(self.breakeven_pct, 6),
            "required_win_rate": round(self.required_win_rate, 4),
            "gap_loss": round(self.gap_loss, 2),
            "gap_loss_pct": round(self.gap_loss_pct, 6),
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class AccountState:
    """当前账户状态。风控需要的全部事实都在这里,不从别处读。"""
    total_capital: float = 500.0
    cash: float = 500.0
    market_value: float = 0.0
    peak_equity: float = 0.0
    today_realized_pnl: float = 0.0
    month_round_trips: int = 0
    days_since_stop: int = 999
    last_stop_date: str = ""

    @property
    def equity(self) -> float:
        return self.cash + self.market_value

    @property
    def total_exposure(self) -> float:
        return self.market_value


def plan_size(cfg: Config, cash_available: float, price: float) -> tuple[int, float, float]:
    """算能买几手。

    从最大可能手数往下试,直到『股票金额 + 全部费用』放得进预算。
    之所以不用除法一次算完,是因为最低佣金是阶梯式的,除法会算错一块钱。
    """
    if price <= 0 or cash_available <= 0:
        return 0, 0.0, 0.0
    budget = min(cash_available, cfg.capital * cfg.risk.max_position_pct)
    max_lots = int(budget // (price * 100)) + 1
    for lots in range(max_lots, 0, -1):
        shares = lots * 100
        notional = shares * price
        cost = cfg.fees.one_way(notional)
        if notional + cost <= budget + 1e-9:
            return shares, notional, cost
    return 0, 0.0, 0.0


def evaluate_entry(
    cfg: Config,
    state: AccountState,
    code: str,
    name: str,
    price: float,
    atr_value: float,
    in_pool: bool = True,
    t0: bool = True,
) -> RiskDecision:
    """建仓前的全部检查。返回的 allowed=False 就是不允许下单。

    t0=False 表示这是 T+1 标的：今天买入,今天卖不掉,止损位最早明天才能执行。
    这个事实本身就是一个风险,下面会单独拎出来算。
    """
    checks: list[Check] = []
    r = cfg.risk
    f = cfg.fees
    required_win = 0.0
    gap_loss = 0.0
    gap_pct = 0.0

    if price <= 0:
        return RiskDecision(False, [Check("有效报价", BLOCK, "无有效价格")])

    shares, notional, buy_cost = plan_size(cfg, state.cash, price)

    # 1. 买得起吗
    if shares == 0:
        lot_cost = price * 100
        checks.append(Check(
            "可负担性", BLOCK,
            f"1手需 {lot_cost:.2f} 元 + 费用,可用 {state.cash:.2f} 元,买不起",
        ))
    else:
        checks.append(Check(
            "可负担性", OK,
            f"{shares} 份 = {notional:.2f} 元,买入费用 {buy_cost:.2f} 元",
        ))

    # 2. 标的类型
    checks.append(Check(
        "标的范围", OK if in_pool else BLOCK,
        "在候选 ETF 池内" if in_pool else "不在候选池内,拒绝交易非白名单标的",
    ))

    # 3. 单标的仓位上限
    if state.total_capital > 0:
        pos_pct = notional / state.total_capital
        if pos_pct > r.max_position_pct + 1e-9:
            checks.append(Check(
                "单标的仓位上限", BLOCK,
                f"本笔占总资金 {pos_pct*100:.1f}%,超过上限 {r.max_position_pct*100:.0f}%",
            ))
        else:
            checks.append(Check(
                "单标的仓位上限", OK,
                f"本笔占总资金 {pos_pct*100:.1f}%,上限 {r.max_position_pct*100:.0f}%",
            ))

    # 4. 总仓位上限
    if state.total_capital > 0:
        after = (state.total_exposure + notional) / state.total_capital
        if after > r.max_total_exposure_pct + 1e-9:
            checks.append(Check(
                "总仓位上限", BLOCK,
                f"买入后总仓位 {after*100:.1f}%,超过上限 {r.max_total_exposure_pct*100:.0f}%",
            ))
        else:
            checks.append(Check(
                "总仓位上限", OK,
                f"买入后总仓位 {after*100:.1f}%,上限 {r.max_total_exposure_pct*100:.0f}%",
            ))

    # 5. 费用占比 —— 500 元本金下的头号杀手
    if notional > 0:
        rt_cost = f.round_trip(notional)
        fee_ratio = rt_cost / notional
        be = f.breakeven_pct(notional)
        if fee_ratio > r.max_fee_ratio_per_trade + 1e-9:
            extra = ""
            if f.is_min_commission_bound(notional):
                extra = (f" —— 这笔被『最低佣金 {f.min_commission:.0f} 元』卡住,"
                         f"名义费率 {f.commission_rate*10000:.1f}‱ 实际 "
                         f"{f.effective_rate(notional)*10000:.1f}‱。"
                         f"要么换免五券商,要么加大单笔金额。")
            checks.append(Check(
                "费用占比", BLOCK,
                f"往返费用 {rt_cost:.2f} 元 = 本笔的 {fee_ratio*100:.2f}%,"
                f"超过上限 {r.max_fee_ratio_per_trade*100:.2f}%。"
                f"意味着买入后要先涨 {be*100:.2f}% 才回本。" + extra,
            ))
        else:
            checks.append(Check(
                "费用占比", OK,
                f"往返费用 {rt_cost:.2f} 元 = {fee_ratio*100:.2f}%,"
                f"盈亏平衡涨幅 {be*100:.2f}%",
            ))
        if f.is_min_commission_bound(notional):
            checks.append(Check(
                "最低佣金提醒", WARN,
                f"被最低佣金卡住:名义 {f.effective_rate(notional)*10000:.1f}‱,"
                f"实际是它的 {f.min_commission/(notional*f.commission_rate):.1f} 倍",
            ))

    # 6. 风险必须显著大于成本
    #    用『止损距离』代表这笔交易承担的风险,不引入任何胜率假设。
    if notional > 0 and atr_value > 0 and shares > 0:
        risk_per_share = r.stop_loss_atr * atr_value
        total_risk = risk_per_share * shares
        rt_cost = f.round_trip(notional)
        mult = total_risk / rt_cost if rt_cost > 0 else float("inf")
        if mult < r.min_edge_multiple:
            checks.append(Check(
                "风险成本比", BLOCK,
                f"止损距离代表的潜在风险 {total_risk:.2f} 元,只有往返费用 "
                f"{rt_cost:.2f} 元的 {mult:.1f} 倍,低于要求的 {r.min_edge_multiple:.1f} 倍。"
                f"赚的钱还不够付手续费。",
            ))
        else:
            checks.append(Check(
                "风险成本比", OK,
                f"潜在风险 {total_risk:.2f} 元 = 往返费用 {rt_cost:.2f} 元的 {mult:.1f} 倍",
            ))

    # 6b. 打平所需胜率 —— 整个闸门里最锋利的一条
    #     思路:止损和止盈都要付一次往返费用,但费用对『亏』和『赚』是不对称的。
    #     亏损被费用放大,盈利被费用缩小,所以需要的胜率远高于赔率倒数。
    #     这条规则来自一次真实的 AI 复核:模型算出止盈净赚 10.5 元、止损净亏 23.7 元,
    #     因此需要 69% 的胜率才不亏 —— 而原本的规则完全没有覆盖这一点。
    required_win = 0.0
    if notional > 0 and atr_value > 0 and shares > 0:
        rt_cost = f.round_trip(notional)
        loss_at_stop = r.stop_loss_atr * atr_value * shares + rt_cost
        gain_at_target = r.take_profit_atr * atr_value * shares - rt_cost
        if gain_at_target <= 0:
            checks.append(Check(
                "打平所需胜率", BLOCK,
                f"止盈价上的毛利 {r.take_profit_atr * atr_value * shares:.2f} 元还不够付 "
                f"{rt_cost:.2f} 元的往返费用。这笔交易在最理想的止盈情形下也是亏的。",
            ))
        else:
            required_win = loss_at_stop / (loss_at_stop + gain_at_target)
            detail = (f"止损净亏 {loss_at_stop:.2f} 元、止盈净赚 {gain_at_target:.2f} 元，"
                      f"需要 {required_win*100:.0f}% 的胜率才不亏本")
            if required_win > r.max_required_win_rate:
                checks.append(Check(
                    "打平所需胜率", BLOCK,
                    detail + f"，高于上限 {r.max_required_win_rate*100:.0f}%。"
                    f"要么把止盈放远一点，要么这笔交易根本不值得做。",
                ))
            else:
                checks.append(Check("打平所需胜率", OK, detail))

    # 7. 交易频次
    if state.month_round_trips >= r.max_trades_per_month:
        checks.append(Check(
            "月度交易频次", BLOCK,
            f"本月已完成 {state.month_round_trips} 次往返,达到上限 {r.max_trades_per_month} 次。"
            f"小资金高频交易等于给券商打工。",
        ))
    else:
        checks.append(Check(
            "月度交易频次", OK,
            f"本月 {state.month_round_trips}/{r.max_trades_per_month} 次",
        ))

    # 8. 当日熔断
    if state.total_capital > 0:
        day_pnl_pct = state.today_realized_pnl / state.total_capital
        if day_pnl_pct <= r.daily_stop_loss_pct:
            checks.append(Check(
                "当日亏损熔断", BLOCK,
                f"今日已实现亏损 {state.today_realized_pnl:.2f} 元 "
                f"({day_pnl_pct*100:.2f}%),触发 {r.daily_stop_loss_pct*100:.1f}% 熔断,今日停止开新仓",
            ))
        else:
            checks.append(Check(
                "当日亏损熔断", OK, f"今日盈亏 {day_pnl_pct*100:+.2f}%",
            ))

    # 9. 回撤熔断
    peak = state.peak_equity or state.total_capital
    if peak > 0:
        dd = (state.equity - peak) / peak
        if dd <= r.max_drawdown_pct:
            checks.append(Check(
                "总回撤熔断", BLOCK,
                f"当前权益 {state.equity:.2f} 从峰值 {peak:.2f} 回撤 {dd*100:.2f}%,"
                f"触发 {r.max_drawdown_pct*100:.0f}% 熔断。停手复盘,不要再交易。",
            ))
        else:
            checks.append(Check("总回撤熔断", OK, f"当前回撤 {dd*100:.2f}%"))

    # 10. 止损后的冷静期
    if state.days_since_stop < r.cooldown_days_after_stop:
        checks.append(Check(
            "止损冷静期", BLOCK,
            f"距上次止损 {state.days_since_stop} 天,冷静期 {r.cooldown_days_after_stop} 天未满",
        ))
    else:
        checks.append(Check("止损冷静期", OK, "无限制"))

    # 11. T+1 的隔夜跳空风险
    #     这条来自一次真实的 AI 复核意见:T+1 标的当天买入后卖不掉,
    #     止损位在隔夜跳空面前是无效的,实际亏损可以远超计划亏损。
    #     这里用 1 倍 ATR 的跳空做估计 —— 不是预测,是给用户看量级。
    if notional > 0 and atr_value > 0 and shares > 0:
        if t0:
            checks.append(Check("T+0 当天可止损", OK,
                                "该标的当天可卖,止损位理论上当天就能执行"))
        else:
            gap_loss = (atr_value * shares) + f.round_trip(notional)
            gap_pct = gap_loss / state.total_capital if state.total_capital else 0.0
            checks.append(Check(
                "T+1 隔夜跳空风险", WARN,
                f"这是 T+1 标的,今天买入今天卖不掉,止损最早明天执行。"
                f"若明天开盘直接跳空 1 倍 ATR（{atr_value:.3f} 元）,"
                f"亏损约 {gap_loss:.2f} 元 = 本金的 {gap_pct*100:.1f}%,"
                f"是计划亏损的 {gap_loss / (r.stop_loss_atr * atr_value * shares):.1f} 倍。"
                f"想当天能跑就选跨境/黄金类 T+0 标的。",
            ))

    allowed = all(c.passed for c in checks)
    rt = f.round_trip(notional) if notional > 0 else 0.0
    # 即使被拦截,也把算出来的股数和金额原样返回。
    # shares=0 会让工单变成一张没有内容的纸,而拦截的理由恰恰需要用这些数字来解释。
    # 真正的『能不能下单』只由 allowed 决定。
    return RiskDecision(
        allowed=allowed,
        checks=checks,
        shares=shares,
        notional=notional,
        est_cost=(buy_cost + (rt - buy_cost)) if notional > 0 else 0.0,
        breakeven_pct=(rt / notional) if notional > 0 else 0.0,
        required_win_rate=required_win,
        gap_loss=gap_loss,
        gap_loss_pct=gap_pct,
    )


def max_viable_notional(cfg: Config) -> float:
    """满足费用占比上限的最小单笔金额。

    低于这个金额,单笔费用占比必然超标 —— 也就是说这个账户根本不该交易,
    或者必须换一家免五的券商。这个函数是给『费用体检』报告用的。
    """
    f = cfg.fees
    if f.min_commission <= 0:
        return 0.0
    # 2*max(N*r, m) / N <= limit  →  若被最低佣金卡住, N >= 2m/limit
    return 2.0 * f.min_commission / cfg.risk.max_fee_ratio_per_trade


def required_capital_for_free_trading(cfg: Config) -> float:
    """在这个费率下,单笔不被最低佣金卡住所需的最小金额。"""
    if cfg.fees.commission_rate <= 0:
        return float("inf")
    return cfg.fees.min_commission / cfg.fees.commission_rate
