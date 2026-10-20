"""持仓与审计日志。

设计上只认一个事实来源:**append-only 的流水账 journal.jsonl**。
state.json 是从流水账推导出来的缓存,随时可以删掉重建。

为什么这么做:交易系统里最容易出错的地方是『状态和事实不一致』。
如果直接改 state.json,一旦中途崩溃就会留下一个既不是买入前也不是买入后的
中间态,而且事后无法追溯。只追加流水账,状态永远可以重算。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, date

from .config import Config

BUY = "BUY"
SELL = "SELL"
TICKET = "TICKET"
NOTE = "NOTE"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return date.today().isoformat()


@dataclass
class Position:
    code: str
    name: str
    shares: int
    cost_price: float      # 含费用的摊薄成本
    open_date: str
    t0: bool = False

    def market_value(self, price: float) -> float:
        return self.shares * price

    def pnl(self, price: float) -> float:
        return (price - self.cost_price) * self.shares

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class State:
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    peak_equity: float = 0.0
    realized_pnl_total: float = 0.0
    today_realized_pnl: float = 0.0
    today_date: str = ""
    month_round_trips: int = 0
    month_key: str = ""
    last_stop_date: str = ""

    def to_dict(self) -> dict:
        return {
            "cash": round(self.cash, 2),
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "peak_equity": round(self.peak_equity, 2),
            "realized_pnl_total": round(self.realized_pnl_total, 2),
            "today_realized_pnl": round(self.today_realized_pnl, 2),
            "today_date": self.today_date,
            "month_round_trips": self.month_round_trips,
            "month_key": self.month_key,
            "last_stop_date": self.last_stop_date,
        }


class Journal:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.path = os.path.join(cfg.data_dir, "journal.jsonl")
        self.state_path = os.path.join(cfg.data_dir, "state.json")

    # ---------------------------------------------------------------- 写入

    def append(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("at", _now())
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def record_ticket(self, ticket: dict) -> None:
        self.append({
            "type": TICKET,
            "action": ticket.get("action"),
            "code": ticket.get("code"),
            "name": ticket.get("name"),
            "shares": ticket.get("shares"),
            "notional": ticket.get("notional"),
            "price": ticket.get("price"),
            "risk_allowed": (ticket.get("risk") or {}).get("allowed"),
            "ai_decision": (ticket.get("ai") or {}).get("decision"),
            "ticket_id": ticket.get("ticket_id"),
        })

    def record_fill(self, code: str, name: str, side: str, price: float,
                    shares: int, fee: float = 0.0, t0: bool = False,
                    note: str = "") -> None:
        """登记一笔真实成交。这是唯一会改变持仓的事件。"""
        self.append({
            "type": side, "code": code, "name": name, "price": price,
            "shares": shares, "fee": fee, "t0": t0, "note": note,
        })
        self.rebuild()

    def record_note(self, text: str) -> None:
        self.append({"type": NOTE, "text": text})

    # ---------------------------------------------------------------- 读取

    def events(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def rebuild(self) -> State:
        """从流水账重算全部状态。任何时候都可以调用。"""
        st = State(cash=self.cfg.capital, peak_equity=self.cfg.capital)
        today = _today()
        month = today[:7]

        for ev in self.events():
            et = ev.get("type")
            if et == BUY:
                code = ev["code"]
                shares = int(ev["shares"])
                price = float(ev["price"])
                fee = float(ev.get("fee") or 0.0)
                notional = shares * price
                total = notional + fee
                st.cash -= total
                # 成本价里摊入买入费用,这样卖出时的盈亏才是真实的
                cost = total / shares if shares else price
                prev = st.positions.get(code)
                if prev:
                    merged_shares = prev.shares + shares
                    merged_cost = (prev.cost_price * prev.shares + cost * shares) / merged_shares
                    st.positions[code] = Position(
                        code, ev.get("name", prev.name), merged_shares,
                        merged_cost, prev.open_date, ev.get("t0", prev.t0),
                    )
                else:
                    st.positions[code] = Position(
                        code, ev.get("name", code), shares, cost,
                        ev.get("at", today)[:10], bool(ev.get("t0")),
                    )
            elif et == SELL:
                code = ev["code"]
                shares = int(ev["shares"])
                price = float(ev["price"])
                fee = float(ev.get("fee") or 0.0)
                pos = st.positions.get(code)
                if not pos:
                    continue
                sold = min(shares, pos.shares)
                proceeds = sold * price - fee
                cost_basis = sold * pos.cost_price
                pnl = proceeds - cost_basis
                st.cash += proceeds
                st.realized_pnl_total += pnl
                if ev.get("at", "")[:10] == today:
                    st.today_realized_pnl += pnl
                if pnl < 0 and ev.get("note") == "stop":
                    st.last_stop_date = ev.get("at", "")[:10]
                if pos.shares - sold <= 0:
                    st.positions.pop(code, None)
                else:
                    pos.shares -= sold
            elif et == "STOP":
                st.last_stop_date = ev.get("at", today)[:10]

        # 当日盈亏在累加时已经按成交日期过滤过,跨日自然归零
        st.today_date = today

        # 本月往返次数 = 本月卖出笔数(一笔卖出即完成一次往返)
        st.month_key = month
        st.month_round_trips = sum(
            1 for ev in self.events()
            if ev.get("type") == SELL and ev.get("at", "")[:7] == month
        )
        return st

    def save_state(self, st: State) -> None:
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(st.to_dict(), fh, ensure_ascii=False, indent=2)

    def load_state(self) -> State:
        st = self.rebuild()
        self.save_state(st)
        return st

    def days_since_stop(self, st: State) -> int:
        if not st.last_stop_date:
            return 999
        try:
            d = datetime.strptime(st.last_stop_date, "%Y-%m-%d").date()
        except ValueError:
            return 999
        return (date.today() - d).days

    def to_account_state(self, st: State, prices: dict[str, float]):
        """转成 risk 模块需要的 AccountState。"""
        from .risk import AccountState
        mv = 0.0
        for code, pos in st.positions.items():
            px = prices.get(code, pos.cost_price)
            mv += pos.market_value(px)
        equity = st.cash + mv
        peak = max(st.peak_equity, equity, self.cfg.capital)
        return AccountState(
            total_capital=self.cfg.capital,
            cash=st.cash,
            market_value=mv,
            peak_equity=peak,
            today_realized_pnl=st.today_realized_pnl,
            month_round_trips=st.month_round_trips,
            days_since_stop=self.days_since_stop(st),
            last_stop_date=st.last_stop_date,
        )

    def update_peak(self, st: State, prices: dict[str, float]) -> None:
        mv = sum(p.market_value(prices.get(c, p.cost_price)) for c, p in st.positions.items())
        equity = st.cash + mv
        if equity > st.peak_equity:
            st.peak_equity = equity
