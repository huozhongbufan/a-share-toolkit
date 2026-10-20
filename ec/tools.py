"""给本地小模型装上实时数据工具。

为什么不用现成的 MCP 服务器
---------------------------
找过三个，都有硬伤：

  · ArvinLovegood/go-stock      7,881★  GPL-3.0，传染性许可，代码不能拿
  · Guling-Pro/guling-trader      21★   AGPL-3.0，而且**实盘下单**，危险
  · a-stock-mcp-server             MIT  可用，但底层是 AKShare，README 自己写
                                         「数据延迟 15-20 分钟」—— 正是要避免的老数据

而本项目的 `quotes.py` 走腾讯 `qt.gtimg.cn`，`ts` 字段精确到秒，盘中实测
就是当下时刻。所以这里不引入任何外部依赖，只用已有的数据层。

为什么不用 MCP 协议
-------------------
ollama 不原生支持 MCP，要额外写桥接。而它从 v0.4 起原生支持 OpenAI 风格的
`tools`，直接给函数定义就能用 —— 少一层中间件，少一处会坏的地方。

关键设计：**让模型传「名字」，不让它猜代码**
--------------------------------------------
第一版工具要求模型自己写 `["sh601288", "sz159841"]`，实测失败：

    qwen2.5:7b  → 传了 sh601280 / 159860     （代码错了两位）
    deepseek-r1 → 没调工具，还编了个 601169  （更糟）

7B 模型记不住 7696 个代码。改成传名字、内部用同花顺表转代码之后就对了：

    调用 get_quote(['农业银行', '证券ETF天弘'])
    → sh601288 7.32 -2.27%   sz159841 0.929 -0.75%

**结论：小模型的工具参数必须是自然语言，不能要求它做代码映射。**
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .config import Config

OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_quote",
        "description": (
            "查询 A 股标的的实时行情（最新价、涨跌幅、成交额、数据时间）。"
            "参数直接传名字或代码原文即可，不需要你转换格式 —— "
            "名字就写「农业银行」，代码就写「601288」。"
            "凡是问价格、涨跌、行情、成交量的问题，都必须先调用这个工具，"
            "绝对不能凭记忆回答。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "标的名称或代码原文，例如 [\"农业银行\", \"601288\"]",
                }
            },
            "required": ["queries"],
        },
    },
}]

SYSTEM = (
    "你是一个 A 股行情查询助手。你有 get_quote 工具可以拿到**实时**行情。\n"
    "规则：\n"
    "1. 任何关于价格、涨跌、成交量的提问，都必须先调用 get_quote。\n"
    "2. 不许凭记忆报价格 —— 你的训练数据里没有今天的行情。\n"
    "3. 工具返回 data_time（可读时间）和 data_age_sec（几秒前）。\n"
    "   data_age_sec 小于 300 就是当下实时数据，**不要说它过时**。\n"
    "   只有明显很大（比如超过 86400，一天以上）才提示可能过时。\n"
    "4. 回答简短，只说你查到的东西，不要补充投资建议。"
)


def resolve(query: str, limit: int = 3) -> list[str]:
    """名字或代码 → 规范代码。优先精确匹配，其次模糊。纯离线。"""
    from .quotes import normalize_code
    from . import ths as T
    from . import ths_scout as S

    q = str(query or "").strip()
    if not q:
        return []

    digits = "".join(ch for ch in q if ch.isdigit())
    if len(digits) == 6:
        try:
            return [normalize_code(digits)]
        except Exception:
            pass

    table = T.tradable_universe()
    hits, _kw, _kf = S.prescreen(q, table, limit=12)
    if not hits:
        return []
    exact = [i.code for i in hits if q == i.name or i.name in q or q in i.name]
    return (exact or [i.code for i in hits[:limit]])[:limit]


def _readable_time(ts: str) -> str:
    """20261020104857 → 2026-10-20 10:48:57。

    必须转换：实测模型看到紧凑格式会判成「可能已经过时」，
    而那一刻其实正是当下。
    """
    s = str(ts or "")
    if len(s) == 14 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
    return s


def _age_sec(ts: str) -> int | None:
    from datetime import datetime

    s = str(ts or "")
    if len(s) != 14 or not s.isdigit():
        return None
    try:
        dt = datetime.strptime(s, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return max(0, int((datetime.now() - dt).total_seconds()))


def get_quote(queries: list[str]) -> list[dict]:
    """工具实现：把名字解析成代码，再拉实时行情。"""
    from .quotes import fetch_quotes

    codes: list[str] = []
    origin: dict[str, str] = {}
    for q in queries:
        for c in resolve(q):
            if c not in codes:
                codes.append(c)
                origin[c] = q

    if not codes:
        return [{"query": str(q), "error": "本地代码表里没找到匹配的标的"} for q in queries]

    try:
        quotes = fetch_quotes(codes)
    except Exception as exc:
        return [{"query": str(q), "error": f"行情接口失败：{exc}"} for q in queries]

    out = []
    for c in codes:
        x = quotes.get(c)
        if not x:
            out.append({"query": origin.get(c, c), "code": c, "error": "行情查不到"})
            continue
        age = _age_sec(x.ts)
        out.append({
            "code": x.code,
            "name": x.name,
            "price": x.price,
            "pct": round(x.pct * 100, 2),
            "amount_wan": round(x.amount_wan),
            "data_time": _readable_time(x.ts),
            "data_age_sec": age,
        })
    return out


def _call(model: str, messages: list[dict], tools: bool,
          timeout: int = 300) -> dict | None:
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.0, "num_predict": 1200},
    }
    if tools:
        body["tools"] = TOOLS
    req = urllib.request.Request(
        OLLAMA_CHAT, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


def ask(cfg: Config, question: str, model: str = "qwen2.5:7b",
        max_rounds: int = 4, trace=None) -> dict:
    """带实时工具的问答。返回 {answer, rounds, calls, elapsed, model}。

    能调工具的（实测）：
      · qwen3.5:4b   ✅ 而且有视觉，一个顶两个 —— 推荐常驻它
      · qwen2.5:7b   ✅ 稍快 0.4 秒，但没有视觉
      · deepseek-r1:8b ❌ 不调工具，直接回「我无法提供实时股价」然后编个代码
    """
    say = trace or (lambda *_: None)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    calls: list[dict] = []
    t0 = time.time()

    for rnd in range(1, max_rounds + 1):
        d = _call(model, messages, tools=True)
        if d is None:
            return {"answer": "", "error": "ollama 没响应或返回异常",
                    "rounds": rnd, "calls": calls,
                    "elapsed": round(time.time() - t0, 1), "model": model}

        msg = d.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        say(f"  [轮 {rnd}] tool_calls={len(tool_calls)}")

        if not tool_calls:
            return {"answer": (msg.get("content") or "").strip(),
                    "rounds": rnd, "calls": calls,
                    "elapsed": round(time.time() - t0, 1), "model": model}

        messages.append(msg)
        for c in tool_calls:
            fn = c.get("function") or {}
            name = fn.get("name")
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            args = args or {}

            if name != "get_quote":
                result = {"error": f"未知工具 {name}"}
            else:
                result = get_quote(args.get("queries") or [])
            calls.append({"tool": name, "args": args, "result": result})
            say(f"    {name}({args.get('queries')}) → {json.dumps(result, ensure_ascii=False)[:200]}")
            messages.append({
                "role": "tool",
                "content": json.dumps(result, ensure_ascii=False),
            })

    return {"answer": "", "error": f"{max_rounds} 轮内模型没给出最终回答（一直在调工具）",
            "rounds": max_rounds, "calls": calls,
            "elapsed": round(time.time() - t0, 1), "model": model}
