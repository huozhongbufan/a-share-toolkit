"""长期定投计划模拟器 —— 用真实约束重算。

为什么需要它
------------
一份"每月投 X 元，年化 Y%，N 年后市值 Z"的计划，最容易漏掉两件事：

  1. **整手约束。** A 股 100 股起买，不能买 164 股。
     月投 ¥100 想买 ¥7.49 的农业银行，得攒 8 个月才够 1 手。
     这期间钱躺在账上，不产生任何收益。
  2. **手续费。** 每次买入都有一次佣金，¥5 最低的情况下，
     买 1 手 ¥173 的郑州银行，佣金占 **2.89%**。

两件事叠加，实际结果往往只有计划的一半。这个模块把两者都算进去，
并给出「原计划 vs 实际 vs 免五」三方对照。

沿用计划本身的两个假设，以保证可比
----------------------------------
  · 股价不变（只看股息复利，不赌价差）
  · 分红每年一次，按年末股数 × 每股分红，全部再投入
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .config import Config

LOT = 100          # A 股 1 手 = 100 股


@dataclass
class YearRow:
    idx: int                  # 第几年（1 起）
    age: int
    monthly: float            # 当年月投
    cash_in: float            # 当年投入现金
    lots: int                 # 当年买入手数
    shares: int               # 年末持股数
    fees: float               # 当年手续费
    dividend: float           # 当年分红
    cash_idle: float          # 年末闲置现金（攒不够一手）
    value: float              # 年末市值
    cum_cash: float           # 累计投入
    cum_fees: float           # 累计手续费
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlanResult:
    code: str
    name: str
    price: float
    lot_cost: float
    dividend_yield: float
    min_commission: float
    start_age: int
    rows: list[YearRow] = field(default_factory=list)
    total_cash: float = 0.0
    total_fees: float = 0.0
    final_shares: int = 0
    final_value: float = 0.0
    final_cash_idle: float = 0.0
    final_dividend_year: float = 0.0
    avg_cost: float = 0.0
    ideal_value: float = 0.0     # 若不考虑整手与费用，按复利算出的市值
    gap_pct: float = 0.0         # 实际 / 理想 的差距
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rows"] = [r.to_dict() for r in self.rows]
        return d


def _fee(cfg: Config, notional: float, min_comm: float | None = None) -> float:
    """买入单边费用。定投只买不卖，所以不算卖出那一半。"""
    if notional <= 0:
        return 0.0
    rate = cfg.fees.commission_rate
    floor = cfg.fees.min_commission if min_comm is None else min_comm
    comm = max(notional * rate, floor)
    slip = notional * (cfg.fees.slippage_bps / 10000.0)
    return comm + slip


def _buy(cfg: Config, cash: float, shares: int, price: float,
         floor: float) -> tuple[float, int, float, int]:
    """能买就一次买到位，返回 (剩余现金, 新持股数, 本次费用, 本次手数)。

    关键：**一次委托只收一次佣金。** 买 5 手 ¥173 的郑州银行是一次 ¥865 的
    委托，佣金按 ¥865 算一次，不是收 5 次 ¥5。早期版本按每手循环收费，
    把郑州银行的手续费多算了 62 倍。
    """
    lot_cost = price * LOT
    if lot_cost <= 0 or cash < lot_cost:
        return cash, shares, 0.0, 0
    n = int(cash // lot_cost)
    # 把佣金算进去后再往回收，直到 notional + fee <= cash
    while n > 0:
        notional = n * lot_cost
        f = _fee(cfg, notional, floor)
        if notional + f <= cash:
            return cash - notional - f, shares + n * LOT, f, n
        n -= 1
    return cash, shares, 0.0, 0


def simulate(cfg: Config, *, code: str, name: str, price: float,
             monthly_by_year: list[float], start_age: int = 18,
             dividend_yield: float = 0.05, min_commission: float | None = None,
             label: str = "") -> PlanResult:
    """按月模拟定投。

    每月初把月投金额打进账户，只要现金够 1 手就立刻买（一次买到位）。
    每年末结算一次分红，分红现金留在账户里继续参与买入。
    """
    if price <= 0:
        raise ValueError("price 必须为正")
    lot_cost = price * LOT
    floor = cfg.fees.min_commission if min_commission is None else min_commission

    res = PlanResult(
        code=code, name=name, price=round(price, 3),
        lot_cost=round(lot_cost, 2), dividend_yield=dividend_yield,
        min_commission=floor, start_age=start_age,
    )

    cash = 0.0
    shares = 0
    cum_cash = 0.0
    cum_fees = 0.0

    for yi, monthly in enumerate(monthly_by_year):
        year_fees = 0.0
        year_lots = 0
        year_div = 0.0
        year_cash_in = monthly * 12

        for _ in range(12):
            cash += monthly
            cum_cash += monthly
            cash, shares, f, n = _buy(cfg, cash, shares, price, floor)
            year_fees += f
            year_lots += n
            cum_fees += f

        # 年末分红，全部再投入
        if dividend_yield > 0 and shares > 0:
            year_div = shares * price * dividend_yield
            cash += year_div
            cash, shares, f, n = _buy(cfg, cash, shares, price, floor)
            year_fees += f
            year_lots += n
            cum_fees += f

        value = shares * price
        note = ""
        if year_lots == 0:
            note = f"这一年攒不够 1 手（需 ¥{lot_cost + _fee(cfg, lot_cost, floor):.0f}）"
        res.rows.append(YearRow(
            idx=yi + 1, age=start_age + yi, monthly=monthly,
            cash_in=round(year_cash_in, 2), lots=year_lots, shares=shares,
            fees=round(year_fees, 2), dividend=round(year_div, 2),
            cash_idle=round(cash, 2), value=round(value, 2),
            cum_cash=round(cum_cash, 2), cum_fees=round(cum_fees, 2),
            note=note,
        ))

    res.total_cash = round(cum_cash, 2)
    res.total_fees = round(cum_fees, 2)
    res.final_shares = shares
    res.final_value = round(shares * price, 2)
    res.final_cash_idle = round(cash, 2)
    res.final_dividend_year = round(shares * price * dividend_yield, 2)
    res.avg_cost = round(cum_cash / shares, 4) if shares else 0.0

    # 理想值：不考虑整手和费用，纯复利（这就是大多数计划表用的算法）
    ideal = 0.0
    for monthly in monthly_by_year:
        for _ in range(12):
            ideal = ideal * (1 + dividend_yield / 12) + monthly
    res.ideal_value = round(ideal, 2)
    res.gap_pct = round(res.final_value / ideal - 1, 4) if ideal else 0.0

    if floor > 0 and lot_cost < lot_cost * 40:
        res.notes.append(
            f"最低佣金 ¥{floor:.0f} 会明显吃掉小额定投："
            f"买 1 手 ¥{lot_cost:.0f} 的佣金占 {_fee(cfg, lot_cost, floor)/lot_cost*100:.2f}%")
    if res.rows and res.rows[0].lots == 0:
        res.notes.append(
            f"第 1 年 1 手都买不到：月投 ¥{monthly_by_year[0]:.0f} × 12 = "
            f"¥{monthly_by_year[0]*12:.0f}，而 1 手要 ¥{lot_cost:.0f}")
    if label:
        res.notes.insert(0, label)
    return res


def compare(cfg: Config, *, code: str, name: str, price: float,
            monthly_by_year: list[float], start_age: int = 18,
            dividend_yield: float = 0.05) -> dict:
    """三方对照：现费率 / 免五 / 理想复利。"""
    real = simulate(cfg, code=code, name=name, price=price,
                    monthly_by_year=monthly_by_year, start_age=start_age,
                    dividend_yield=dividend_yield)
    free = simulate(cfg, code=code, name=name, price=price,
                    monthly_by_year=monthly_by_year, start_age=start_age,
                    dividend_yield=dividend_yield, min_commission=0.0)
    return {"real": real, "free5": free,
            "ideal": real.ideal_value,
            "min_lot_monthly": round(price * LOT, 2)}


def affordable_banks(cfg: Config, quotes: dict, uni: dict,
                     max_lot: float) -> list[dict]:
    """按 1 手成本筛出「月投档次买得起」的银行股。"""
    out = []
    for code, q in quotes.items():
        inst = uni.get(code)
        if not inst or inst.kind != "股票" or "银行" not in inst.name:
            continue
        lot = q.price * LOT
        if lot <= max_lot and q.amount_wan >= 3000:
            out.append({"code": code, "name": q.name, "price": round(q.price, 3),
                        "lot_cost": round(lot, 2),
                        "fee_pct": round(_fee(cfg, lot) / lot, 4)})
    out.sort(key=lambda r: r["lot_cost"])
    return out
