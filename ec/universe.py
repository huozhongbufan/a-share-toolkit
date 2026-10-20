"""候选池构建：从精选 ETF 池里筛出『500 元买得起、也值得买』的那一小撮。

三级过滤，顺序不能反：
  1. 买得起   —— 1 手(100份)的钱必须够，否则连下单资格都没有
  2. 排雷     —— 杠杆/分级/货币/债券/商品等一律不要
  3. 流动性   —— 日成交额太小的，买卖价差能吃掉全部利润

数据来源是腾讯的批量行情接口（一次可查 60 只），稳定且带买一卖一。
东财的全市场列表只作为可选的『发现新标的』通道，它挂了不影响主流程。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime

from .config import Config
from .quotes import Quote, fetch_quotes, QuoteError
from . import pool as pool_mod

# 名称启发式：只用于池子外的未知代码。池子内的 T+0 标记是人工确认过的数据。
_CROSS_BORDER_WORDS = (
    "恒生", "恒指", "港股", "H股", "中概", "纳指", "纳斯达克", "标普", "道琼斯",
    "日经", "德国", "法国", "英国", "亚太", "东南亚", "沙特", "美国", "海外",
    "全球", "国际", "中韩", "中日", "越南", "印度", "欧洲",
)
_T0_OTHER_WORDS = ("黄金", "白银", "豆粕", "能源化工", "有色")


def is_t0(code: str, name: str) -> bool:
    """T+0 判断。优先用池子里人工确认过的标记，未知代码才走名称启发式。"""
    entry = pool_mod.lookup(code)
    if entry is not None:
        return entry.t0
    c = code.lower()
    if c.startswith(("sh513", "sh520", "sh511")):
        return True
    return any(w in name for w in _CROSS_BORDER_WORDS) or any(w in name for w in _T0_OTHER_WORDS)


@dataclass
class Candidate:
    code: str
    name: str
    price: float
    lot_cost: float          # 1 手成本 = price * 100
    amount_wan: float
    category: str = ""
    float_mv_yi: float = 0.0
    t0: bool = False
    pct: float = 0.0
    spread_pct: float = 0.0
    reasons: list[str] = field(default_factory=list)   # 被排除的原因

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Universe:
    kept: list[Candidate] = field(default_factory=list)
    dropped: list[Candidate] = field(default_factory=list)
    generated_at: str = ""
    capital: float = 0.0
    max_lot_cost: float = 0.0
    source: str = ""
    quote_failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "capital": self.capital,
            "max_lot_cost": round(self.max_lot_cost, 2),
            "source": self.source,
            "quote_failures": self.quote_failures,
            "kept": [c.to_dict() for c in self.kept],
            "dropped": [c.to_dict() for c in self.dropped],
        }


def build_universe(cfg: Config, force_refresh: bool = False, enrich: bool = True) -> Universe:
    """构建候选池。行情抓不到就整体失败,不猜价格。"""
    max_lot = cfg.max_affordable_lot_cost()
    uni = Universe(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        capital=cfg.capital,
        max_lot_cost=max_lot,
        source="pool+tencent",
    )

    entries = list(pool_mod.POOL)
    if force_refresh:
        extra = _discover(cfg)
        known = {e.code for e in entries}
        entries.extend(e for e in extra if e.code not in known)

    codes = [e.code for e in entries]
    try:
        quotes: dict[str, Quote] = fetch_quotes(codes)
    except (QuoteError, OSError) as exc:
        raise QuoteError(f"行情抓取失败,无法构建候选池: {exc}") from exc

    if not quotes:
        raise QuoteError("行情接口没有返回任何数据。检查网络,或稍后重试。")

    blacklist = {c.lower() for c in cfg.universe.manual_blacklist}
    whitelist = {c.lower() for c in cfg.universe.manual_whitelist}
    min_turnover = cfg.universe.min_avg_turnover_wan

    for entry in entries:
        q = quotes.get(entry.code)
        if q is None:
            uni.quote_failures.append(entry.code)
            continue

        cand = Candidate(
            code=entry.code,
            name=q.name or entry.name,
            price=q.price,
            lot_cost=q.price * 100.0,
            amount_wan=q.amount_wan,
            category=entry.category,
            float_mv_yi=q.float_mv_yi,
            t0=entry.t0,
            pct=q.pct,
            spread_pct=q.spread_pct,
        )

        forced_in = entry.code.lower() in whitelist
        reasons: list[str] = []
        if entry.code.lower() in blacklist:
            reasons.append("手动排除")
        if q.price <= 0:
            reasons.append("无有效报价")
        if not forced_in:
            if cand.lot_cost > max_lot:
                reasons.append(f"买不起1手(需{cand.lot_cost:.0f}元,可用{max_lot:.0f}元)")
            if q.amount_wan < min_turnover:
                reasons.append(f"成交额不足({q.amount_wan:.0f}万<{min_turnover:.0f}万)")
            hit = [kw for kw in cfg.universe.exclude_keywords if kw in cand.name]
            if hit:
                reasons.append("排除品种(" + "/".join(hit) + ")")
            if cfg.universe.only_t0 and not cand.t0:
                reasons.append("非T+0")

        cand.reasons = reasons
        (uni.dropped if reasons else uni.kept).append(cand)

    uni.kept.sort(key=lambda c: c.amount_wan, reverse=True)
    return uni


def _discover(cfg: Config) -> list[pool_mod.PoolEntry]:
    """可选：从东财全市场列表里发现池子外的新 ETF。

    这个接口不稳定（连续请求几次就会被掐断），所以它只是锦上添花：
    成功就把新代码并在本次扫描里，失败就静默返回空列表，不影响主流程。
    """
    from .quotes import fetch_etf_list
    try:
        rows = fetch_etf_list()
    except Exception:
        return []
    known = set(pool_mod.BY_CODE)
    out: list[pool_mod.PoolEntry] = []
    for r in rows:
        if r["code"] in known:
            continue
        if r["price"] * 100 > cfg.max_affordable_lot_cost():
            continue
        out.append(pool_mod.PoolEntry(
            r["code"], r["name"], "industry", is_t0(r["code"], r["name"]),
            note="自动发现",
        ))
    return out


def group_by_category(cands: list[Candidate]) -> dict[str, list[Candidate]]:
    out: dict[str, list[Candidate]] = {}
    for c in cands:
        out.setdefault(c.category, []).append(c)
    return out
