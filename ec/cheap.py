"""低价多股筛选与持仓规划。

用途：用户只想看**单价 ¥5 以下**的个股，分散持有几只，目标收益很小
（"每天一杯饮料"级别）。

这个模块的第一职责不是「找出能买的票」，是**把费用数学摆出来**。

先看这组数字（¥5 最低佣金，A 股个股含印花税 0.05% 单边 + 过户费 0.001% 双边）：

    股价 ¥2.0  1手=¥200  往返 ¥10.10 = 5.05%
    股价 ¥3.0  1手=¥300  往返 ¥10.16 = 3.39%
    股价 ¥5.0  1手=¥500  往返 ¥10.26 = 2.05%

A 股日均波动 1~2%。**买一只 ¥2 的股票，当天就已经先亏掉两天的波动。**

低价股 + 最低佣金是最坏的组合：单笔金额小，固定佣金占比被放到最大。
所以这里的每个结论都会把费用算进去，而不是只给一个股票列表。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime

from .config import Config
from . import ths
from .quotes import Quote, fetch_quotes

# 一只股票的价格档位。低于 1.5 元要特别小心面值退市。
_FACE_DELIST = 1.5


@dataclass
class CheapStock:
    code: str
    name: str
    price: float
    lot_cost: float           # 1 手 = 100 股的成本
    pct: float = 0.0
    amount_wan: float = 0.0
    kind: str = "股票"
    round_trip_cost: float = 0.0
    round_trip_pct: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FeeReport:
    """把「目标收益 vs 手续费」摊开算清楚。"""
    capital: float
    n_positions: int
    lot_cost_each: float
    round_trip_each: float
    round_trip_pct: float
    fee_per_round: float
    target_daily: float
    target_monthly: float
    monthly_return_needed: float
    annual_return_needed: float
    max_rounds_per_month: float
    verdict: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 全市场快照

def _cache_path(cfg: Config) -> str:
    return os.path.join(cfg.data_dir, "quotes_all.json")


def _load_cache(cfg: Config, max_age_sec: int = 6 * 3600) -> dict[str, dict] | None:
    p = _cache_path(cfg)
    if not os.path.exists(p):
        return None
    if time.time() - os.path.getmtime(p) > max_age_sec:
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(cfg: Config, rows: dict[str, dict]) -> None:
    try:
        os.makedirs(cfg.data_dir, exist_ok=True)
        with open(_cache_path(cfg), "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False)
    except OSError:
        pass


def fetch_all_quotes(cfg: Config, force: bool = False,
                     verbose=None) -> dict[str, Quote]:
    """抓全市场快照（约 7700 只，腾讯批量 60/请求 ≈ 130 次）。

    一天缓存一次。全市场扫描一天只需要一回，没必要每次都打 130 个请求。
    """
    say = verbose or (lambda *_: None)
    if not force:
        cached = _load_cache(cfg)
        if cached:
            say(f"  用当天缓存的快照（{len(cached)} 只）")
            return {k: Quote(**v) for k, v in cached.items() if _quote_fields_ok(v)}

    uni = ths.tradable_universe()
    codes = list(uni)
    say(f"  本地代码表 {len(codes)} 只，开始批量抓行情…")

    out: dict[str, Quote] = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        try:
            got = fetch_quotes(batch)
            out.update(got)
        except Exception as exc:
            say(f"  第 {i//60+1} 批失败：{exc}")
        if (i // 60) % 20 == 19:
            say(f"  …{len(out)} 只")
    if out:
        _save_cache(cfg, {k: _quote_to_dict(v) for k, v in out.items()})
    say(f"  完成，{len(out)} 只")
    return out


def _quote_fields_ok(d: dict) -> bool:
    need = {"code", "name", "price", "prev_close", "open", "high", "low",
            "volume_shou", "amount_wan"}
    return need.issubset(d.keys())


def _quote_to_dict(q: Quote) -> dict:
    return {
        "code": q.code, "name": q.name, "price": q.price,
        "prev_close": q.prev_close, "open": q.open, "high": q.high,
        "low": q.low, "volume_shou": q.volume_shou, "amount_wan": q.amount_wan,
        "float_mv_yi": q.float_mv_yi, "bid1": q.bid1, "ask1": q.ask1,
        "ts": q.ts, "source": q.source,
    }


# ---------------------------------------------------------------- 筛选

def screen(cfg: Config, max_price: float = 5.0,
           min_amount_wan: float = 5000.0,
           include_st: bool = False,
           kinds: tuple[str, ...] | None = None,
           quotes: dict[str, Quote] | None = None,
           verbose=None) -> list[CheapStock]:
    """筛出单价 ≤ max_price 的标的。

    kinds 可以只取某几类，例如 `kinds=("ETF", "LOF", "基金")` 只看场内基金。
    不传就是全市场。

    排除：
      · ST / *ST / 退市整理 / 名称带「退」
      · 面值退市风险（价格 < 1.5 元，仅对个股有意义）
      · 成交额太低（默认 < 5000 万，买得进卖不出）
      · 无有效报价
    """
    say = verbose or (lambda *_: None)
    q = quotes if quotes is not None else fetch_all_quotes(cfg, verbose=verbose)

    try:
        bad_codes = set(ths.risk_excluded())
        uni = ths.tradable_universe()
    except Exception:
        bad_codes = set()
        uni = {}

    out: list[CheapStock] = []
    for code, quote in q.items():
        if quote.price <= 0 or quote.price > max_price:
            continue
        if quote.amount_wan < min_amount_wan:
            continue
        inst = uni.get(code)
        kind = inst.kind if inst else "未知"
        if kinds and kind not in kinds:
            continue
        is_stock = kind == "股票"
        if not include_st and is_stock:
            if code in bad_codes:
                continue
            nm = quote.name
            if "ST" in nm.upper() or nm.startswith("*") or "退" in nm:
                continue

        lot = quote.price * 100.0
        rt = cfg.fees.round_trip(lot)
        item = CheapStock(
            code=code, name=quote.name, price=round(quote.price, 3),
            lot_cost=round(lot, 2), pct=round(quote.pct * 100, 2),
            amount_wan=round(quote.amount_wan, 0), kind=kind,
            round_trip_cost=round(rt, 2),
            round_trip_pct=round(rt / lot, 6) if lot else 0.0,
        )
        if is_stock and quote.price < _FACE_DELIST:
            item.warnings.append(f"价格 {quote.price:.2f} 低于 {_FACE_DELIST}，有面值退市风险")
        if item.round_trip_pct > 0.03:
            item.warnings.append(
                f"往返成本 {item.round_trip_pct*100:.2f}%，"
                f"比 A 股日均波动（1~2%）还高")
        out.append(item)

    # 按往返成本占比排序 —— 便宜的不是股价低的，是成本占比低的
    out.sort(key=lambda x: x.round_trip_pct)
    say(f"  单价 ≤ ¥{max_price} 且流动性达标：{len(out)} 只")
    return out


def price_band_table(cfg: Config, quotes: dict[str, Quote],
                     kind: str = "ETF") -> list[dict]:
    """按价格档位统计往返成本占比，回答「哪个价位的手续费最划算」。

    这一栏是反直觉的：**单价越低，手续费占比越高。**
    ¥0.4 的 ETF 一手才 40 元，往返固定 10 元 = 25%，
    而 ¥4.9 的 ETF 一手 490 元，往返还是 10 元 = 2%。
    同样的固定佣金，摊在越小的金额上越致命。
    """
    bands = [(0.3, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 3.0),
             (3.0, 4.0), (4.0, 5.0), (5.0, 100.0)]
    try:
        uni = ths.tradable_universe()
    except Exception:
        uni = {}

    rows = []
    for lo, hi in bands:
        members = []
        for code, q in quotes.items():
            if not (lo <= q.price < hi):
                continue
            inst = uni.get(code)
            if inst and inst.kind != kind:
                continue
            if inst is None and kind != "ETF":
                continue
            if q.amount_wan < 3000:
                continue
            members.append(q)
        if not members:
            continue
        mid = sum(m.price for m in members) / len(members)
        lot = mid * 100
        rt = cfg.fees.round_trip(lot)
        rows.append({
            "band": f"¥{lo:.1f}~{hi:.1f}" if hi < 100 else f"¥{lo:.1f}+",
            "n": len(members),
            "avg_price": round(mid, 3),
            "lot_cost": round(lot, 0),
            "round_trip": round(rt, 2),
            "round_trip_pct": round(rt / lot, 4) if lot else None,
        })
    return rows


# ---------------------------------------------------------------- 费用测算

def fee_report(cfg: Config, capital: float, n_positions: int,
               lot_cost: float, target_daily: float) -> FeeReport:
    """「每天赚 X」在当前费率下成立吗。"""
    rt_each = cfg.fees.round_trip(lot_cost)
    fee_round = rt_each * n_positions
    target_m = target_daily * 22
    monthly_need = target_m / capital if capital else 0
    annual_need = ((1 + monthly_need) ** 12 - 1) if monthly_need > -1 else 0

    rep = FeeReport(
        capital=capital, n_positions=n_positions,
        lot_cost_each=round(lot_cost, 2),
        round_trip_each=round(rt_each, 2),
        round_trip_pct=round(rt_each / lot_cost, 6) if lot_cost else 0,
        fee_per_round=round(fee_round, 2),
        target_daily=target_daily,
        target_monthly=round(target_m, 2),
        monthly_return_needed=round(monthly_need, 6),
        annual_return_needed=round(annual_need, 6),
        max_rounds_per_month=round(target_m / fee_round, 1) if fee_round else 0,
    )

    if annual_need > 0.30:
        rep.verdict = (f"不成立。要按这个目标做，年化需要 {annual_need*100:.0f}%，"
                       f"A 股长期年化只有 6~10%。")
        rep.notes.append(f"本金加到约 {target_m / 0.11 * 12:.0f} 元，"
                         f"年化要求才降到 11% 左右的合理区间")
    elif annual_need > 0.15:
        rep.verdict = f"偏激进。年化需要 {annual_need*100:.0f}%，明显高于市场长期回报。"
    else:
        rep.verdict = f"目标本身合理（年化 {annual_need*100:.0f}%）。"

    if rep.max_rounds_per_month < 2:
        rep.notes.append(
            f"每月最多能调 {rep.max_rounds_per_month:.1f} 轮仓，"
            f"否则手续费吃光收益。这决定了策略必须极低频。")
    if rep.round_trip_pct > 0.02:
        rep.notes.append(
            f"单只往返成本 {rep.round_trip_pct*100:.2f}% 太高。"
            f"根因是最低佣金 {cfg.fees.min_commission:.0f} 元卡在小额单上 —— "
            f"换成免五券商，这一项会降到 0.1% 级别。")
    return rep


def plan_positions(cfg: Config, capital: float, lot_cost: float,
                   max_positions: int = 8,
                   reserve_pct: float = 0.05) -> dict:
    """给定本金和单只 1 手的成本，能持几只 + 手续费影响。"""
    usable = capital * (1 - reserve_pct)
    per = lot_cost + cfg.fees.one_way(lot_cost)
    n = int(usable // per) if per > 0 else 0
    n = min(n, max_positions)
    rt = cfg.fees.round_trip(lot_cost)
    return {
        "capital": round(capital, 2),
        "usable": round(usable, 2),
        "lot_cost": round(lot_cost, 2),
        "can_hold": n,
        "deployed": round(n * per, 2),
        "cash_left": round(capital - n * per, 2),
        "fee_one_round": round(rt * n, 2),
        "fee_pct_of_capital": round(rt * n / capital, 6) if capital else 0,
    }
