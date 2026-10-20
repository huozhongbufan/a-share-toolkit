"""同花顺风格的桌面工作台（原生窗口软件，不是网页）。

布局照同花顺客户端：
  ┌──────────────────────────────────────────────────────────────┐
  │ [ETF] 工作台      上证… 深证… 创业板…            （顶部黑条）  │
  │ 上涨 n ████ 下跌 m ████                        （涨跌家数条）  │
  ├──────────┬───────────────────────────────────┬───────────────┤
  │ 自选股    │ 标的名称 现价 涨幅   [分时][日K]     │ 账户 / 路A    │
  │ 列表      │ ┌─────────────────────────────┐   │ 路B / 花费    │
  │（点击     │ │        K线 / 分时图           │   │               │
  │ 切图）    │ │  （红涨绿跌、MA、成交量、悬浮）│   │               │
  │          │ └─────────────────────────────┘   │               │
  ├──────────┴───────────────────────────────────┴───────────────┤
  │ [问行情输入] [问行情][找标的][跑训练][刷新]                      │
  │ 行情时间 · 待办 · 更新于                                     │
  └──────────────────────────────────────────────────────────────┘

数据（全部已验证）：
  · 指数       quotes.fetch_quotes，30 秒刷
  · 自选股     截同花顺窗口 → 视觉模型 → API 核对，60 秒刷
  · 日K/分时   ec.chart（腾讯），选中即载，日K 5 分钟刷 / 分时 60 秒刷
  · 账户/花费   dock.collect，60 秒刷
  · 涨跌家数    cheap 全市场快照缓存（同日快照，标注时间，不冒充实时）

诚实原则：同花顺没跑 → 列表明说；美股（MSFT）没有K线数据 → 明说；
全市场快照旧 → 标时间。任何子模块失败只影响自己那块，窗口绝不崩。
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk

from .config import Config
from .chart import fetch_kline, fetch_minute, sma

# ---------------------------------------------------------------- 同花顺配色

BG = "#12151a"        # 主背景（同花顺黑皮肤）
BG_PANEL = "#161a20"  # 表格/面板
BG_BAR = "#0d0f13"    # 顶部黑条 / 状态栏
BG_HEAD = "#1c222b"   # 表头
FG = "#d5dae2"
DIM = "#7c8494"
RED = "#f04a3e"       # 涨（A 股惯例）
GREEN = "#0fa96b"     # 跌
ROW_ODD = "#171c23"
ROW_EVEN = "#14181f"
GRID = "#1e242c"
MA5_C = "#f7d02c"
MA10_C = "#c678dd"
MA20_C = "#4aa3ff"

INDEX_CODES = ["sh000001", "sz399001", "sz399006"]


def _age_text(ts: str | None) -> tuple[str, str]:
    if not ts:
        return "行情无数据", DIM
    try:
        dt = datetime.strptime(str(ts), "%Y%m%d%H%M%S")
        age = (datetime.now() - dt).total_seconds()
        txt = dt.strftime("%H:%M:%S")
        if age < 300:
            return f"{txt} · {int(age)}秒前", RED
        if age < 3600:
            return f"{txt} · {int(age/60)}分钟前", DIM
        return f"{txt} · {age/3600:.1f}小时前", DIM
    except ValueError:
        return str(ts), DIM


class Workbench:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.q: queue.Queue = queue.Queue()
        self.ask_busy = False
        self.train_busy = False
        self.idx_labels: dict[str, tuple[tk.Label, tk.Label]] = {}

        # 图表状态
        self.cur_code: str | None = None
        self.mode = "day"          # day=日K, min=分时
        self.k_rows: list = []
        self.m_rows: list = []
        self.prev_close: float | None = None
        self._redraw_job = None

        self.root = tk.Tk()
        self.root.title("ETF 工作台")
        self.root.geometry("1240x720+140+50")
        self.root.minsize(1000, 560)
        self.root.configure(bg=BG)

        self._build()
        self.root.after(200, self._pump)
        self.root.after(300, self._tick_indices)
        self.root.after(500, self._tick_watchlist)
        self.root.after(800, self._tick_overview)
        self.root.after(3000, self._tick_chart)

    # ------------------------------------------------------------ UI

    def _build(self) -> None:
        bar = tk.Frame(self.root, bg=BG_BAR, height=44)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        logo = tk.Label(bar, text="ETF", bg=RED, fg="#ffffff",
                        font=("Microsoft YaHei", 12, "bold"), padx=8)
        logo.pack(side="left", padx=(12, 8), pady=9)
        tk.Label(bar, text="ETF 工作台", bg=BG_BAR, fg=FG,
                 font=("Microsoft YaHei", 12, "bold")).pack(side="left")
        self._build_indices(bar)

        self.breadth = tk.Canvas(self.root, height=22, bg=BG_BAR,
                                 highlightthickness=0)
        self.breadth.pack(fill="x")
        self._draw_breadth(None)

        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        self._build_list(main)     # 列 0
        self._build_chart(main)    # 列 1
        self._build_side(main)     # 列 2

        self._build_query(self.root)
        self._build_status(self.root)

    def _build_indices(self, bar: tk.Frame) -> None:
        for code in INDEX_CODES:
            box = tk.Frame(bar, bg=BG_BAR)
            box.pack(side="left", padx=(16, 0))
            name = {"sh000001": "上证", "sz399001": "深证",
                    "sz399006": "创业板"}.get(code, code)
            tk.Label(box, text=name, bg=BG_BAR, fg=DIM,
                     font=("Microsoft YaHei", 9)).pack(side="left")
            lp = tk.Label(box, text="—", bg=BG_BAR, fg=DIM,
                          font=("Consolas", 10, "bold"))
            lp.pack(side="left", padx=(6, 0))
            lc = tk.Label(box, text="", bg=BG_BAR, fg=DIM,
                          font=("Consolas", 10))
            lc.pack(side="left", padx=(6, 0))
            self.idx_labels[code] = (lp, lc)

    # ---- 左：自选股列表

    def _build_list(self, parent: tk.Frame) -> None:
        wrap = tk.Frame(parent, bg=BG_PANEL)
        wrap.grid(row=0, column=0, sticky="nsw", padx=(8, 4), pady=8)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=BG_PANEL,
                        fieldbackground=BG_PANEL, foreground=FG,
                        borderwidth=0, rowheight=24,
                        font=("Microsoft YaHei", 9))
        style.configure("Treeview.Heading", background=BG_HEAD,
                        foreground=DIM, borderwidth=0, relief="flat",
                        font=("Microsoft YaHei", 8, "bold"))
        style.map("Treeview", background=[("selected", "#2a3140")],
                  foreground=[("selected", FG)])

        cols = ("name", "price", "pct")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                 selectmode="browse")
        for c, (t, w, a) in {"name": ("名称", 118, "w"),
                             "price": ("最新", 62, "e"),
                             "pct": ("涨幅", 62, "e")}.items():
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, minwidth=50, anchor=a,
                             stretch=(c == "name"))
        self.tree.tag_configure("up", foreground=RED)
        self.tree.tag_configure("down", foreground=GREEN)
        self.tree.tag_configure("flat", foreground=DIM)
        self.tree.tag_configure("odd", background=ROW_ODD)
        self.tree.tag_configure("even", background=ROW_EVEN)
        self.tree.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        sb = ttk.Scrollbar(wrap, orient="vertical",
                           command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        tk.Label(wrap, text="自选股", bg=BG_PANEL, fg=DIM,
                 font=("Microsoft YaHei", 8), anchor="w").pack(
            side="bottom", fill="x", padx=8, pady=(2, 4))

    # ---- 中：图表区

    def _build_chart(self, parent: tk.Frame) -> None:
        box = tk.Frame(parent, bg=BG_PANEL)
        box.grid(row=0, column=1, sticky="nsew", padx=4, pady=8)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(1, weight=1)

        head = tk.Frame(box, bg=BG_PANEL, height=36)
        head.grid(row=0, column=0, sticky="ew")
        head.pack_propagate(False)

        self.lb_name = tk.Label(head, text="点左边自选股看K线", bg=BG_PANEL,
                                fg=FG, font=("Microsoft YaHei", 11, "bold"))
        self.lb_name.pack(side="left", padx=(10, 8))
        self.lb_price = tk.Label(head, text="", bg=BG_PANEL, fg=DIM,
                                 font=("Consolas", 15, "bold"))
        self.lb_price.pack(side="left")
        self.lb_pct = tk.Label(head, text="", bg=BG_PANEL, fg=DIM,
                               font=("Consolas", 11, "bold"))
        self.lb_pct.pack(side="left", padx=(6, 10))
        self.lb_legend = tk.Label(head, text="", bg=BG_PANEL, fg=DIM,
                                  font=("Microsoft YaHei", 8))
        self.lb_legend.pack(side="left")

        self.btn_day = tk.Button(head, text="日K", command=lambda: self._set_mode("day"),
                                 relief="flat", bg=BG_HEAD, fg=FG,
                                 activebackground=BG_HEAD, activeforeground=FG,
                                 font=("Microsoft YaHei", 9), padx=10, pady=3,
                                 cursor="hand2")
        self.btn_day.pack(side="right", padx=(4, 10), pady=4)
        self.btn_min = tk.Button(head, text="分时", command=lambda: self._set_mode("min"),
                                 relief="flat", bg=BG_PANEL, fg=DIM,
                                 activebackground=BG_HEAD, activeforeground=FG,
                                 font=("Microsoft YaHei", 9), padx=10, pady=3,
                                 cursor="hand2")
        self.btn_min.pack(side="right", padx=(0, 0), pady=4)

        self.canvas = tk.Canvas(box, bg=BG_PANEL, highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Leave>", lambda _e: self._hover(""))
        self.canvas.bind("<Configure>",
                         lambda _e: self._schedule_redraw())

        self.lb_hover = tk.Label(box, text="", bg=BG_PANEL, fg=DIM,
                                 font=("Consolas", 9), anchor="w")
        self.lb_hover.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))

        self._chart_placeholder("点左边自选股看 K 线（分时/日K 可切换）")

    def _build_side(self, parent: tk.Frame) -> None:
        side = tk.Frame(parent, bg=BG, width=252)
        side.grid(row=0, column=2, sticky="ns", padx=(4, 8), pady=8)
        side.pack_propagate(False)

        def sec(title: str) -> tk.Label:
            box = tk.Frame(side, bg=BG_PANEL)
            box.pack(fill="x", pady=(0, 8))
            tk.Label(box, text=title, bg=BG_PANEL, fg=DIM,
                     font=("Microsoft YaHei", 8), anchor="w"
                     ).pack(fill="x", padx=10, pady=(8, 2))
            lb = tk.Label(box, text="—", bg=BG_PANEL, fg=FG,
                          font=("Microsoft YaHei", 10), anchor="w",
                          justify="left", wraplength=224)
            lb.pack(fill="x", padx=10, pady=(0, 8))
            return lb

        self.lb_acct = sec("账户")
        self.lb_plan = sec("路 A · 长期定投")
        self.lb_train = sec("路 B · 训练")
        self.lb_spend = sec("花费 / 模型")

    def _build_query(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=8, pady=(0, 6))
        self.qvar = tk.StringVar()
        ent = tk.Entry(row, textvariable=self.qvar, bg=BG_PANEL, fg=FG,
                       insertbackground=FG, relief="flat",
                       font=("Microsoft YaHei", 11))
        ent.pack(side="left", fill="x", expand=True, ipady=6)
        ent.bind("<Return>", lambda _e: self._ask())

        def btn(text, cmd, accent=False):
            b = tk.Button(row, text=text, command=cmd, relief="flat",
                          bg=(RED if accent else BG_PANEL),
                          fg="#fff" if accent else FG,
                          activebackground=BG_HEAD, activeforeground=FG,
                          font=("Microsoft YaHei", 10), padx=12, pady=5,
                          cursor="hand2")
            b.pack(side="left", padx=(6, 0))
            return b

        self.btn_ask = btn("问行情", self._ask)
        self.btn_find = btn("找标的", self._find)
        self.btn_train = btn("跑训练", self._train, accent=True)
        self.btn_refresh = btn("刷新", self._refresh_all)

    def _build_status(self, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=BG_BAR, height=26)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.st_quote = tk.Label(bar, text="行情加载中…", bg=BG_BAR, fg=DIM,
                                 font=("Microsoft YaHei", 8))
        self.st_quote.pack(side="left", padx=10)
        self.st_todo = tk.Label(bar, text="", bg=BG_BAR, fg=DIM,
                                font=("Microsoft YaHei", 8))
        self.st_todo.pack(side="left", padx=10)
        self.st_upd = tk.Label(bar, text="", bg=BG_BAR, fg=DIM,
                               font=("Microsoft YaHei", 8))
        self.st_upd.pack(side="right", padx=10)

    # ------------------------------------------------------------ 数据线程

    def _thread(self, fn, tag: str) -> None:
        def work():
            try:
                payload = fn()
                if payload is not None:
                    self.q.put((tag, payload))
            except Exception as exc:
                self.q.put((f"{tag}_err", str(exc)[:140]))

        threading.Thread(target=work, daemon=True).start()

    def _tick_indices(self) -> None:
        def job():
            from .quotes import fetch_quotes
            q = fetch_quotes(INDEX_CODES)
            return {c: q.get(c) for c in INDEX_CODES}
        self._thread(job, "idx")
        self.root.after(30000, self._tick_indices)

    def _tick_watchlist(self) -> None:
        def job():
            from . import screen
            cap = screen.capture(target="window", process="hexin", timeout=40)
            items = screen.read_watchlist(self.cfg, cap)
            items = screen.verify_with_api(items)
            return {"window": cap.window_title, "items": items}
        self._thread(job, "wl")
        self.root.after(60000, self._tick_watchlist)

    def _tick_overview(self) -> None:
        def job():
            from .dock import collect
            d = collect(self.cfg)
            breadth = None
            try:
                from . import cheap
                rows = cheap._load_cache(self.cfg)
                if rows:
                    up = down = flat = 0
                    ts = ""
                    for _code, x in rows.items():
                        try:
                            px = float(x.get("price") or 0)
                            prev = float(x.get("prev_close") or 0)
                            if px <= 0 or prev <= 0:
                                continue
                            if px > prev:
                                up += 1
                            elif px < prev:
                                down += 1
                            else:
                                flat += 1
                            ts = str(x.get("ts") or "")
                        except (TypeError, ValueError):
                            continue
                    breadth = {"up": up, "down": down, "flat": flat,
                               "total": up + down + flat, "ts": ts}
            except Exception:
                breadth = None
            d["breadth"] = breadth
            return d
        self._thread(job, "ov")
        self.root.after(60000, self._tick_overview)

    def _tick_chart(self) -> None:
        """当前标的按模式定时刷新。"""
        if self.cur_code:
            self._load_chart(self.cur_code, self.mode)
        self.root.after(300000 if self.mode == "day" else 60000,
                        self._tick_chart)

    # ------------------------------------------------------------ 图表

    def _set_mode(self, mode: str) -> None:
        if self.mode == mode:
            return
        self.mode = mode
        self.btn_day.config(bg=BG_HEAD if mode == "day" else BG_PANEL,
                            fg=FG if mode == "day" else DIM)
        self.btn_min.config(bg=BG_HEAD if mode == "min" else BG_PANEL,
                            fg=FG if mode == "min" else DIM)
        if self.cur_code:
            self._load_chart(self.cur_code, mode)

    def _load_chart(self, code: str, mode: str) -> None:
        # 美股没有腾讯K线（我们的数据源），明说，不装
        if not code.startswith(("sh", "sz", "bj")):
            self._chart_placeholder(f"{code} 是美股/其他市场，这里没有K线数据")
            return

        if mode == "day":
            def job():
                d = fetch_kline(code, n=120)
                d["name"] = self._name_of(code)
                return d
            self._thread(job, "k")
        else:
            def job():
                d = fetch_minute(code)
                d["name"] = self._name_of(code)
                try:
                    from .quotes import fetch_quotes
                    q = fetch_quotes([code])
                    x = q.get(code)
                    d["prev_close"] = getattr(x, "prev_close", None)
                except Exception:
                    d["prev_close"] = None
                return d
            self._thread(job, "m")

    def _name_of(self, code: str) -> str:
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, "values")
            if iid == code and vals:
                return str(vals[0])
        return code

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        code = sel[0]
        if code == self.cur_code:
            return
        self.cur_code = code
        self.lb_name.config(text=self._name_of(code))
        self.lb_price.config(text="…")
        self.lb_pct.config(text="")
        self._load_chart(code, self.mode)

    def _chart_placeholder(self, text: str) -> None:
        self.canvas.delete("all")
        w = self.canvas.winfo_width() or 500
        h = self.canvas.winfo_height() or 300
        self.canvas.create_text(w // 2, h // 2, text=text, fill=DIM,
                                font=("Microsoft YaHei", 11))

    def _schedule_redraw(self) -> None:
        if self._redraw_job:
            self.root.after_cancel(self._redraw_job)
        self._redraw_job = self.root.after(250, self._redraw)

    def _redraw(self) -> None:
        if self.mode == "day" and self.k_rows:
            self._draw_kline()
        elif self.mode == "min" and self.m_rows:
            self._draw_minute()

    def _axis(self) -> dict:
        """返回 (left, right, top, vol_top, price_bottom, date_y, step)。"""
        c = self.canvas
        W = max(c.winfo_width(), 100)
        H = max(c.winfo_height(), 100)
        left, right, top = 54, 8, 10
        vol_h = int(H * 0.20)
        price_bottom = H - vol_h - 24
        return {"W": W, "H": H, "left": left, "right": right, "top": top,
                "vol_top": price_bottom + 8, "vol_bottom": H - 20,
                "price_bottom": price_bottom, "date_y": H - 14}

    def _draw_kline(self) -> None:
        rows = self.k_rows
        if not rows:
            return
        c = self.canvas
        c.delete("all")
        a = self._axis()
        W, left, right, top = a["W"], a["left"], a["right"], a["top"]
        price_bottom = a["price_bottom"]
        plot_w = W - left - right
        n = len(rows)
        step = plot_w / n
        bw = max(1, step * 0.6)

        lo = min(r[4] for r in rows)
        hi = max(r[3] for r in rows)
        pad = (hi - lo) * 0.06 or hi * 0.01 or 0.01
        hi += pad
        lo -= pad

        def y(p):
            return price_bottom - (p - lo) / (hi - lo) * (price_bottom - top)

        for i in range(5):
            p = lo + (hi - lo) * i / 4
            yy = y(p)
            c.create_line(left, yy, W - right, yy, fill=GRID)
            c.create_text(left - 6, yy, anchor="e", fill=DIM,
                          text=f"{p:.3f}".rstrip("0").rstrip("."),
                          font=("Consolas", 8))

        for i, (_d, o, cl, h, l, _v) in enumerate(rows):
            x = left + step * i + step / 2
            up = cl >= o
            col = RED if up else GREEN
            c.create_line(x, y(h), x, y(l), fill=col)
            y1, y2 = y(o), y(cl)
            if abs(y1 - y2) < 1:
                y2 = y1 + 1
            if up:   # 阳线空心（同花顺画法）
                c.create_rectangle(x - bw / 2, min(y1, y2), x + bw / 2,
                                   max(y1, y2), outline=RED, fill=BG_PANEL)
            else:    # 阴线实心
                c.create_rectangle(x - bw / 2, min(y1, y2), x + bw / 2,
                                   max(y1, y2), outline=GREEN, fill=GREEN)

        closes = [r[2] for r in rows]
        for win, col in ((5, MA5_C), (10, MA10_C), (20, MA20_C)):
            coords = []
            for i, p in enumerate(sma(closes, win)):
                if p is not None:
                    coords += [left + step * i + step / 2, y(p)]
            if len(coords) >= 4:
                c.create_line(*coords, fill=col, width=1, smooth=True)

        last = closes[-1]
        yy = y(last)
        c.create_line(left, yy, W - right, yy, dash=(3, 3), fill=DIM)
        c.create_text(W - right, yy, anchor="e", fill=DIM,
                      text=f"{last:.3f}".rstrip("0").rstrip("."),
                      font=("Consolas", 8))

        vol_max = max(r[5] for r in rows) or 1
        vt, vb = a["vol_top"], a["vol_bottom"]
        for i, (_d, o, cl, _h, _l, v) in enumerate(rows):
            x = left + step * i + step / 2
            hh = (v / vol_max) * (vb - vt)
            col = RED if cl >= o else GREEN
            c.create_rectangle(x - bw / 2, vb - hh, x + bw / 2, vb,
                               fill=col, outline="")

        every = max(1, n // 6)
        for i in range(0, n, every):
            c.create_text(left + step * i + step / 2, a["date_y"],
                          anchor="n", fill=DIM,
                          text=rows[i][0][5:].replace("-", "/"),
                          font=("Consolas", 8))

    def _draw_minute(self) -> None:
        rows = self.m_rows
        if not rows or not self.prev_close:
            return
        c = self.canvas
        c.delete("all")
        a = self._axis()
        W, left, right, top = a["W"], a["left"], a["right"], a["top"]
        price_bottom = a["price_bottom"]
        plot_w = W - left - right
        n = len(rows)
        step = plot_w / n
        prev = self.prev_close

        prices = [r[1] for r in rows]
        avgs = []
        ca = cv = 0.0
        for r in rows:
            ca += r[3]
            cv += r[2]
            avgs.append(ca / cv if cv else r[1])

        lo = min(min(prices), prev)
        hi = max(max(prices), prev)
        pad = (hi - lo) * 0.08 or hi * 0.01 or 0.01
        hi += pad
        lo -= pad

        def y(p):
            return price_bottom - (p - lo) / (hi - lo) * (price_bottom - top)

        # 涨跌幅网格：±2% ±1% 0%
        for pct in (-2, -1, 0, 1, 2):
            p = prev * (1 + pct / 100)
            if p < lo or p > hi:
                continue
            yy = y(p)
            c.create_line(left, yy, W - right, yy, fill=GRID,
                          dash=(2, 4) if pct else ())
            c.create_text(W - right, yy, anchor="e", fill=DIM,
                          text=f"{pct:+.0f}%", font=("Consolas", 8))

        # 昨收线（0% 已画），价格线 + 均价线
        coords = []
        for i, p in enumerate(prices):
            coords += [left + step * i, y(p)]
        if len(coords) >= 4:
            c.create_line(*coords, fill="#ffffff", width=1)
        acoords = []
        for i, p in enumerate(avgs):
            acoords += [left + step * i, y(p)]
        if len(acoords) >= 4:
            c.create_line(*acoords, fill=MA5_C, width=1)

        # 成交量：按 tick 方向着色
        vol_max = max(r[2] for r in rows) or 1
        vt, vb = a["vol_top"], a["vol_bottom"]
        bw = max(1, step * 0.6)
        for i, r in enumerate(rows):
            x = left + step * i
            hh = (r[2] / vol_max) * (vb - vt)
            up = i == 0 or r[1] >= rows[i - 1][1]
            c.create_rectangle(x - bw / 2, vb - hh, x + bw / 2, vb,
                               fill=RED if up else GREEN, outline="")

        # 时间标签：按整点找位置
        targets = ["0930", "1000", "1030", "1100", "1130",
                   "1300", "1400", "1430", "1500"]
        for t in targets:
            idx = None
            for i, r in enumerate(rows):
                if r[0] >= t:
                    idx = i
                    break
            if idx is not None:
                c.create_text(left + step * idx, a["date_y"], anchor="n",
                              fill=DIM, text=t, font=("Consolas", 8))

        # 现价虚线 + 标签
        last = prices[-1]
        yy = y(last)
        col = RED if last >= prev else GREEN
        c.create_line(left, yy, W - right, yy, dash=(3, 3), fill=col)
        c.create_text(W - right, yy, anchor="e", fill=col,
                      text=f"{last:.3f}".rstrip("0").rstrip("."),
                      font=("Consolas", 8))

    def _on_hover(self, event) -> None:
        if not self.cur_code:
            return
        a = self._axis()
        left, right = a["left"], a["right"]
        W = a["W"]
        if not (left <= event.x <= W - right):
            self._hover("")
            return
        plot_w = W - left - right
        if self.mode == "day" and self.k_rows:
            n = len(self.k_rows)
            idx = int((event.x - left) / plot_w * n)
            idx = max(0, min(n - 1, idx))
            d, o, cl, h, l, v = self.k_rows[idx]
            col = RED if cl >= o else GREEN
            self._hover(f"{d}  开{o:.3f} 高{h:.3f} 低{l:.3f} 收{cl:.3f} "
                        f"量{v/1e4:.1f}万", col)
        elif self.mode == "min" and self.m_rows:
            n = len(self.m_rows)
            idx = int((event.x - left) / plot_w * n)
            idx = max(0, min(n - 1, idx))
            t, p, _v, _a = self.m_rows[idx]
            col = RED if p >= (self.prev_close or p) else GREEN
            self._hover(f"{t[:2]}:{t[2:]}  {p:.3f}", col)

    def _hover(self, text: str, color: str = DIM) -> None:
        self.lb_hover.config(text=text, fg=color)

    # ------------------------------------------------------------ 动作

    def _refresh_all(self) -> None:
        self._tick_indices()
        self._tick_watchlist()
        self._tick_overview()
        if self.cur_code:
            self._load_chart(self.cur_code, self.mode)

    def _ask(self) -> None:
        if self.ask_busy:
            return
        q = self.qvar.get().strip()
        if not q:
            q = simpledialog.askstring("问行情", "问什么？", parent=self.root)
            if not q:
                return
        self.ask_busy = True
        self.btn_ask.config(text="思考中…")
        self.qvar.set("")

        def job():
            from . import tools
            return tools.ask(self.cfg, q, model="qwen3.5:4b")
        self._thread(job, "ans")

    def _find(self) -> None:
        q = self.qvar.get().strip()
        if not q:
            q = simpledialog.askstring("找标的", "找什么？（如：防守型 / 银行 / 红利）",
                                       parent=self.root)
            if not q:
                return
        self.btn_find.config(text="检索中…")

        def job():
            from . import ths_scout
            r = ths_scout.search(self.cfg, q, top_k=8)
            return {"q": q, "result": r}
        self._thread(job, "find")

    def _train(self) -> None:
        if self.train_busy:
            messagebox.showinfo("训练", "已经在跑了，等它完成。", parent=self.root)
            return
        self.train_busy = True
        self.btn_train.config(text="训练中…")

        def job():
            from .pipeline import run_flow
            from . import train
            t0 = time.time()
            result = run_flow(self.cfg, save_todos=False)
            elapsed = time.time() - t0
            try:
                train.record(self.cfg, result, elapsed)
            except Exception:
                pass
            return {"headline": result.headline, "elapsed": elapsed}
        self._thread(job, "train")

    # ------------------------------------------------------------ 主循环

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    def _handle(self, kind: str, payload) -> None:
        if kind == "idx":
            self._render_indices(payload)
        elif kind == "idx_err":
            self.st_quote.config(text=f"指数：{payload}")
        elif kind == "wl":
            self._render_watchlist(payload)
        elif kind == "wl_err":
            self._render_watchlist_err(payload)
        elif kind == "ov":
            self._render_overview(payload)
        elif kind == "ov_err":
            self.st_quote.config(text=f"数据：{payload}")
        elif kind == "k":
            if payload.get("code") == self.cur_code and self.mode == "day":
                self.k_rows = payload.get("rows") or []
                if self.k_rows:
                    self._draw_kline()
                    last = self.k_rows[-1]
                    prev = self.k_rows[-2][2] if len(self.k_rows) > 1 else last[2]
                    self._update_chart_header(payload.get("name"), last[2], prev)
                else:
                    self._chart_placeholder("没有K线数据")
        elif kind == "k_err":
            if self.mode == "day":
                self._chart_placeholder(f"日K拿不到：{payload}")
        elif kind == "m":
            if payload.get("code") == self.cur_code and self.mode == "min":
                self.m_rows = payload.get("rows") or []
                self.prev_close = payload.get("prev_close")
                if self.m_rows and self.prev_close:
                    self._draw_minute()
                    last = self.m_rows[-1][1]
                    self._update_chart_header(payload.get("name"), last,
                                              self.prev_close)
                else:
                    self._chart_placeholder("分时数据不完整（午休/停牌？稍后自动刷）")
        elif kind == "m_err":
            if self.mode == "min":
                self._chart_placeholder(f"分时拿不到：{payload}")
        elif kind == "ans":
            self.ask_busy = False
            self.btn_ask.config(text="问行情")
            ans = payload.get("answer") or payload.get("error") or "没答上来"
            messagebox.showinfo("问行情", ans, parent=self.root)
        elif kind == "find":
            self.btn_find.config(text="找标的")
            self._popup_find(payload)
        elif kind == "find_err":
            self.btn_find.config(text="找标的")
            messagebox.showinfo("找标的", payload, parent=self.root)
        elif kind == "train":
            self.train_busy = False
            self.btn_train.config(text="跑训练")
            self._refresh_all()
            messagebox.showinfo(
                "训练完成",
                f"{payload['headline']}\n\n耗时 {payload['elapsed']:.1f} 秒",
                parent=self.root)
        elif kind == "train_err":
            self.train_busy = False
            self.btn_train.config(text="跑训练")
            messagebox.showinfo("训练失败", payload, parent=self.root)

    def _update_chart_header(self, name: str | None, price: float,
                             prev: float) -> None:
        if name:
            self.lb_name.config(text=name)
        pct = (price / prev - 1) * 100 if prev else 0
        col = RED if pct > 0 else (GREEN if pct < 0 else DIM)
        self.lb_price.config(text=f"{price:.3f}".rstrip("0").rstrip("."),
                             fg=col)
        self.lb_pct.config(text=f"{pct:+.2f}%", fg=col)
        if self.mode == "day":
            self.lb_legend.config(
                text="MA5 黄  MA10 紫  MA20 蓝", fg=DIM)
        else:
            self.lb_legend.config(text="白=价格  黄=均价", fg=DIM)

    # ------------------------------------------------------------ 渲染

    def _render_indices(self, q: dict) -> None:
        for code, x in q.items():
            labels = self.idx_labels.get(code)
            if not labels or not x:
                continue
            lp, lc = labels
            lp.config(text=f"{x.price:.2f}")
            pct = round(x.pct * 100, 2)
            color = RED if pct > 0 else (GREEN if pct < 0 else DIM)
            lp.config(fg=color)
            lc.config(text=f"{pct:+.2f}%", fg=color)

    def _render_watchlist(self, payload: dict) -> None:
        cur = self.cur_code
        self.tree.delete(*self.tree.get_children())
        items = payload.get("items") or []
        shown = 0
        for i, it in enumerate(items):
            if not it.code:
                continue
            if it.verified and it.api_price:
                price = f"{it.api_price:.3f}".rstrip("0").rstrip(".")
                pct = it.api_pct or 0
                pct_s = f"{pct:+.2f}%"
                color = "up" if pct > 0 else ("down" if pct < 0 else "flat")
            else:
                price = it.price or "—"
                pct_s = "—"
                color = "flat"
            name = it.name or ""
            if not it.verified and it.code:
                name = f"{name} ⚠"
            row = ("even" if i % 2 == 0 else "odd", color)
            self.tree.insert("", "end", iid=it.code, values=(name, price, pct_s),
                             tags=row)
            shown += 1
        # 新列表到了：没选过就自动选第一只 A 股标的。
        # 不能选第一行 —— 第一行可能是 MSFT（美股），没有K线数据，
        # 用户会看到图表区空着以为坏了。
        if not cur:
            pick = None
            for iid in self.tree.get_children():
                if str(iid).startswith(("sh", "sz", "bj")):
                    pick = iid
                    break
            if pick:
                self.tree.selection_set(pick)
                self.tree.focus(pick)
                self._on_select()
        title = payload.get("window") or "同花顺"
        self.st_quote.config(text=f"自选股 {shown} 只 · 「{title}」")

    def _render_watchlist_err(self, msg: str) -> None:
        self.tree.delete(*self.tree.get_children())
        self.tree.insert("", "end",
                         values=("同花顺没在跑？终端跑 python run.py ths", "—", "—"),
                         tags=("flat", "odd"))
        self.st_quote.config(text="自选股读不到（同花顺窗口不在）")

    def _render_overview(self, d: dict) -> None:
        if "state_err" in d:
            self.lb_acct.config(text="账户读取失败", fg=RED)
        else:
            self.lb_acct.config(
                text=f"现金  ¥{d.get('cash', 0):,.2f}    持仓 {d.get('n_pos', 0)} 只\n"
                     f"已实现 ¥{d.get('pnl', 0):+,.2f}    本月往返 {d.get('rounds', 0)} 次",
                fg=FG)
        price = d.get("quote_price")
        plan_txt = "农行 601288 · 月投 100→1000 · 13 年"
        if price:
            plan_txt += f"\n现价 {price} · 今日 {d.get('quote_pct', 0):+.2f}%"
        self.lb_plan.config(text=plan_txt, fg=FG)
        runs = d.get("train_runs", 0)
        self.lb_train.config(
            text=f"训练 {runs} 次 / {d.get('train_days', 0)} 天 · "
                 f"待办 {d.get('todo_open', 0)} 条",
            fg=FG)
        self.lb_spend.config(
            text=f"今日 ¥{d.get('spend_day', 0):.3f} · "
                 f"本月 ¥{d.get('spend_month', 0):.2f} / ¥{d.get('spend_cap', 30):.0f}",
            fg=FG)
        self._draw_breadth(d.get("breadth"))
        txt, color = _age_text(d.get("quote_ts"))
        self.st_quote.config(text=f"行情 {txt}", fg=color)
        self.st_todo.config(
            text=f"待办 {d.get('todo_open', 0)}（累计 {d.get('todo_total', 0)}）")
        self.st_upd.config(text=f"更新于 {d.get('at', '')}")

    def _draw_breadth(self, b) -> None:
        c = self.breadth
        c.delete("all")
        w = c.winfo_width() or 1000
        if not b or not b.get("total"):
            c.create_text(w // 2, 11,
                          text="暂无全市场快照（终端跑 run.py dip 生成）",
                          fill=DIM, font=("Microsoft YaHei", 8))
            return
        total = b["total"]
        red_w = int(w * b["up"] / total)
        grn_w = int(w * b["down"] / total)
        if red_w > 0:
            c.create_rectangle(0, 0, red_w, 22, fill="#3d1513", outline="")
            c.create_text(max(red_w // 2, 40), 11, anchor="center",
                          text=f"上涨 {b['up']}", fill=RED,
                          font=("Microsoft YaHei", 8))
        if grn_w > 0:
            x0 = red_w
            x1 = red_w + grn_w
            c.create_rectangle(x0, 0, x1, 22, fill="#0e2a1f", outline="")
            c.create_text(x0 + max(grn_w // 2, 40), 11, anchor="center",
                          text=f"下跌 {b['down']}", fill=GREEN,
                          font=("Microsoft YaHei", 8))
        ts = str(b.get("ts") or "")
        ts = ts[8:14] if len(ts) >= 12 else ""
        c.create_text(w - 90, 11, text=f"快照 {ts}" if ts else "快照",
                      fill=DIM, font=("Microsoft YaHei", 8))

    def _popup_find(self, payload: dict) -> None:
        r = payload.get("result")
        top = tk.Toplevel(self.root)
        top.title(f"找标的：{payload.get('q', '')}")
        top.geometry("640x360+260+140")
        top.configure(bg=BG)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=BG_PANEL,
                        fieldbackground=BG_PANEL, foreground=FG,
                        rowheight=26, font=("Microsoft YaHei", 10))
        style.configure("Treeview.Heading", background=BG_HEAD, foreground=DIM,
                        font=("Microsoft YaHei", 9, "bold"))
        cols = ("code", "name", "price", "score", "why")
        t = ttk.Treeview(top, columns=cols, show="headings")
        for c, (txt, w) in {"code": ("代码", 90), "name": ("名称", 160),
                            "price": ("现价", 80), "score": ("相关度", 70),
                            "why": ("说明", 220)}.items():
            t.heading(c, text=txt)
            t.column(c, width=w, anchor="w" if c in ("name", "why") else "e")
        t.tag_configure("up", foreground=RED)
        t.tag_configure("down", foreground=GREEN)
        t.pack(fill="both", expand=True, padx=6, pady=6)
        if r and r.hits:
            for h in r.hits:
                tag = "flat"
                price = "—"
                if h.price:
                    price = f"{h.price:.3f}".rstrip("0").rstrip(".")
                    tag = "up" if h.price > 0 else "flat"
                t.insert("", "end", values=(h.code, h.name, price,
                                             f"{h.score:.0%}", h.why or ""),
                         tags=(tag,))
            note = r.note or ""
        else:
            t.insert("", "end", values=("—", "没找到", "", "", ""))
            note = "换个词试试，比如「防守型」「银行」「红利」"
        tk.Label(top, text=note, bg=BG, fg=DIM, font=("Microsoft YaHei", 8),
                 anchor="w", wraplength=600).pack(fill="x", padx=8, pady=(0, 6))

    def run(self) -> None:
        self.root.mainloop()


def run(cfg: Config) -> None:
    Workbench(cfg).run()
