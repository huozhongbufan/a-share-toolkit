"""贴着同花顺的常驻状态面板。

为什么这么做
------------
同花顺没有源码，改不了它的界面。但可以用一个**独立的无边框置顶窗口**
贴在它旁边 —— 不动同花顺内部，只是让两块窗口在视觉上拼在一起。

零第三方依赖：只用标准库的 `tkinter`（UI）+ `ctypes`（读窗口位置）。
这是本项目一贯的约束，不为了一个面板引入 Electron/WebView2。

贴哪边是算出来的，不是猜的
--------------------------
实测：屏幕逻辑 1721x960，同花顺在 (799,539) 900x413。
右边缘 1699 → 右边只剩 22px，贴不了；下边只剩 8px，也贴不了；
**左边空出 799px**，所以默认贴左边。

窗口被最大化时四面都没空间，那就退化成「浮在角落的置顶小窗」。

它能做什么、不能做什么
----------------------
能：一眼看到账户、待办、API 花费、数据新鲜度，不用切终端。
不能：下单、改配置、跑完整流水线（那条要 30 秒以上，不适合放进面板）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime

from .config import Config

USER32 = ctypes.WinDLL("user32", use_last_error=True)

PANEL_W_MIN = 300
PANEL_GAP = 8

BG = "#0f1216"
CARD = "#171b21"
LINE = "#232a33"
FG = "#e6e8eb"
DIM = "#8b929c"
OK = "#4ade80"
WARN = "#fbbf24"
BAD = "#f87171"
ACCENT = "#60a5fa"


# ---------------------------------------------------------------- 窗口几何


def find_ths() -> tuple[int, str] | None:
    """按标题含「同花顺」找主窗口。"""
    hits: list[tuple[int, str]] = []
    proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if not USER32.IsWindowVisible(hwnd):
            return True
        n = USER32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        USER32.GetWindowTextW(hwnd, buf, n + 1)
        if "同花顺" in buf.value:
            hits.append((hwnd, buf.value))
        return True

    USER32.EnumWindows(proto(cb), 0)
    return hits[0] if hits else None


def rect_of(hwnd: int) -> tuple[int, int, int, int] | None:
    r = wt.RECT()
    if not USER32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def screen_size() -> tuple[int, int]:
    return (USER32.GetSystemMetrics(0), USER32.GetSystemMetrics(1))


def pick_geometry(ths: tuple[int, int, int, int] | None,
                  want_w: int) -> tuple[int, int, int, int, str]:
    """算面板该摆哪。返回 (x, y, w, h, 位置说明)。"""
    sw, sh = screen_size()
    if ths is None:
        # 同花顺没开：浮在右上角
        w = max(PANEL_W_MIN, min(want_w, 380))
        return sw - w - 20, 60, w, 470, "浮动（同花顺未运行）"

    x, y, w, h = ths
    room_left = x - PANEL_GAP
    room_right = sw - (x + w) - PANEL_GAP
    pw = max(PANEL_W_MIN, min(want_w, 460))
    ph = max(470, h)

    if room_left >= pw:
        return x - pw - PANEL_GAP, y, pw, ph, "贴左边"
    if room_right >= pw:
        return x + w + PANEL_GAP, y, pw, ph, "贴右边"
    # 两边都放不下：浮在右上角，高度缩一点
    fw = max(PANEL_W_MIN, min(360, sw // 4))
    return sw - fw - 20, 60, fw, min(360, sh - 120), "浮动（同花顺占满屏）"


# ---------------------------------------------------------------- 数据


def _freshness(as_of: str | None) -> tuple[str, str]:
    """数据时间 → (显示文本, 颜色)。"""
    if not as_of:
        return "无行情数据", DIM
    s = str(as_of)
    try:
        if len(s) == 14 and s.isdigit():
            dt = datetime.strptime(s, "%Y%m%d%H%M%S")
            age = (datetime.now() - dt).total_seconds()
            txt = f"{dt:%m-%d %H:%M:%S}"
            if age < 300:
                return f"{txt}  ·  {int(age)} 秒前", OK
            if age < 3600:
                return f"{txt}  ·  {int(age/60)} 分钟前", WARN
            return f"{txt}  ·  {age/3600:.1f} 小时前", BAD
    except ValueError:
        pass
    return s, DIM


def collect(cfg: Config) -> dict:
    """抓一次状态。全部读本地文件，不发网络请求。"""
    out: dict = {"at": datetime.now().strftime("%H:%M:%S")}
    try:
        from .journal import Journal
        st = Journal(cfg).load_state()
        out["cash"] = st.cash
        out["n_pos"] = len(st.positions)
        out["pnl"] = st.realized_pnl_total
        out["rounds"] = st.month_round_trips
    except Exception as exc:
        out["state_err"] = str(exc)[:60]

    try:
        from .todos import TodoStore
        ts = TodoStore(cfg)
        out["todo_open"] = len(ts.open_todos())
        stats = ts.stats()
        out["todo_total"] = stats.get("total", 0)
        out["todo_done"] = stats.get("done", 0)
        out["todo_rej"] = stats.get("rejected", 0)
    except Exception as exc:
        out["todo_err"] = str(exc)[:60]

    try:
        from .budget import BudgetGuard
        b = BudgetGuard(cfg).report()
        out["spend_day"] = b["today"]
        out["spend_month"] = b["month"]
        out["spend_cap"] = b["monthly_limit"]
    except Exception as exc:
        out["budget_err"] = str(exc)[:60]

    try:
        from . import ths_scout
        out["table_age"] = ths_scout.table_age_hours()
    except Exception:
        out["table_age"] = None

    try:
        from . import train
        r = train.report(cfg)
        out["train_runs"] = r.get("runs", 0)
        out["train_days"] = r.get("days", 0)
        gb = r.get("gate_blocks") or {}
        out["top_gate"] = next(iter(gb), "") if gb else ""
    except Exception:
        out["train_runs"] = 0

    # 最近一次训练的数据时间
    try:
        from . import train as _t
        rows = _t.entries(cfg)
        if rows:
            out["last_train_at"] = rows[-1].get("at", "")[5:16]
    except Exception:
        pass

    # ---- 唯一一次网络请求：实时行情 ----
    # 为什么非要有这一条：早期版本把「上次训练记录的时间」标成「行情时间」，
    # 面板显示「10:16:50 · 51 分钟前」，看的人会以为那是实时价格 ——
    # 而数据新鲜度正是最在意的东西。宁可多一个请求，也不能让时间戳说谎。
    try:
        from .quotes import fetch_quotes
        q = fetch_quotes(["sh601288"])
        x = q.get("sh601288")
        if x:
            out["quote_ts"] = x.ts
            out["quote_price"] = x.price
            out["quote_pct"] = round(x.pct * 100, 2)
    except Exception as exc:
        out["quote_err"] = f"行情接口失败：{str(exc)[:46]}"
    return out


# ---------------------------------------------------------------- 面板


class Dock:
    def __init__(self, cfg: Config, width: int = 380, interval: int = 60):
        self.cfg = cfg
        self.want_w = width
        self.interval = interval
        self.q: queue.Queue = queue.Queue()
        self.ths_hwnd: int | None = None
        self.last_geo: tuple | None = None

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        # overrideredirect 之后窗口没有标题栏，但标题仍然要设 ——
        # 否则进程列表里显示成 'tk'，没法辨认也没法按标题截图。
        self.root.title("etf-copilot 工作台")

        self._build()
        self.root.after(50, self._pump)
        self.root.after(100, self._refresh_async)
        self.root.after(150, self._follow)
        self.root.after(200, self._tick)

    # -------------------------------------------------- UI

    def _build(self) -> None:
        bar = tk.Frame(self.root, bg=CARD, height=26)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        self.lb_title = tk.Label(bar, text="etf-copilot", bg=CARD, fg=FG,
                                 font=("Microsoft YaHei", 9, "bold"))
        self.lb_title.pack(side="left", padx=(10, 6))
        self.lb_pos = tk.Label(bar, text="", bg=CARD, fg=DIM,
                               font=("Microsoft YaHei", 8))
        self.lb_pos.pack(side="left")

        close = tk.Label(bar, text="✕", bg=CARD, fg=DIM,
                         font=("Microsoft YaHei", 10), cursor="hand2")
        close.pack(side="right", padx=(0, 10))
        close.bind("<Button-1>", lambda _e: self.root.destroy())

        for w in (bar, self.lb_title, self.lb_pos):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=10, pady=(8, 4))

        self.lb_fresh = self._section("行情时间")
        self.lb_conv = self._section("结论")
        self.lb_todo = self._section("待办")
        self.lb_acct = self._section("账户")
        self.lb_plan = self._section("路 A 定投")
        self.lb_train = self._section("训练 / API 花费")
        self.lb_err = tk.Label(self.body, text="", bg=BG, fg=BAD,
                               font=("Microsoft YaHei", 8),
                               justify="left", anchor="w", wraplength=340)
        self.lb_err.pack(fill="x")

        foot = tk.Frame(self.root, bg=CARD, height=24)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        self.lb_foot = tk.Label(foot, text="加载中…", bg=CARD, fg=DIM,
                                font=("Microsoft YaHei", 8))
        self.lb_foot.pack(side="left", padx=10)
        rf = tk.Label(foot, text="刷新", bg=CARD, fg=ACCENT,
                      font=("Microsoft YaHei", 8), cursor="hand2")
        rf.pack(side="right", padx=10)
        rf.bind("<Button-1>", lambda _e: self._refresh_async())

    def _section(self, title: str) -> tk.Label:
        box = tk.Frame(self.body, bg=BG)
        box.pack(fill="x", pady=(0, 7))
        tk.Label(box, text=title, bg=BG, fg=DIM,
                 font=("Microsoft YaHei", 8), anchor="w").pack(fill="x")
        lb = tk.Label(box, text="—", bg=BG, fg=FG,
                      font=("Microsoft YaHei", 10), anchor="w", justify="left",
                      wraplength=340)
        lb.pack(fill="x")
        return lb

    # -------------------------------------------------- 拖动

    def _drag_start(self, e) -> None:
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()
        self._dragged = True

    def _drag_move(self, e) -> None:
        try:
            self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")
        except Exception:
            pass

    # -------------------------------------------------- 跟随同花顺

    def _follow(self) -> None:
        try:
            hit = find_ths()
            ths = rect_of(hit[0]) if hit else None
            self.ths_hwnd = hit[0] if hit else None
            geo = pick_geometry(ths, self.want_w)
            if getattr(self, "_dragged", False):
                pass  # 用户手动拖过就不再抢位置，尊重他的选择
            elif geo != self.last_geo:
                x, y, w, h = geo[:4]
                self.root.geometry(f"{w}x{h}+{x}+{y}")
                self.lb_pos.config(text=geo[4])
                self.last_geo = geo
        except Exception:
            pass
        self.root.after(1200, self._follow)

    # -------------------------------------------------- 刷新

    def _refresh_async(self) -> None:
        self.lb_foot.config(text="刷新中…")

        def work():
            try:
                self.q.put(("data", collect(self.cfg)))
            except Exception as exc:
                self.q.put(("err", str(exc)[:120]))

        threading.Thread(target=work, daemon=True).start()

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "data":
                    self._render(payload)
                elif kind == "err":
                    self.lb_err.config(text=f"读取失败：{payload}")
                    self.lb_foot.config(text="刷新失败")
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _tick(self) -> None:
        # 每 interval 秒自动刷一次
        self._refresh_async()
        self.root.after(self.interval * 1000, self._tick)

    def _render(self, d: dict) -> None:
        # 行情时间：优先报实时快照的时刻，拿不到就明说拿不到
        ts = d.get("quote_ts")
        if ts:
            text, color = _freshness(ts)
            extra = ""
            if d.get("quote_price"):
                extra = f"　农行 {d['quote_price']} {d.get('quote_pct', 0):+.2f}%"
            self.lb_fresh.config(text=text + extra, fg=color)
        elif d.get("quote_err"):
            self.lb_fresh.config(text="行情拿不到（见底部红字）", fg=BAD)
        else:
            self.lb_fresh.config(text="尚未取到实时行情", fg=DIM)

        n_open = d.get("todo_open", 0)
        if n_open:
            self.lb_conv.config(text=f"有 {n_open} 条待办待处理", fg=WARN)
        else:
            self.lb_conv.config(text="今天不需要操作", fg=OK)

        self.lb_todo.config(
            text=f"未完成 {n_open} · 累计 {d.get('todo_total',0)}"
                 f"（成交 {d.get('todo_done',0)} / 否决 {d.get('todo_rej',0)}）",
            fg=FG)

        if "state_err" in d:
            self.lb_acct.config(text="读取失败", fg=BAD)
        else:
            self.lb_acct.config(
                text=f"现金 ¥{d.get('cash',0):,.2f} · 持仓 {d.get('n_pos',0)} 只\n"
                     f"已实现 ¥{d.get('pnl',0):+,.2f} · 本月往返 {d.get('rounds',0)} 次",
                fg=FG)

        age = d.get("table_age")
        if age is None:
            tag = "本地表 未知"
        elif age < 1:
            tag = f"本地表 {age*60:.0f} 分钟前"
        elif age < 100:
            tag = f"本地表 {age:.0f} 小时前"
        else:
            tag = f"本地表 {age/24:.1f} 天前"
        self.lb_plan.config(text=f"农行 601288 · 月投 100→1000 · 13 年\n{tag}", fg=FG)

        runs = d.get("train_runs", 0)
        self.lb_train.config(
            text=f"{runs} 次 / {d.get('train_days',0)} 天"
                 f"　今日 ¥{d.get('spend_day',0):.3f}"
                 f" / 本月 ¥{d.get('spend_month',0):.2f}",
            fg=FG if runs else DIM)

        errs = [f"{k}: {v}" for k, v in d.items() if k.endswith("_err")]
        self.lb_err.config(text="\n".join(errs) if errs else "")
        self.lb_foot.config(text=f"更新于 {d.get('at','')}　每 {self.interval}s 刷新")

    def run(self) -> None:
        self.root.mainloop()


def run(cfg: Config, width: int = 380, interval: int = 60) -> None:
    Dock(cfg, width=width, interval=interval).run()
