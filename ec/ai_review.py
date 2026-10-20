"""多模型交叉复核。

AI 在这个系统里的角色是**风控复核员**,不是预测器。这条界线是刻意的:

  - 买卖方向由 strategy 的确定性规则决定,可回测、可复现
  - AI 不参与选股,不参与择时,不输出目标价
  - AI 只能做一件事:在规则信号之上挑毛病,并且可以否决
  - AI 没有任何路径可以加大仓位或放宽风控

为什么这样设计:如果让 AI 决定买卖,就无法区分『策略有效』和『模型这次猜对了』,
而且多模型之间本来就会互相矛盾。让它当审稿人,它的价值才是可验证的。

失败语义很重要:**AI 调用失败 = 弃权,不等于同意**。全部模型失败时状态是
unavailable,工单上会明确写出来,不会伪装成『复核通过』。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict

from .config import Config, AIModel

# 从隔离 home 的 config.toml 里取凭据用的正则
_TOKEN_RE = re.compile(r'experimental_bearer_token\s*=\s*"([^"]+)"')

APPROVE = "approve"
CAUTION = "caution"
REJECT = "reject"
ABSTAIN = "abstain"

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": [APPROVE, CAUTION, REJECT],
            "description": "approve=这个建议可以执行; caution=可以执行但要盯紧某个风险; reject=不该执行",
        },
        "confidence": {
            "type": "number",
            "description": "0 到 1,你对自己这个判断的把握",
        },
        "top_risk": {
            "type": "string",
            "description": "这个建议最大的一个风险点,一句话",
        },
        "reason": {
            "type": "string",
            "description": "你的判断依据,基于给出的数据,不要泛泛而谈",
        },
        "suggested_fix": {
            "type": "string",
            "description": "如果让你改一处,改什么。没有就写 none",
        },
    },
    "required": ["verdict", "confidence", "top_risk", "reason", "suggested_fix"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """你是一名独立风控复核员。有人拿一份已经算好的 ETF 建仓建议来找你签字。

你的职责边界,请严格遵守:
1. 你的任务**不是**预测涨跌,也不是判断现在是不是好买点。你没有能力做这件事,也不要假装能做。
2. 你的任务是找出这份建议里的**结构性缺陷**:数据够不够、假设站不站得住、风控有没有漏洞、
   这笔交易在数学上划不划算、最坏情况下会发生什么。
3. 你只能给出 approve / caution / reject 三种结论之一,以及一个 0 到 1 的把握度。
4. 你**不能**建议加大仓位、放宽风控阈值、或推荐别的标的。你只有否决权,没有加码权。
5. 如果数据不足以判断,就诚实说数据不足并给 caution,不要编造理由。

判断时请特别注意这几点:
- 这是一个约 500 元人民币的小账户,1 手 = 100 份,仓位粒度极粗,基本无法分散。
- 场内 ETF 免印花税,主要成本是佣金;如果券商有『最低 5 元佣金』,往返成本可能占到本金的 2%。
- A 股股票型 ETF 是 T+1,当天买入当天不能卖;跨境/黄金/债券 ETF 才是 T+0。
- 只有一份日线数据,没有分钟级、没有盘口深度、没有基本面。

请基于给出的数据作答,不要引入你没有看到的数字。用中文回答。"""


@dataclass
class ReviewResult:
    model_id: str
    kind: str
    status: str                 # ok | timeout | error | unavailable | skipped_budget
    verdict: str = ABSTAIN
    confidence: float = 0.0
    top_risk: str = ""
    reason: str = ""
    suggested_fix: str = ""
    elapsed_sec: float = 0.0
    cost_usd: float = 0.0
    tokens: int | None = None   # 直连时的 total_tokens，用来核对花费
    error: str = ""

    @property
    def counts_as_approval(self) -> bool:
        return self.status == "ok" and self.verdict in (APPROVE, CAUTION)

    @property
    def is_veto(self) -> bool:
        return self.status == "ok" and self.verdict == REJECT

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewBoard:
    results: list[ReviewResult] = field(default_factory=list)
    approvals: int = 0
    vetoes: int = 0
    abstentions: int = 0
    status: str = "ok"          # ok | insufficient | unavailable
    decision: str = ""          # allow | block
    summary: str = ""

    @property
    def total_cost_usd(self) -> float:
        return round(sum(r.cost_usd for r in self.results), 4)

    @property
    def total_elapsed(self) -> float:
        return round(sum(r.elapsed_sec for r in self.results), 1)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "decision": self.decision,
            "approvals": self.approvals,
            "vetoes": self.vetoes,
            "abstentions": self.abstentions,
            "summary": self.summary,
            "total_cost_usd": self.total_cost_usd,
            "total_elapsed": self.total_elapsed,
            "results": [r.to_dict() for r in self.results],
        }


def _ollama_exe() -> str | None:
    p = shutil.which("ollama")
    if p:
        return p
    fallback = os.path.expanduser(r"~\AppData\Local\Programs\Ollama\ollama.exe")
    return fallback if os.path.exists(fallback) else None


def _claude_exe() -> str | None:
    p = shutil.which("claude")
    if p:
        return p
    fallback = os.path.expanduser(r"~\.local\bin\claude.exe")
    return fallback if os.path.exists(fallback) else None


def build_brief(cfg: Config, context: dict) -> str:
    """把决策所需的事实压成一段紧凑文本。只给数字,不给原文。"""
    lines = ["# 待复核的建仓建议", ""]
    lines.append("## 账户")
    acc = context.get("account", {})
    lines.append(f"总本金 {acc.get('total_capital')} 元,当前现金 {acc.get('cash')} 元,"
                 f"当前持仓市值 {acc.get('market_value')} 元")
    lines.append(f"本月已完成往返交易 {acc.get('month_round_trips')} 次")
    lines.append("")

    f = context.get("fees", {})
    lines.append("## 券商费率(用户填写)")
    lines.append(f"佣金率 {f.get('commission_rate')},最低佣金 {f.get('min_commission')} 元,"
                 f"印花税 {f.get('stamp_duty_rate')},滑点 {f.get('slippage_bps')} bp")
    lines.append("")

    s = context.get("signal", {})
    lines.append("## 建议操作")
    lines.append(f"标的 {s.get('code')} {s.get('name')}"
                 f"{'(T+0)' if s.get('t0') else '(T+1)'}")
    lines.append(f"现价 {s.get('price')} 元,1 手(100份)成本 {s.get('lot_cost')} 元")
    lines.append(f"计划买入 {s.get('shares')} 份,金额 {s.get('notional')} 元")
    lines.append(f"止损位 {s.get('stop')},止盈位 {s.get('target')}")
    lines.append("")

    lines.append("## 技术面事实(日线,前复权)")
    lines.append(f"MA{s.get('ma_fast_win')} = {s.get('ma_fast')},MA{s.get('ma_slow_win')} = {s.get('ma_slow')}")
    lines.append(f"ATR14 = {s.get('atr')}(占价格 {float(s.get('atr_pct') or 0)*100:.2f}%)")
    lines.append(f"RSI14 = {s.get('rsi')}")
    lines.append(f"{s.get('mom_window')}日动量 = {float(s.get('mom') or 0)*100:+.2f}%")
    lines.append(f"价格处于过去 {s.get('pct_window')} 日的 {float(s.get('pct_rank') or 0)*100:.0f}% 分位")
    lines.append(f"当日成交额 {s.get('amount_wan')} 万元,买卖价差 {float(s.get('spread_pct') or 0)*100:.3f}%")
    lines.append(f"策略打分 {s.get('score')}(仅用于排序,不代表预期收益)")
    lines.append("")

    lines.append("## 最近 20 个交易日收盘价")
    closes = s.get("recent_closes") or []
    lines.append(", ".join(str(c) for c in closes))
    lines.append("")

    lines.append("## 费用算术")
    ec = context.get("economics", {})
    lines.append(f"单边买入费用 {ec.get('buy_cost')} 元,往返总费用 {ec.get('round_trip_cost')} 元")
    # 明细必须写出来。只给总额时模型会自己重算，一发现和自己算的对不上
    # 就会判定「费用算术自相矛盾」—— 实测 Claude 就这么误判过一次，
    # 差额其实来自滑点。
    lines.append(
        f"费用明细(单边): 佣金 {ec.get('commission_one_way')} 元"
        f"(最低佣金{'已' if ec.get('min_commission_bound') else '未'}触发) "
        f"＋ 滑点 {ec.get('slippage_one_way')} 元({ec.get('slippage_bps')} bp)"
        f"＝ {ec.get('buy_cost')} 元。**上述所有费用数字均已含滑点。**")
    lines.append(f"往返费用占本笔金额 {float(ec.get('fee_ratio') or 0)*100:.3f}%")
    lines.append(f"买入后需要上涨 {float(ec.get('breakeven_pct') or 0)*100:.3f}% 才回到成本")
    lines.append(f"止损距离代表的潜在亏损 {ec.get('risk_amount')} 元,"
                 f"是往返费用的 {ec.get('risk_cost_multiple')} 倍")
    if ec.get("required_win_rate"):
        lines.append(f"按「止损净亏 / 止盈净赚」倒推,打平所需胜率 "
                     f"{float(ec['required_win_rate'])*100:.1f}%")
    lines.append("")

    lines.append("## 规则风控闸门的结论")
    for c in context.get("checks", []):
        mark = {"ok": "通过", "warn": "警告", "block": "拦截"}.get(c.get("level"), "?")
        lines.append(f"- [{mark}] {c.get('name')}: {c.get('detail')}")
    lines.append("")

    lines.append("## 策略给出的买入理由")
    for r in context.get("reasons", []):
        lines.append(f"- {r}")
    if context.get("warnings"):
        lines.append("")
        lines.append("## 策略自己标注的警告")
        for w in context["warnings"]:
            lines.append(f"- {w}")
    lines.append("")
    lines.append("请按 schema 输出你的复核结论。")
    return "\n".join(lines)


JSON_HINT = """只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释文字。字段：
{

  "verdict": "approve | caution | reject",
  "confidence": 0.0 到 1.0 的数字,
  "top_risk": "最大的一个风险点，一句话",
  "reason": "判断依据，必须引用给出的具体数字",
  "suggested_fix": "如果让你改一处，改什么；没有就写 none"
}

verdict 只允许 approve / caution / reject 三个值之一。"""


def _read_key(model: AIModel) -> str:
    """从隔离 home 的 config.toml 里读 experimental_bearer_token。

    为什么不读 auth.json：实测那三份 auth.json 里放的是同一把 OpenAI key，
    对 deepseek / moonshot 都是 401。真正的凭据在 config.toml 的
    experimental_bearer_token 里。
    """
    if not model.key_from:
        return ""
    p = os.path.expanduser(model.key_from)
    if not os.path.exists(p):
        return ""
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        txt = fh.read()
    m = _TOKEN_RE.search(txt)
    return m.group(1) if m else ""


def _strip_fence(text: str) -> str:
    """模型偶尔会把 JSON 包在 ```json ... ``` 里。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _run_http(model: AIModel, prompt: str, workdir: str) -> ReviewResult:
    """直连 OpenAI 兼容接口（DeepSeek / Moonshot / 其它）。

    为什么不用 codex CLI
    --------------------
    实测 codex 会忽略 config.toml 里的 model_provider 和 base_url，直接连
    `chatgpt.com`。后果是「DeepSeek」和「Kimi」两个复核员其实是**同一个
    ChatGPT 在答两次**，所谓「三个模型独立复核」根本不独立 —— 而那正是
    这套系统最重要的前提。改成直连 HTTP 之后，才真的是三家不同厂商。
    """
    key = _read_key(model)
    if not key:
        return ReviewResult(model.id, model.kind, "unavailable",
                            error=f"读不到凭据：{model.key_from}")
    if not model.base_url:
        return ReviewResult(model.id, model.kind, "unavailable",
                            error="没有配置 base_url")

    body = {
        "model": model.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt + "\n\n" + JSON_HINT},
        ],
        "max_tokens": model.max_tokens,
    }
    if model.temperature is not None:
        # kimi-k3 只接受 temperature=1，传别的直接 400
        body["temperature"] = model.temperature
    if model.json_mode:
        body["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        model.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=model.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return ReviewResult(model.id, model.kind, "error",
                            elapsed_sec=round(time.time() - t0, 1),
                            error=f"HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        return ReviewResult(model.id, model.kind, "error",
                            elapsed_sec=round(time.time() - t0, 1),
                            error=f"连不上 {model.base_url}: {exc.reason}")
    except TimeoutError:
        return ReviewResult(model.id, model.kind, "timeout",
                            elapsed_sec=round(time.time() - t0, 1),
                            error=f"超过 {model.timeout} 秒未返回")

    elapsed = round(time.time() - t0, 1)
    try:
        choice = payload["choices"][0]
        content = choice["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ReviewResult(model.id, model.kind, "error", elapsed_sec=elapsed,
                            error=f"返回结构异常: {str(payload)[:200]}")

    usage = payload.get("usage") or {}
    tokens = usage.get("total_tokens")
    finish = choice.get("finish_reason") or ""

    try:
        data = json.loads(_strip_fence(content))
    except json.JSONDecodeError as exc:
        # 思考型模型把 max_tokens 烧在 reasoning 上时，输出会被拦腰截断。
        # 这种情况要明确报「token 不够」，不能含糊地报成 JSON 解析失败 ——
        # 否则下次还会以为是自己写错了。
        hint = ""
        if finish == "length":
            hint = (f"（finish_reason=length，max_tokens={model.max_tokens} 用完了。"
                    f"思考型模型要把 max_tokens 调大）")
        return ReviewResult(model.id, model.kind, "error", elapsed_sec=elapsed,
                            tokens=tokens,
                            error=f"不是合法 JSON{hint}: {exc}；"
                                  f"内容开头 {content[:120]!r}")

    return ReviewResult(
        model_id=model.id, kind=model.kind, status="ok", elapsed_sec=elapsed,
        verdict=str(data.get("verdict", ABSTAIN)).lower(),
        confidence=float(data.get("confidence") or 0.0),
        top_risk=str(data.get("top_risk", "")),
        reason=str(data.get("reason", "")),
        suggested_fix=str(data.get("suggested_fix", "")),
        tokens=tokens,
    )


def _run_claude(model: AIModel, prompt: str, workdir: str) -> ReviewResult:
    """用 Claude Code 的 `claude -p` 做结构化复核。

    实测（Claude Code 2.1.281）三个要点，都是踩出来的：

    1. `--json-schema` 只吃**内联 JSON**，传文件路径会被当成 JSON 解析而报
       `Unexpected identifier "C"`（把盘符当成了标识符）。
    2. `--output-format json` 返回的是**包装信封**，真正的结论在 `result` 字段里，
       而且 `result` 本身是**字符串形式的 JSON**，要再解一次。
    3. 开销压不下去：无论怎么限制工具，input 都在 27k~35k tokens，
       大部分是 Claude Code 自己的系统提示词。实测约 $0.20/次 ≈ ¥1.4。
       所以它只在真有候选要复核时才该被调用 —— 这也是流水线天然的行为。
    """
    exe = _claude_exe()
    if not exe:
        return ReviewResult(model.id, model.kind, "unavailable",
                            error="找不到 claude 可执行文件")

    schema_json = json.dumps(REVIEW_SCHEMA, ensure_ascii=False)
    argv = [
        exe, "-p",
        "--json-schema", schema_json,
        "--output-format", "json",
        # 复核只需要读我们给的文本，不该碰文件系统。禁掉常用工具
        # 既安全，实测也能把 input 从 35k 降到 27k。
        "--disallowed-tools", "Bash", "Read", "Write", "Edit", "Glob",
        "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite",
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            argv, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=model.timeout, cwd=workdir,
        )
    except subprocess.TimeoutExpired:
        return ReviewResult(model.id, model.kind, "timeout",
                            elapsed_sec=round(time.time() - t0, 1),
                            error=f"超过 {model.timeout} 秒未返回")
    except OSError as exc:
        return ReviewResult(model.id, model.kind, "error",
                            elapsed_sec=round(time.time() - t0, 1), error=str(exc))

    elapsed = round(time.time() - t0, 1)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-400:]
        return ReviewResult(model.id, model.kind, "error", elapsed_sec=elapsed,
                            error=f"退出码 {proc.returncode}: {err}")

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return ReviewResult(model.id, model.kind, "error", elapsed_sec=elapsed,
                            error=f"外层不是合法 JSON: {exc}")

    cost = float(envelope.get("total_cost_usd") or 0.0)
    raw = envelope.get("result")
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = _extract_json(raw)
    if not isinstance(data, dict):
        return ReviewResult(model.id, model.kind, "error", elapsed_sec=elapsed,
                            cost_usd=cost,
                            error=f"result 里没有可解析的结论: {str(raw)[:150]}")

    return ReviewResult(
        model_id=model.id, kind=model.kind, status="ok", elapsed_sec=elapsed,
        cost_usd=cost,
        verdict=str(data.get("verdict", ABSTAIN)).lower(),
        confidence=float(data.get("confidence") or 0.0),
        top_risk=str(data.get("top_risk", "")),
        reason=str(data.get("reason", "")),
        suggested_fix=str(data.get("suggested_fix", "")),
    )


OLLAMA_PROMPT = """你是一名风控复核员,审查一份 ETF 建仓建议。你不预测涨跌,只找结构性缺陷。

背景:约 500 元的小账户,1 手 = 100 份,仓位粒度极粗无法分散。
场内 ETF 免印花税,成本主要是佣金,若券商有最低 5 元佣金,往返成本可达本金 2%。
A 股股票型 ETF 是 T+1,当天买入当天不能卖。

只输出一个 JSON 对象,不要任何解释文字、不要 markdown 代码块。字段:
{"verdict": "approve 或 caution 或 reject", "confidence": 0 到 1 的小数,
 "top_risk": "最大的一个风险点,一句话", "reason": "判断依据",
 "suggested_fix": "如果改一处,改什么;没有就写 none"}

"""


def _extract_json(text: str) -> dict | None:
    """从自由文本里抠出第一个 JSON 对象。本地小模型经常加解释文字。"""
    t = text.strip()
    if "```" in t:
        parts = t.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None


def _run_ollama(model: AIModel, prompt: str, workdir: str) -> ReviewResult:
    """走 ollama 的 HTTP API 而不是命令行。

    关键差别是 `format: "json"` —— 它由服务端保证输出是合法 JSON。
    走 `ollama run` 时小模型会在 JSON 前后加解释文字,解析失败率很高;
    实测 qwen2.5:7b 在命令行下必失败,在 format=json 下稳定成功。
    """
    import urllib.error
    import urllib.request

    body = {
        "model": model.model,
        "prompt": OLLAMA_PROMPT + prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_predict": model.max_tokens},
    }
    if model.think is not None:
        body["think"] = model.think
    body = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=body, headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=model.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return ReviewResult(model.id, model.kind, "unavailable",
                            elapsed_sec=round(time.time() - t0, 1),
                            error=f"连不上 ollama（{reason}）。服务没启动？")
    except (TimeoutError, OSError) as exc:
        return ReviewResult(model.id, model.kind, "timeout",
                            elapsed_sec=round(time.time() - t0, 1), error=str(exc))
    except json.JSONDecodeError as exc:
        return ReviewResult(model.id, model.kind, "error",
                            elapsed_sec=round(time.time() - t0, 1),
                            error=f"ollama 返回的不是 JSON: {exc}")

    elapsed = round(time.time() - t0, 1)
    raw = payload.get("response") or ""
    data = _extract_json(raw)
    if data is None:
        return ReviewResult(model.id, model.kind, "error", elapsed_sec=elapsed,
                            error="本地模型没有输出可解析的 JSON")
    return ReviewResult(
        model_id=model.id, kind=model.kind, status="ok", elapsed_sec=elapsed,
        verdict=str(data.get("verdict", ABSTAIN)).lower(),
        confidence=float(data.get("confidence") or 0.0),
        top_risk=str(data.get("top_risk", "")),
        reason=str(data.get("reason", "")),
        suggested_fix=str(data.get("suggested_fix", "")),
    )


def review(cfg: Config, context: dict, dry_run: bool = False,
           only: list[str] | None = None) -> ReviewBoard:
    """跑一遍交叉复核,返回董事会结论。"""
    board = ReviewBoard()
    if not cfg.ai.enabled or not cfg.ai.reviewers:
        board.status = "unavailable"
        board.decision = "allow"
        board.summary = "AI 复核未启用,仅依赖规则风控闸门。"
        return board

    models = [m for m in cfg.ai.reviewers if not only or m.id in only]

    if dry_run:
        for m in models:
            board.results.append(ReviewResult(
                m.id, m.kind, "ok", verdict=CAUTION, confidence=0.5,
                top_risk="干跑模式:未真实调用模型",
                reason="这是 --dry-run 的占位结论,不代表任何模型的真实意见。",
                suggested_fix="去掉 --dry-run 以获得真实复核。",
            ))
        board.status = "ok"
        board.decision = "allow"
        board.summary = f"干跑模式:跳过真实调用,{len(models)} 个模型均为占位结论。"
        return board

    prompt = SYSTEM_PROMPT + "\n\n" + build_brief(cfg, context)

    # 这一笔要下的注有多大 —— 用来判断「值不值得花钱复核」
    notional = float((context.get("signal") or {}).get("notional") or 0.0)
    code = str((context.get("signal") or {}).get("code") or "")

    from .budget import BudgetGuard, model_cost_cny
    guard = BudgetGuard(cfg)

    with tempfile.TemporaryDirectory(prefix="etf-review-") as workdir:
        for m in models:
            cost = model_cost_cny(cfg, m.id)
            verdict = guard.can_afford(cost, notional)
            if not verdict.allowed:
                # 因预算跳过 ≠ 模型同意。必须单独标出来，而且不计入赞成票。
                # 注意 cost_usd 留 0 —— 跳过没花钱，算进成本会让台账虚高。
                board.results.append(ReviewResult(
                    m.id, m.kind, "skipped_budget",
                    cost_usd=0.0,
                    error=f"因预算跳过：{verdict.reason}",
                ))
                guard.record(m.id, 0.0, notional=notional, code=code,
                             note=f"跳过，节省 ¥{cost:.2f}：{verdict.reason}")
                continue

            if m.kind == "http":
                res = _run_http(m, prompt, workdir)
            elif m.kind == "codex":
                # 保留旧名字但不再走 codex CLI：实测它绕过配置连 chatgpt.com，
                # 会让「独立复核」变成同一个模型答两次。统一走直连。
                res = _run_http(m, prompt, workdir)
            elif m.kind == "claude":
                res = _run_claude(m, prompt, workdir)
            elif m.kind == "ollama":
                res = _run_ollama(m, prompt, workdir)
            else:
                res = ReviewResult(m.id, m.kind, "error",
                                   error=f"未知的模型类型: {m.kind}")
            board.results.append(res)
            # 记账：优先用模型自己报的成本（Claude 会报），没有就用配置值
            actual = res.cost_usd * 7.2 if res.cost_usd > 0 else cost
            if actual > 0:
                guard.record(m.id, actual, notional=notional, code=code,
                             note="ok" if res.status == "ok" else res.status)

    # 只有「投票员」参与计票。advisory 模型（本地 8B）只记录意见，
    # 不能有否决权 —— 实测它给出过「把止损放宽到 8-10%」这种会加大亏损的建议。
    advisory = [r for r in board.results
                if r.model_id in {m.id for m in models if m.advisory}]
    voters = [r for r in board.results if r not in advisory]

    board.approvals = sum(1 for r in voters if r.counts_as_approval)
    board.vetoes = sum(1 for r in voters if r.is_veto)
    board.abstentions = sum(1 for r in voters
                            if r.status != "ok" or r.verdict == ABSTAIN)

    strong_veto = [r for r in voters
                   if r.is_veto and r.confidence >= cfg.ai.veto_confidence]

    if board.approvals == 0 and board.abstentions == len(voters):
        board.status = "unavailable"
        board.decision = "allow"   # 弃权不等于否决,交给规则闸门和人来决定
        board.summary = ("所有模型都未能给出意见(超时或报错)。"
                         "这**不是**复核通过,只是没有额外信息。请自行判断。")
    elif strong_veto:
        board.status = "ok"
        board.decision = "block"
        names = "、".join(f"{r.model_id}(把握{r.confidence:.0%})" for r in strong_veto)
        board.summary = f"被 {names} 明确否决,不建议执行。理由见下方。"
    elif board.approvals < cfg.ai.min_approvals:
        board.status = "insufficient"
        board.decision = "block"
        board.summary = (f"只有 {board.approvals} 个模型给出赞成意见,"
                         f"少于要求的 {cfg.ai.min_approvals} 个。")
    else:
        board.status = "ok"
        board.decision = "allow"
        board.summary = f"{board.approvals} 个模型未反对,{board.vetoes} 个否决。"
    if advisory:
        names = "、".join(f"{r.model_id}→{r.verdict or r.status}" for r in advisory)
        board.summary += f"（另有参考意见：{names}，不计票）"
    return board
