"""待办队列与状态机 —— 工作流的骨架。

整个系统的核心不是行情，是**一条待办的生命周期**。所有模块最终都汇到这里。

状态机是显式的，因为要能区分三件不同的事：

    系统没给信号
    系统给了，我看到了，我决定不做      （已放弃，必须填原因）
    系统给了，我做了                    （已成交）

如果这三件事混在一起，复盘时永远不知道问题出在策略还是纪律上。

存储沿用 journal 的原则：**只追加，不修改**。`data/todos.jsonl` 里每一条都是
一次状态迁移事件，当前状态由重放得出。改状态 = 追加一条新事件，
永远不回头改历史。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime

from .config import Config

# ---------------------------------------------------------------- 状态定义

PENDING_REVIEW = "待复核"
PENDING_CONFIRM = "待确认"
PENDING_EXECUTE = "待执行"
PENDING_FILL = "待登记"
DONE = "已成交"
REJECTED = "已否决"
ABANDONED = "已放弃"

TERMINAL = {DONE, REJECTED, ABANDONED}

# 合法的状态迁移。不在表里的一律拒绝 —— 状态机不能靠调用方自觉。
TRANSITIONS: dict[str, set[str]] = {
    PENDING_REVIEW: {PENDING_CONFIRM, REJECTED},
    PENDING_CONFIRM: {PENDING_EXECUTE, ABANDONED, REJECTED},
    PENDING_EXECUTE: {PENDING_FILL, ABANDONED},
    PENDING_FILL: {DONE, ABANDONED},
    DONE: set(),
    REJECTED: set(),
    ABANDONED: set(),
}

# 放弃时必须给理由的状态：从「待确认」起，是人的决定，不是系统的
NEEDS_REASON = {PENDING_CONFIRM, PENDING_EXECUTE, PENDING_FILL}

BUY = "买入"
SELL = "卖出"


class TodoError(Exception):
    """状态机层面的错误。调用方必须处理，不能吞。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Transition:
    at: str
    frm: str
    to: str
    actor: str = "system"     # system | ai | human
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Todo:
    id: str
    created_at: str
    kind: str                 # 买入 | 卖出
    code: str
    name: str
    price: float
    shares: int = 0
    notional: float = 0.0
    state: str = PENDING_REVIEW
    # 决策依据快照 —— 冻结当时的数字，事后复盘才说得清
    signal: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    ai: dict = field(default_factory=dict)
    economics: dict = field(default_factory=dict)
    # 数据来源与时间戳。降级必须看得见，绝不用假数据顶上。
    data_health: dict = field(default_factory=dict)
    history: list[Transition] = field(default_factory=list)
    fill: dict = field(default_factory=dict)
    abandon_reason: str = ""

    # ---------------------------------------------------------- 属性

    @property
    def is_open(self) -> bool:
        return self.state not in TERMINAL

    @property
    def is_sell(self) -> bool:
        return self.kind == SELL

    @property
    def sort_key(self) -> tuple:
        """排序规则写死，不可配：

            1. 卖出永远排在买入前面（风控优先）
            2. 同类型内，已推进到后面的排前面（待登记 > 待执行 > 待确认）
            3. 最后按时间
        """
        kind_rank = 0 if self.is_sell else 1
        state_rank = {
            PENDING_FILL: 0, PENDING_EXECUTE: 1, PENDING_CONFIRM: 2, PENDING_REVIEW: 3,
        }.get(self.state, 9)
        return (kind_rank, state_rank, self.created_at)

    @property
    def blocked(self) -> bool:
        return not self.risk.get("allowed", False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["history"] = [t.to_dict() for t in self.history]
        d["is_open"] = self.is_open
        d["is_sell"] = self.is_sell
        d["blocked"] = self.blocked
        return d


class TodoStore:
    """只追加的待办流水账。

    dry=True 时**只构建 Todo 对象，不写盘**，用于「训练模式」：
    每天跑一遍完整流水线，把闸门会拦什么、AI 会怎么判都记下来，
    但不污染真实的待办队列。

    注意一个坑：`next_id()` 依赖已经落盘的记录来编号。
    dry 模式下记录不落盘，如果不额外记账，每次 create 都会拿到同一个 id。
    所以 dry 模式下用 `_dry_ids` 在内存里补编号。
    """

    def __init__(self, cfg: Config, dry: bool = False):
        self.cfg = cfg
        self.dry = dry
        self._dry_ids: list[str] = []
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.path = os.path.join(cfg.data_dir, "todos.jsonl")

    # ---------------------------------------------------------- 写

    def _append(self, event: dict) -> None:
        if self.dry:
            return
        event = dict(event)
        event.setdefault("at", _now())
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def next_id(self, when: str | None = None) -> str:
        day = (when or _now())[:10].replace("-", "")
        existing = self.all()
        n = sum(1 for t in existing if t.id.startswith(f"T{day}")) + 1
        if self.dry:
            # dry 模式不落盘，用内存里的编号补齐，否则每次都返回同一个 id
            n += sum(1 for i in self._dry_ids if i.startswith(f"T{day}"))
        return f"T{day}-{n:02d}"

    def create(self, *, kind: str, code: str, name: str, price: float,
               shares: int = 0, notional: float = 0.0,
               signal: dict | None = None, risk: dict | None = None,
               ai: dict | None = None, economics: dict | None = None,
               data_health: dict | None = None,
               initial_state: str = PENDING_REVIEW,
               reason: str = "") -> Todo:
        todo = Todo(
            id=self.next_id(),
            created_at=_now(),
            kind=kind, code=code, name=name, price=price,
            shares=shares, notional=notional,
            signal=signal or {}, risk=risk or {}, ai=ai or {},
            economics=economics or {}, data_health=data_health or {},
        )
        if self.dry:
            self._dry_ids.append(todo.id)
        self._append({"type": "create", "todo": {
            "id": todo.id, "created_at": todo.created_at, "kind": kind,
            "code": code, "name": name, "price": price, "shares": shares,
            "notional": notional, "signal": todo.signal, "risk": todo.risk,
            "ai": todo.ai, "economics": todo.economics,
            "data_health": todo.data_health,
        }})
        if initial_state != PENDING_REVIEW:
            todo.history.append(Transition(_now(), PENDING_REVIEW, initial_state,
                                           "system", reason))
            self._append({"type": "state", "id": todo.id, "from": PENDING_REVIEW,
                          "to": initial_state, "actor": "system", "reason": reason})
            todo.state = initial_state
        return todo

    def transition(self, todo_id: str, to: str, *, actor: str = "human",
                   reason: str = "") -> Todo:
        """推进状态。非法迁移直接抛错，不静默忽略。"""
        todo = self.get(todo_id)
        if todo is None:
            raise TodoError(f"待办不存在: {todo_id}")
        frm = todo.state
        if to not in TRANSITIONS.get(frm, set()):
            raise TodoError(
                f"非法状态迁移：{frm} → {to}。"
                f"从 {frm} 只能到 {sorted(TRANSITIONS.get(frm, set())) or '（终态，不可再变）'}"
            )
        if to == ABANDONED and frm in NEEDS_REASON and not reason.strip():
            raise TodoError("放弃必须填原因 —— 这是最有价值的一条复盘数据")

        self._append({"type": "state", "id": todo_id, "from": frm, "to": to,
                      "actor": actor, "reason": reason})
        todo.state = to
        todo.history.append(Transition(_now(), frm, to, actor, reason))
        if to == ABANDONED:
            todo.abandon_reason = reason
        return todo

    def record_fill(self, todo_id: str, *, price: float, shares: int,
                    fee: float = 0.0, at: str = "") -> Todo:
        """登记真实成交。回填的是券商实际扣的钱，不是估算。"""
        todo = self.get(todo_id)
        if todo is None:
            raise TodoError(f"待办不存在: {todo_id}")
        if todo.state != PENDING_FILL:
            raise TodoError(f"只有「{PENDING_FILL}」状态能登记成交，当前是 {todo.state}")
        if price <= 0 or shares <= 0:
            raise TodoError("成交价和份数必须为正")

        fill = {"price": price, "shares": shares, "fee": fee,
                "at": at or _now(), "notional": round(price * shares, 2)}
        self._append({"type": "fill", "id": todo_id, **fill})
        todo.fill = fill
        return self.transition(todo_id, DONE, actor="human",
                               reason=f"成交 {shares} 份 @ {price}")

    def note(self, todo_id: str, text: str) -> None:
        self._append({"type": "note", "id": todo_id, "text": text})

    # ---------------------------------------------------------- 读

    def events(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def all(self) -> list[Todo]:
        """重放流水账，得到当前全部待办。"""
        todos: dict[str, Todo] = {}
        for ev in self.events():
            et = ev.get("type")
            if et == "create":
                raw = ev["todo"]
                todos[raw["id"]] = Todo(
                    id=raw["id"], created_at=raw["created_at"], kind=raw["kind"],
                    code=raw["code"], name=raw["name"], price=raw["price"],
                    shares=raw.get("shares", 0), notional=raw.get("notional", 0.0),
                    signal=raw.get("signal") or {}, risk=raw.get("risk") or {},
                    ai=raw.get("ai") or {}, economics=raw.get("economics") or {},
                    data_health=raw.get("data_health") or {},
                )
            elif et == "state":
                t = todos.get(ev["id"])
                if not t:
                    continue
                t.state = ev["to"]
                t.history.append(Transition(
                    ev.get("at", ""), ev["from"], ev["to"],
                    ev.get("actor", "system"), ev.get("reason", "")))
                if ev["to"] == ABANDONED:
                    t.abandon_reason = ev.get("reason", "")
            elif et == "fill":
                t = todos.get(ev["id"])
                if t:
                    t.fill = {k: ev[k] for k in
                              ("price", "shares", "fee", "at", "notional") if k in ev}
        return list(todos.values())

    def get(self, todo_id: str) -> Todo | None:
        return next((t for t in self.all() if t.id == todo_id), None)

    def open_todos(self) -> list[Todo]:
        return sorted([t for t in self.all() if t.is_open], key=lambda t: t.sort_key)

    def closed_todos(self) -> list[Todo]:
        return sorted([t for t in self.all() if not t.is_open],
                      key=lambda t: t.created_at, reverse=True)

    def has_open_for(self, code: str) -> bool:
        """同一标的已有未完成待办时，不再重复生成。"""
        return any(t.code == code for t in self.open_todos())

    # ---------------------------------------------------------- 统计

    def stats(self) -> dict:
        allt = self.all()
        out = {
            "total": len(allt),
            "open": sum(1 for t in allt if t.is_open),
            "done": sum(1 for t in allt if t.state == DONE),
            "rejected": sum(1 for t in allt if t.state == REJECTED),
            "abandoned": sum(1 for t in allt if t.state == ABANDONED),
        }
        # 放弃原因分布 —— 这是最能反映「你在什么情况下会违背系统」的数据
        reasons: dict[str, int] = {}
        for t in allt:
            if t.state == ABANDONED and t.abandon_reason:
                key = t.abandon_reason.strip()[:24]
                reasons[key] = reasons.get(key, 0) + 1
        out["abandon_reasons"] = sorted(reasons.items(), key=lambda kv: -kv[1])
        return out
