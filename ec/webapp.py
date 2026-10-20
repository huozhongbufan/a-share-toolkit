"""本地 Web 工作台后端。

只用一个文件、只用 Python 标准库（http.server / threading / json / urllib）。
零第三方依赖 —— 这是本项目一贯的约束，不为了一个页面引入 Flask。

安全（必须读）
-------------
只绑定 127.0.0.1。历史教训：v3 项目（ai_trader_v3）曾经
`WEB_HOST = "0.0.0.0"` 且无任何鉴权，把账户状态直接暴露到公网。
这个工作台永远只服务本机；真需要远程访问时，自己去开 ssh 隧道。

设计
----
· GET  /api/*         读状态，全部返回 JSON
· POST /api/train     触发后台线程跑一次训练（run_flow save_todos=False，
                      不落真实待办），不阻塞请求本身
· GET  /api/train/status  查训练任务状态（模块级变量，服务重启就空）
· 静态文件从 ../web/ 目录服务（前端文件由并行任务生成，缺失时显式 404）

错误原则
--------
任何子块失败只把对应字段置 null/空，绝不让整个接口 500 假装成功。
JSON 全部 ensure_ascii=False。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# serve(cfg) 里赋值；handler 全程引用它。
_CFG = None

# 训练任务状态。模块级变量，服务重启就空。
_TRAIN_LOCK = threading.Lock()
_TRAIN_RUNNING = False
_LAST_TRAIN = None

# 默认定投节奏（与 docs/基金计划.md 一致）。
_DCA_SCHEDULE = [100, 300, 400, 500, 500, 700, 900, 1000,
                 1000, 1000, 1000, 1000, 1000]


def _get_cfg():
    """serve() 传入的配置；没调用 serve 时退回 load_config()。"""
    if _CFG is not None:
        return _CFG
    from .config import load_config
    return load_config()


def _q(qs: dict, key: str, default: str = "") -> str:
    """取 query 参数。parse_qs 返回 list，这里取第一个。"""
    vals = qs.get(key) or []
    return vals[0] if vals else default


def _age_level(ts: str) -> tuple:
    """行情时间戳 → (可读文本, 年龄秒, 等级)。

    等级：<300 秒 ok / <3600 秒 warn / 更久 bad；解析不了 dim。
    这条必须基于 collect() 的 quote_ts 算 —— 早期面板版本拿「上次训练记录的
    时间」冒充行情时间，数字是真的、标签是假的，那是最危险的一种误导。
    """
    try:
        dt = datetime.strptime(str(ts), "%Y%m%d%H%M%S")
    except (TypeError, ValueError):
        return None, None, "dim"
    age = int((datetime.now() - dt).total_seconds())
    if age < 60:
        label = f"{age} 秒前"
    elif age < 3600:
        label = f"{age // 60} 分钟前"
    else:
        label = f"{age / 3600:.1f} 小时前"
    text = f"{dt:%m-%d %H:%M:%S} · {label}"
    if age < 300:
        level = "ok"
    elif age < 3600:
        level = "warn"
    else:
        level = "bad"
    return text, age, level


def _models_block() -> dict:
    """当前加载在显存里的模型。"""
    from . import warm
    ls = warm.loaded()
    names = [m.get("name") for m in ls]
    return {
        "loaded": names,
        # 全部钉住才算 pinned；有任何一个会过期就报 false
        "pinned": bool(names) and all(m.get("pinned") for m in ls),
    }


def _plan_block(cfg, d: dict) -> dict:
    """路 A 定投计划摘要，用实时价现算（约 0.01 秒，无需缓存）。"""
    from . import longplan
    px = d.get("quote_price")
    note_extra = ""
    if not px:
        px = 7.3
        note_extra = "（行情价缺失，用 7.3 估算）"
    pr = longplan.simulate(
        cfg, code="sh601288", name="农业银行", price=float(px),
        monthly_by_year=list(_DCA_SCHEDULE),
    )
    return {
        "code": pr.code,
        "name": pr.name,
        "years": len(pr.rows),
        "invest": round(pr.total_cash, 0),
        "final_value": round(pr.final_value, 0),
        "monthly_dividend": round(pr.final_dividend_year / 12, 0),
        "fees": round(pr.total_fees, 0),
        "price": px,
        "note": "整手+佣金模拟" + note_extra,
    }


def train_status() -> dict:
    """训练任务当前状态。last 来自最近一次 POST /api/train 的结果。"""
    return {"running": _TRAIN_RUNNING, "last": _LAST_TRAIN}


def _train_worker(cfg) -> None:
    """后台线程：跑一次训练流水线并记录。异常也记进 _LAST_TRAIN。"""
    global _TRAIN_RUNNING, _LAST_TRAIN
    from .pipeline import run_flow
    from . import train

    t0 = time.time()
    try:
        # save_todos=False 是训练模式：完整跑一遍但不落真实待办。
        result = run_flow(cfg, save_todos=False)
        elapsed = time.time() - t0
        rec_err = None
        try:
            train.record(cfg, result, elapsed)
        except Exception as exc:
            rec_err = str(exc)[:200]
        _LAST_TRAIN = {
            "headline": result.headline,
            "headline_level": result.headline_level,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(elapsed, 1),
            "stages": [
                {"no": s.no, "name": s.name, "status": s.status,
                 "detail": (s.detail or "")[:300]}
                for s in result.stages
            ],
            "record_error": rec_err,
            "error": None,
        }
    except Exception as exc:
        _LAST_TRAIN = {
            "headline": None,
            "headline_level": "error",
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(time.time() - t0, 1),
            "stages": [],
            "record_error": None,
            "error": str(exc)[:200],
        }
    finally:
        _TRAIN_RUNNING = False


class Handler(BaseHTTPRequestHandler):
    server_version = "etf-copilot-webapp/1.0"

    # ---------------------------------------------------------- 鉴权

    def _authorized(self) -> bool:
        """/api/ 路径的鉴权。

        设计：默认只绑 127.0.0.1，本机请求永远放行。
        用户要手机访问时显式 --host 0.0.0.0 --token xxx：
          · 来自 127.0.0.1 的**直接**请求不需要 token（电脑自己用）
          · 来自局域网其它设备的请求必须带 ?token=xxx
          · 没 token 或 token 不对 → 401

        隧道（cpolar 等）的坑
        --------------------
        隧道是「本地进程连到 127.0.0.1 再转发」，请求到达本服务时
        client_address 是 127.0.0.1 —— 只按源地址判断会把公网流量当本机放行，
        等于公网 URL 免口令裸奔。所以：**只要带了 X-Forwarded-For 头**
        （cpolar 会给真实客户端 IP），就必须验 token，不看源地址。

        v3 的教训：0.0.0.0 无鉴权 = 账户信息裸奔。口令至少把「随手扫端口的人」
        挡在门外；它不是银行级安全，但比裸奔强得多。
        """
        token = getattr(self.server, "token", "")
        if not token:
            return True
        fwd = self.headers.get("X-Forwarded-For", "").strip()
        if fwd:
            # 经过隧道：原始客户端在公网，必须带 token
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            got = (qs.get("token") or [""])[0]
            return got == token
        client = (self.client_address or ["?"])[0]
        if client in ("127.0.0.1", "::1"):
            return True
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        got = (qs.get("token") or [""])[0]
        return got == token

    # ---------------------------------------------------------- 基础发送

    def _send(self, code: int, body,
              ctype: str = "application/json; charset=utf-8") -> None:
        if isinstance(body, bytes):
            data = body
        else:
            # default=str 兜底：万一哪个对象没转干净，返回字符串而不是 500
            data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 浏览器提前断开（比如点了刷新）。不是错误，静默收场。
            pass

    def _json(self, obj) -> None:
        self._send(200, obj)

    def _err(self, msg, code: int = 500) -> None:
        self._send(code, {"error": str(msg)[:200]})

    def log_message(self, fmt, *args):
        print(f"[webapp] {self.log_date_time_string()} {fmt % args}")

    # ---------------------------------------------------------- 静态

    @staticmethod
    def _web_root() -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "web")

    def _static(self, name: str, ctype: str) -> None:
        fp = os.path.join(self._web_root(), name)
        if not os.path.exists(fp):
            # 前端文件由并行任务生成。缺失时显式 404，别返回半页 HTML。
            self._send(404, {"error": f"静态文件缺失: {name}"})
            return
        with open(fp, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # ---------------------------------------------------------- 路由

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            if path == "/":
                self._static("index.html", "text/html; charset=utf-8")
            elif path == "/style.css":
                self._static("style.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._static("app.js", "application/javascript; charset=utf-8")
            elif path.startswith("/api/") and not self._authorized():
                self._send(401, {"error": "需要 ?token=口令"})
            elif path == "/api/overview":
                self._api_overview()
            elif path == "/api/todos":
                self._api_todos(qs)
            elif path == "/api/find":
                self._api_find(qs)
            elif path == "/api/ask":
                self._api_ask(qs)
            elif path == "/api/train/status":
                self._json(train_status())
            elif path == "/api/look":
                self._api_look()
            elif path == "/api/warm":
                self._api_warm()
            elif path == "/api/indices":
                self._api_indices()
            elif path == "/api/breadth":
                self._api_breadth()
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            self._err(exc)

    def do_POST(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/api/") and not self._authorized():
                self._send(401, {"error": "需要 ?token=口令"})
            elif parsed.path == "/api/train":
                self._api_train_start()
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            self._err(exc)

    # ---------------------------------------------------------- API

    def _api_overview(self):
        cfg = _get_cfg()
        out = {"fresh": None, "account": None, "todos": None, "spend": None,
               "train": None, "models": None, "plan": None, "at": None}
        try:
            from .dock import collect
            d = collect(cfg)
        except Exception as exc:
            out["error"] = str(exc)[:200]
            self._json(out)
            return

        out["at"] = d.get("at")

        # 行情新鲜度
        ts = d.get("quote_ts")
        if ts:
            text, age, level = _age_level(ts)
            out["fresh"] = {"text": text, "age_sec": age, "level": level,
                            "price": d.get("quote_price"),
                            "pct": d.get("quote_pct"), "err": None}
        elif d.get("quote_err"):
            out["fresh"] = {"text": None, "age_sec": None, "level": "bad",
                            "price": None, "pct": None,
                            "err": d["quote_err"]}
        else:
            out["fresh"] = {"text": None, "age_sec": None, "level": "dim",
                            "price": None, "pct": None, "err": None}

        # 各子块独立 try —— 一块挂了只置空，不整体 500
        def _sub(key, fn):
            try:
                out[key] = fn()
            except Exception as exc:
                out[key] = {"error": str(exc)[:100]}

        _sub("account", lambda: {
            "cash": d.get("cash"),
            "positions": d.get("n_pos"),
            "realized_pnl": d.get("pnl"),
            "month_rounds": d.get("rounds"),
        })
        _sub("todos", lambda: {
            "open": d.get("todo_open"),
            "total": d.get("todo_total"),
            "done": d.get("todo_done"),
            "rejected": d.get("todo_rej"),
        })
        _sub("spend", lambda: {
            "today": d.get("spend_day"),
            "month": d.get("spend_month"),
            "cap": d.get("spend_cap"),
        })
        _sub("train", lambda: {
            "runs": d.get("train_runs"),
            "days": d.get("train_days"),
            "top_gate": d.get("top_gate") or "",
        })
        _sub("models", _models_block)
        _sub("plan", lambda: _plan_block(cfg, d))
        self._json(out)

    def _api_todos(self, qs):
        cfg = _get_cfg()
        try:
            limit = int(_q(qs, "limit", "20"))
        except ValueError:
            limit = 20
        from .todos import TodoStore
        store = TodoStore(cfg)
        opened = [t.to_dict() for t in store.open_todos()]
        closed_all = [t for t in store.all() if not t.is_open]
        # 最近 closed limit 条，新在前
        closed = [t.to_dict() for t in reversed(closed_all[-limit:])]
        self._json({"open": opened, "closed": closed})

    def _api_find(self, qs):
        q = _q(qs, "q")
        if not q:
            self._send(400, {"error": "缺少参数 q"})
            return
        try:
            top = int(_q(qs, "top", "8"))
        except ValueError:
            top = 8
        from . import ths_scout
        res = ths_scout.search(_get_cfg(), q, top_k=top)
        self._json({
            "query": res.query,
            "note": res.note,
            "hits": [{
                "code": h.code, "name": h.name, "kind": h.kind,
                "price": h.price, "score": h.score,
                "matched": h.matched, "why": h.why, "source": h.source,
            } for h in res.hits],
        })

    def _api_ask(self, qs):
        q = _q(qs, "q")
        if not q:
            self._send(400, {"error": "缺少参数 q"})
            return
        model = _q(qs, "model", "qwen3.5:4b")
        from . import tools
        # 原样透传：{answer, rounds, calls, elapsed, model} 或 {answer:"", error}
        result = tools.ask(_get_cfg(), q, model=model)
        self._json(result)

    def _api_look(self):
        from . import screen
        try:
            cap = screen.capture(target="window", process="hexin")
        except screen.ScreenError as exc:
            msg = str(exc)
            if "没有在运行" in msg or "process_not_running" in msg:
                # 约定错误码：前端按这个字符串判断「同花顺没开」
                self._json({"error": "process_not_running"})
            else:
                self._err(msg)
            return
        try:
            items = screen.read_watchlist(_get_cfg(), cap)
            items = screen.verify_with_api(items)
        except Exception as exc:
            self._err(exc)
            return
        self._json({
            "window": cap.window_title,
            "items": [{
                "code": it.code,
                "name": it.name,
                "api_name": it.api_name,
                "api_price": it.api_price,
                "api_pct": it.api_pct,
                "verified": it.verified,
                "note": it.note,
                "parse_error": it.parse_error,
            } for it in items],
        })

    def _api_warm(self):
        from . import warm
        self._json({"loaded": warm.loaded(), "memory": warm.memory()})

    # 指数条 + 涨跌家数：60 秒模块级缓存，页面每 60 秒刷一次，别每次现抓。
    _IDX_CACHE: dict = {}

    def _api_indices(self):
        """三大指数实时行情（同花顺顶栏那种）。"""
        now = time.time()
        if self._IDX_CACHE.get("at", 0) > now - 60:
            self._json(self._IDX_CACHE["data"])
            return
        try:
            from .quotes import fetch_quotes
            q = fetch_quotes(["sh000001", "sz399001", "sz399006"])
            out = []
            for code in ("sh000001", "sz399001", "sz399006"):
                x = q.get(code)
                if not x:
                    out.append({"code": code, "error": "行情拿不到"})
                    continue
                out.append({
                    "code": code, "name": x.name, "price": x.price,
                    "pct": round(x.pct * 100, 2), "ts": x.ts,
                })
            self._IDX_CACHE = {"at": now, "data": {"indices": out}}
        except Exception as exc:
            self._json({"indices": [], "error": str(exc)[:120]})
            return
        self._json(self._IDX_CACHE["data"])

    def _api_breadth(self):
        """全市场涨跌家数（红涨绿跌条）。

        数据源是 cheap 的全市场快照缓存（quotes_all.json，白天 28 秒抓一次）。
        它是「当天快照」，不是秒级实时 —— 返回里带 snapshot_time，
        前端要显示「快照于 xx:xx」，别装成实时。
        """
        try:
            from . import cheap
            rows = cheap._load_cache(_get_cfg())
            if not rows:
                self._json({"up": None, "down": None, "flat": None,
                            "total": None, "snapshot_time": None,
                            "error": "还没有全市场快照（跑一次 run.py dip 或 scan 就有了）"})
                return
            up = down = flat = 0
            ts = ""
            for code, d in rows.items():
                try:
                    px = float(d.get("price") or 0)
                    prev = float(d.get("prev_close") or 0)
                    if px <= 0 or prev <= 0:
                        continue
                    if px > prev:
                        up += 1
                    elif px < prev:
                        down += 1
                    else:
                        flat += 1
                    ts = str(d.get("ts") or "")
                except (TypeError, ValueError):
                    continue
            self._json({"up": up, "down": down, "flat": flat,
                        "total": up + down + flat, "snapshot_time": ts})
        except Exception as exc:
            self._json({"up": None, "down": None, "flat": None,
                        "total": None, "snapshot_time": None,
                        "error": str(exc)[:120]})

    def _api_train_start(self):
        global _TRAIN_RUNNING
        with _TRAIN_LOCK:
            if _TRAIN_RUNNING:
                self._json({"status": "busy"})
                return
            _TRAIN_RUNNING = True
        threading.Thread(target=_train_worker, args=(_get_cfg(),),
                         daemon=True).start()
        self._json({"status": "started"})


def serve(cfg, host="127.0.0.1", port=8800, token: str = "") -> None:
    """启动。阻塞直到 Ctrl+C。

    安全：
      · 默认只绑 127.0.0.1，本机请求永远放行。
      · 要手机访问：--host 0.0.0.0 --token xxx，则局域网设备必须带
        ?token=xxx 才能碰 /api/（本机仍免 token）。
      · v3 项目曾经 WEB_HOST="0.0.0.0" 且无鉴权，账户信息直接暴露公网。
        这个工作台默认不绑公网；即便用户显式绑 0.0.0.0，也有口令挡着。
        口令是「同 WiFi 防顺手」，不是银行级安全 —— 别把 token 贴到公网。
    """
    global _CFG
    _CFG = cfg
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.token = token or ""
    print("─" * 60)
    print(f"工作台 http://127.0.0.1:{port}")
    if host not in ("127.0.0.1", "localhost"):
        if not token:
            print("⚠ 已绑到局域网但没有口令 —— 同 WiFi 的任何人都能看 /api/。")
            print("  强烈建议带 --token 重启。")
        else:
            print(f"手机访问需要带口令：?token={token}")
    else:
        print("只绑定本机。手机要看请 --host 0.0.0.0 --token 口令")
    print("─" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()
