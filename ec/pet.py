"""瑶瑶桌宠 —— 用她自己的素材帧（原神瑶瑶，Q版二头身）。

素材：assets/yaoyao/*_238.png（来自用户素材链
C:\\Users\\user\\dev\\项目\\mmd-assets\\参考图\\瑶瑶\\处理\\），
137x260 透明底 PNG，8 个状态：
  idle / blink / happy / angry / think / walka / walkb / work

之前那版是 Canvas 画的圆脸占位 —— 那不算瑶瑶。这版才是她本人。

技术：
  · tkinter + PhotoImage（Tk 8.6 原生支持透明 PNG）
  · 窗口透明用洋红键：Canvas 背景涂 #ff00fe，窗口 -transparentcolor #ff00fe
    精灵透明像素透出洋红 → 窗口级透明
  · 说话气泡：Label，不说话时背景也是洋红（=隐形），说话时白底
  · 所有慢操作在线程，UI 只在主线程动；任何异常都不崩
  · 已知坑：窗口刚启动可能处于隐藏态（父进程 SW_HIDE 会传染），
    构建完成后必须 deiconify + lift 一次
"""
from __future__ import annotations

import queue
import random
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import simpledialog

from .config import Config

MAGENTA = "#ff00fe"
BUBBLE_BG = "#ffffff"
BUBBLE_FG = "#1f2430"

FRAMES = ["idle", "blink", "happy", "angry", "think",
          "walka", "walkb", "work"]


def _asset_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "yaoyao"


class Pet:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.q: queue.Queue = queue.Queue()
        self.state = "idle"
        self.ask_busy = False
        self.walk_phase = "a"
        self._fade_job = None
        self._dragged = False
        self._dx = 0
        self._dy = 0

        self.root = tk.Tk()
        self.root.title("瑶瑶")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", MAGENTA)

        self.frames: dict[str, tk.PhotoImage] = self._load_frames()
        self._build()

        # 父进程用 CREATE_NO_WINDOW / -WindowStyle Hidden 启动时，
        # SW_HIDE 会传染给第一个窗口 → 桌面宠物不可见。
        # 实测 deiconify()+lift() 不够（IsIconic 仍是真），
        # 必须用 Win32 ShowWindow(SW_RESTORE) 硬拉。
        self._force_show()
        self.root.after(400, self._force_show)
        self.root.after(1200, self._force_show)

        self._say("瑶瑶在～")
        self._set_state("happy")
        self.root.after(2200, lambda: self._set_state("idle"))
        self.root.after(300, self._pump)
        self.root.after(1000, self._wander)
        self.root.after(1500, self._schedule_blink)
        self.root.after(2000, self._reminder_tick)

    # ------------------------------------------------------------ 素材

    def _load_frames(self) -> dict[str, tk.PhotoImage]:
        frames: dict[str, tk.PhotoImage] = {}
        d = _asset_dir()
        for name in FRAMES:
            fp = d / f"yaoyao_{name}_238.png"
            try:
                frames[name] = tk.PhotoImage(file=str(fp))
            except Exception:
                continue
        return frames

    # ------------------------------------------------------------ UI

    def _build(self) -> None:
        self.root.configure(bg=MAGENTA)

        # 气泡（不说话时背景洋红=隐形）
        self.bubble = tk.Label(self.root, text="", bg=MAGENTA, fg=BUBBLE_FG,
                               font=("Microsoft YaHei", 10),
                               justify="left", wraplength=220, padx=10, pady=6)
        self.bubble.pack(side="top", anchor="w", padx=6, pady=(6, 0))

        # 精灵画布
        self.canvas = tk.Canvas(self.root, width=160, height=260,
                                bg=MAGENTA, highlightthickness=0)
        self.canvas.pack(side="top")

        if self.frames:
            self.sprite_id = self.canvas.create_image(
                80, 130, image=self.frames.get("idle")
                or next(iter(self.frames.values())))
        else:
            # 素材缺失的兜底：不崩，画个占位并说清楚
            self.sprite_id = self.canvas.create_text(
                80, 130, text="素材缺失\nassets/yaoyao/",
                fill="#999999", font=("Microsoft YaHei", 10))
            self._say("瑶瑶找不到自己的素材了（assets/yaoyao/）")

        # 拖拽 + 点击
        for w in (self.canvas, self.bubble):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Button-3>", self._on_right)
            w.bind("<Double-Button-1>", self._on_double)
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

    # ------------------------------------------------------------ 状态机

    def _set_state(self, state: str) -> None:
        self.state = state
        img = self.frames.get(state)
        if img is not None:
            try:
                self.canvas.itemconfig(self.sprite_id, image=img)
            except Exception:
                pass

    def _schedule_blink(self) -> None:
        if self.state == "idle" and "blink" in self.frames:
            self._set_state("blink")
            self.root.after(150, lambda: self._set_state("idle"))
        self.root.after(random.randint(3000, 8000), self._schedule_blink)

    def _say(self, text: str, hold_ms: int = 6000) -> None:
        try:
            self.bubble.config(text=text, bg=BUBBLE_BG)
        except Exception:
            pass
        if self._fade_job:
            try:
                self.root.after_cancel(self._fade_job)
            except Exception:
                pass
        self._fade_job = self.root.after(hold_ms, self._fade_out)

    def _fade_out(self) -> None:
        try:
            self.bubble.config(bg=MAGENTA)
            self.root.after(300, lambda: self.bubble.config(text=""))
        except Exception:
            pass

    # ------------------------------------------------------------ 游走

    def _wander(self) -> None:
        if self._dragged:
            self.root.after(4000, self._wander)
            return
        if self.state == "idle":
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            h = self.canvas.winfo_height() or 260
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            newx = x + random.choice([-1, 1]) * random.randint(6, 20)
            newx = max(0, min(sw - 170, newx))
            newy = min(sh - h - 60, max(0, y + random.choice([0, 0, 2, -2])))
            self._set_state("walka" if self.walk_phase == "b" else "walkb")
            self.walk_phase = "a" if self.walk_phase == "b" else "b"
            try:
                self.root.geometry(f"+{newx}+{newy}")
            except Exception:
                pass
            self.root.after(320, self._wander)
        else:
            self.root.after(1000, self._wander)

    # ------------------------------------------------------------ 交互

    def _drag_start(self, e) -> None:
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()
        self._dragged = True

    def _drag_move(self, e) -> None:
        try:
            self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")
        except Exception:
            pass

    def _on_click(self, _e) -> None:
        if self.state != "work":
            self._set_state("work")
            self.root.after(1200, lambda: self._set_state("idle"))
        self._say("瑶瑶去看看…")

        def job():
            try:
                from .dock import collect
                d = collect(self.cfg)
                ts = d.get("quote_ts")
                if ts:
                    try:
                        dt = datetime.strptime(str(ts), "%Y%m%d%H%M%S")
                        age = (datetime.now() - dt).total_seconds()
                        t = (f"{dt:%H:%M:%S} · {int(age)}秒前"
                             if age < 300 else f"{dt:%H:%M:%S}")
                    except ValueError:
                        t = ts
                else:
                    t = "行情拿不到"
                price = d.get("quote_price")
                pct = d.get("quote_pct", 0)
                line = f"行情 {t}"
                if price:
                    line += f"｜农行 {price} {pct:+.2f}%"
                line += (f"\n待办 {d.get('todo_open', 0)} · "
                         f"现金 ¥{d.get('cash', 0):,.0f} · "
                         f"今日花费 ¥{d.get('spend_day', 0):.3f}")
                return {"text": line}
            except Exception as exc:
                return {"text": f"瑶瑶没连上数据：{str(exc)[:40]}"}
        self._thread(job, "status")

    def _on_double(self, _e) -> None:
        if self.ask_busy:
            return
        q = simpledialog.askstring("问瑶瑶", "问瑶瑶什么？（行情、板块都行）",
                                   parent=self.root)
        if not q:
            return
        self.ask_busy = True
        self._set_state("think")
        self._say(f"瑶瑶在查「{q[:18]}」…", hold_ms=9000)

        def job():
            from . import tools
            return tools.ask(self.cfg, q, model="qwen3.5:4b")
        self._thread(job, "ans")

    def _on_right(self, _e) -> None:
        menu = tk.Menu(self.root, tearoff=0,
                       bg="#171b21", fg="#e6e8eb",
                       activebackground="#2a3140")
        menu.add_command(label="状态", command=self._on_click_menu)
        menu.add_command(label="问行情", command=self._on_double_menu)
        menu.add_separator()
        menu.add_command(label="退出", command=self.root.destroy)
        try:
            menu.tk_popup(self.root.winfo_pointerx(),
                          self.root.winfo_pointery())
        finally:
            menu.grab_release()

    def _on_click_menu(self):
        self._on_click(None)

    def _on_double_menu(self):
        self._on_double(None)

    # ------------------------------------------------------------ 提醒

    def _reminder_tick(self) -> None:
        def job():
            try:
                from .todos import TodoStore
                return {"open": len(TodoStore(self.cfg).open_todos())}
            except Exception:
                return {"open": 0}
        self._thread(job, "remind")
        self.root.after(10 * 60 * 1000, self._reminder_tick)

    # ------------------------------------------------------------ 线程与主循环

    def _thread(self, fn, tag: str) -> None:
        def work():
            try:
                payload = fn()
                if payload is not None:
                    self.q.put((tag, payload))
            except Exception as exc:
                self.q.put((f"{tag}_err", str(exc)[:120]))

        threading.Thread(target=work, daemon=True).start()

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    def _handle(self, kind: str, payload) -> None:
        if kind == "status":
            self._say(payload.get("text", "瑶瑶没看清"))
        elif kind == "status_err":
            self._set_state("angry")
            self._say(f"瑶瑶没连上数据：{payload}", hold_ms=5000)
            self.root.after(2600, lambda: self._set_state("idle"))
        elif kind == "ans":
            self.ask_busy = False
            ans = (payload.get("answer") or payload.get("error")
                   or "瑶瑶没答上来")
            self._set_state("happy")
            self._say(ans[:160] + ("…" if len(ans) > 160 else ""), 12000)
            self.root.after(2600, lambda: self._set_state("idle"))
        elif kind == "ans_err":
            self.ask_busy = False
            self._set_state("angry")
            self._say(f"瑶瑶问不到：{payload}", hold_ms=6000)
            self.root.after(2600, lambda: self._set_state("idle"))
        elif kind == "remind":
            n = payload.get("open", 0)
            if n > 0:
                self._set_state("think")
                self._say(f"有 {n} 条待办没处理，去工作台看看～", 10000)
                self.root.after(3000, lambda: self._set_state("idle"))

    def run(self) -> None:
        self.root.mainloop()


def run(cfg: Config) -> None:
    Pet(cfg).run()
