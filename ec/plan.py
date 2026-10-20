"""定投计划与『一次性买入 vs 分批买入』的历史对照。

这个模块存在的原因是一条回测结论:
    在宽基 ETF 上,择时策略并没有稳定跑赢买入持有,而费用是确定的支出。
    既然方向判断赚不到钱,那理性的做法就是把不确定性交给时间,把确定性
    (费用)压到最低。

但 500 元 + 1 手 = 100 份的最小交易单位组合起来有个硬约束:
    很多 ETF 一手就要 400 多元,根本没有『每月买一点』的空间。
所以计划必须按手数算,而不是按金额算 —— 这跟大部分定投计算器不一样。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .config import Config
from .quotes import Bar


@dataclass
class Installment:
    seq: int
    label: str              # 例如 "第3个月"
    cash_available: float   # 本期累计可用资金
    lots: int
    shares: int
    price: float
    notional: float
    fee: float
    fee_ratio: float
    breakeven_pct: float
    cash_after: float
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    code: str
    name: str
    price: float
    lot_cost: float
    months: int
    monthly: float
    initial: float
    total_invested: float
    total_shares: int
    total_fee: float
    fee_pct_of_invested: float
    avg_cost: float
    installments: list[Installment] = field(default_factory=list)
    verdict: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["installments"] = [i.to_dict() for i in self.installments]
        return d


def build_plan(cfg: Config, code: str, name: str, price: float,
               monthly: float, months: int = 12,
               initial: float | None = None) -> Plan:
    """按手数排定投计划。

    每期先看『累计可用资金够不够买 1 手』,不够就继续攒到下期。
    这样不会出现『计划买了 62 份』这种在下单页面根本填不进去的数量。
    """
    init = cfg.capital if initial is None else initial
    lot_cost = price * 100.0

    plan = Plan(
        code=code, name=name, price=price, lot_cost=lot_cost,
        months=months, monthly=monthly, initial=init,
        total_invested=0.0, total_shares=0, total_fee=0.0,
        fee_pct_of_invested=0.0, avg_cost=0.0,
    )

    cash = init
    for m in range(1, months + 1):
        if m > 1:
            cash += monthly
        lots = int(cash // (lot_cost * (1 + cfg.fees.commission_rate)))
        # 把最低佣金也算进去,否则会出现差几块钱买不起的情况
        while lots > 0 and lots * lot_cost + cfg.fees.one_way(lots * lot_cost) > cash:
            lots -= 1
        if lots <= 0:
            plan.installments.append(Installment(
                seq=m, label=f"第{m}期", cash_available=round(cash, 2),
                lots=0, shares=0, price=price, notional=0.0, fee=0.0,
                fee_ratio=0.0, breakeven_pct=0.0, cash_after=round(cash, 2),
                note=f"资金不足 1 手（需 {lot_cost:.2f} 元），继续攒",
            ))
            continue

        shares = lots * 100
        notional = shares * price
        fee = cfg.fees.one_way(notional)
        cash -= notional + fee
        plan.total_invested += notional
        plan.total_shares += shares
        plan.total_fee += fee
        plan.installments.append(Installment(
            seq=m, label=f"第{m}期", cash_available=round(cash + notional + fee, 2),
            lots=lots, shares=shares, price=price, notional=round(notional, 2),
            fee=round(fee, 2), fee_ratio=round(fee / notional, 6),
            breakeven_pct=round(cfg.fees.breakeven_pct(notional), 6),
            cash_after=round(cash, 2),
        ))

    if plan.total_shares:
        plan.avg_cost = (plan.total_invested + plan.total_fee) / plan.total_shares
        plan.fee_pct_of_invested = plan.total_fee / plan.total_invested

    plan.total_invested = round(plan.total_invested, 2)
    plan.total_fee = round(plan.total_fee, 2)
    plan.avg_cost = round(plan.avg_cost, 4)

    # 结论
    if not plan.total_shares:
        plan.verdict = (f"本金加每期投入凑不出 1 手（{code} 一手要 {lot_cost:.2f} 元）。"
                        f"要么换更便宜的标的，要么把投入周期拉长。")
        plan.warnings.append("这个组合下无法执行定投。")
    else:
        ratio = plan.fee_pct_of_invested
        if ratio > cfg.risk.max_fee_ratio_per_trade:
            plan.verdict = (f"可行，但费用占比 {ratio*100:.2f}% 偏高。"
                            f"主要成本仍是最低佣金，换免五券商会显著改善。")
            plan.warnings.append(
                f"累计费用 {plan.total_fee:.2f} 元，占投入的 {ratio*100:.2f}%。")
        else:
            plan.verdict = (f"可行。累计费用占投入 {ratio*100:.2f}%，"
                            f"低于 {cfg.risk.max_fee_ratio_per_trade*100:.2f}% 的上限。")
        if lot_cost > init:
            plan.warnings.append(
                f"第一期资金 {init:.0f} 元买不起 1 手（需 {lot_cost:.2f} 元），"
                f"要攒到第 "
                f"{next((i.seq for i in plan.installments if i.lots > 0), months)} 期才能动手。")
    return plan


# ---------------------------------------------------------------- 历史对照

@dataclass
class DcaComparison:
    code: str
    name: str
    start: str
    end: str
    months: int
    total_capital: float
    lump_shares: int
    lump_cost: float
    lump_fee: float
    lump_avg: float
    dca_shares: int
    dca_cost: float
    dca_fee: float
    dca_avg: float
    final_price: float
    lump_value: float
    dca_value: float
    lump_return: float
    dca_return: float
    winner: str
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def historical_dca_vs_lump(cfg: Config, code: str, name: str, bars: list[Bar],
                           total_capital: float, tranches: int = 6) -> DcaComparison | None:
    """同一笔钱,一次性买入 vs 分 N 次买入。

    用真实历史日线做:把能取的区间等分成 N 段,每段在第一个交易日投入一份。
    每次都按整手成交,费用按 config 里的真实费率算。
    """
    if len(bars) < tranches * 5 + 5:
        return None

    per = total_capital / tranches
    step = len(bars) // tranches

    # 一次性:第一天全买
    def buy_all(cash: float, px: float) -> tuple[int, float]:
        lots = int(cash // (px * 100))
        while lots > 0:
            n = lots * 100
            if n * px + cfg.fees.one_way(n * px) <= cash:
                return n, n * px
            lots -= 1
        return 0, 0.0

    px0 = bars[0].close
    lump_shares, lump_notional = buy_all(total_capital, px0)
    if lump_shares == 0:
        return None
    lump_fee = cfg.fees.one_way(lump_notional)

    # 分批:每段开头投入一份,不足 1 手就累积到下一段
    dca_shares = 0
    dca_notional = 0.0
    dca_fee = 0.0
    pending = 0.0
    for k in range(tranches):
        idx = min(k * step, len(bars) - 1)
        pending += per
        px = bars[idx].close
        n, notional = buy_all(pending, px)
        if n > 0:
            fee = cfg.fees.one_way(notional)
            pending -= notional + fee
            dca_shares += n
            dca_notional += notional
            dca_fee += fee

    if dca_shares == 0:
        return None

    final = bars[-1].close
    lump_val = lump_shares * final
    dca_val = dca_shares * final
    # 两边都还没卖,所以这里的收益不含卖出费用 —— 卖出费用对两边是同量级的
    lump_ret = lump_val / (lump_notional + lump_fee) - 1.0
    dca_ret = dca_val / (dca_notional + dca_fee) - 1.0

    note = ""
    diff = abs(lump_ret - dca_ret)
    if diff < 0.01:
        note = "两者差距不到 1 个百分点,说明在这段行情里分批还是梭哈基本无所谓。"
    elif lump_ret > dca_ret:
        note = ("这段行情是先跌后涨/一路上涨,一次性买入占了便宜。"
                "但这是事后才知道的 —— 换一段先涨后跌的行情结论会反过来。")
    else:
        note = ("这段行情中途有下跌,分批买入摊低了成本。"
                "同样地,换一段单边上涨的行情结论会反过来。")

    return DcaComparison(
        code=code, name=name,
        start=bars[0].date, end=bars[-1].date, months=tranches,
        total_capital=round(total_capital, 2),
        lump_shares=lump_shares, lump_cost=round(lump_notional, 2),
        lump_fee=round(lump_fee, 2),
        lump_avg=round((lump_notional + lump_fee) / lump_shares, 4),
        dca_shares=dca_shares, dca_cost=round(dca_notional, 2),
        dca_fee=round(dca_fee, 2),
        dca_avg=round((dca_notional + dca_fee) / dca_shares, 4),
        final_price=round(final, 4),
        lump_value=round(lump_val, 2), dca_value=round(dca_val, 2),
        lump_return=round(lump_ret, 6), dca_return=round(dca_ret, 6),
        winner="一次性买入" if lump_ret > dca_ret else "分批买入",
        note=note,
    )


def render_plan_markdown(plan: Plan, comps: list[DcaComparison], cfg: Config) -> str:
    L: list[str] = []
    L.append(f"# 定投计划：{plan.code[2:]} {plan.name}")
    L.append("")
    L.append(f"初始本金 {plan.initial:.0f} 元 ｜ 每期追加 {plan.monthly:.0f} 元 ｜ "
             f"共 {plan.months} 期")
    L.append(f"当前价 {plan.price:.3f} 元 ｜ 1 手 = {plan.lot_cost:.2f} 元 ｜ "
             f"最低佣金 {cfg.fees.min_commission:.0f} 元")
    L.append("")
    L.append(f"**结论：{plan.verdict}**")
    L.append("")
    if plan.warnings:
        for w in plan.warnings:
            L.append(f"- ⚠️ {w}")
        L.append("")
    L.append("## 执行表")
    L.append("")
    L.append("| 期数 | 可用资金 | 买入手数 | 份数 | 金额 | 费用 | 费用占比 | 盈亏平衡 | 剩余现金 | 备注 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for i in plan.installments:
        L.append(f"| {i.label} | {i.cash_available:.2f} | {i.lots} | {i.shares} | "
                 f"{i.notional:.2f} | {i.fee:.2f} | "
                 f"{i.fee_ratio*100:.2f}% | {i.breakeven_pct*100:.3f}% | "
                 f"{i.cash_after:.2f} | {i.note} |")
    L.append("")
    L.append(f"合计投入 {plan.total_invested:.2f} 元，累计 {plan.total_shares} 份，"
             f"手续费 {plan.total_fee:.2f} 元（占投入 {plan.fee_pct_of_invested*100:.2f}%），"
             f"摊薄成本 {plan.avg_cost:.4f} 元/份。")
    L.append("")

    if comps:
        L.append("## 历史对照：一次性买入 vs 分批买入")
        L.append("")
        L.append("同一笔钱，在真实历史行情里的两种花法：")
        L.append("")
        L.append("| 标的 | 区间 | 一次性份数 | 一次性成本 | 分批份数 | 分批成本 | "
                 "一次性收益 | 分批收益 | 胜出 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for c in comps:
            L.append(f"| {c.code[2:]} {c.name} | {c.start[2:]}~{c.end[2:]} | "
                     f"{c.lump_shares} | {c.lump_avg:.4f} | {c.dca_shares} | "
                     f"{c.dca_avg:.4f} | {c.lump_return*100:+.2f}% | "
                     f"{c.dca_return*100:+.2f}% | {c.winner} |")
        L.append("")
        for c in comps:
            L.append(f"- **{c.code[2:]} {c.name}**：{c.note}")
        L.append("")
        L.append("> 这个对照只能说明『过去这段行情里哪种花法更划算』，"
                 "不能用来预测未来。它的价值在于让你知道："
                 "分批的意义是降低『买在最高点』的后悔程度，不是提高收益。")
        L.append("")
    return "\n".join(L)
