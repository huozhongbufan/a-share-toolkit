"""半自动下单工单。

『半自动』的准确定义:系统算完、AI 复核完、风控闸门放行之后,
输出一张写清楚『买什么、买多少、挂什么价、什么条件下卖』的工单,
最后一键由人来按。

这里刻意不做 GUI 自动化点击券商下单窗口。原因很直接:
  - 同花顺下单窗是长驻进程的 Win32 界面,控件会随版本变,点错一次就是真钱
  - ¥500 的本金,一次误操作的成本远高于省下的那几秒钟
  - 人工按确认键本身就是一个有价值的风控环节
所以本模块只产出可核对、可打印、可存档的工单,执行仍由人完成。
"""
from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime

from .config import Config
from .risk import RiskDecision
from .ai_review import ReviewBoard
from .strategy import Signal

EXECUTABLE = "可执行"
BLOCKED = "已拦截"
REVIEW = "需人工决断"


@dataclass
class Ticket:
    ticket_id: str
    created_at: str
    action: str
    code: str
    name: str
    price: float
    shares: int
    notional: float
    t0: bool
    lot_cost: float
    stop: float
    target: float
    buy_fee: float
    sell_fee: float
    round_trip_cost: float
    breakeven_pct: float
    breakeven_price: float
    risk_amount: float
    max_loss_pct_of_capital: float
    required_win_rate: float
    gap_loss: float
    gap_loss_pct: float
    status: str
    status_reason: str
    valid_until: str
    risk: dict = field(default_factory=dict)
    ai: dict = field(default_factory=dict)
    signal: dict = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)
    disclaimers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _next_trading_day_label() -> str:
    """给工单一个有效期。不查交易日历,只按自然日 +1 保守处理。"""
    from datetime import timedelta
    d = datetime.now() + timedelta(days=1)
    return d.strftime("%Y-%m-%d") + " 收盘前"


def build_ticket(cfg: Config, sig: Signal, decision: RiskDecision,
                 board: ReviewBoard, action: str = "买入") -> Ticket:
    shares = decision.shares
    notional = decision.notional or (sig.lot_cost if shares == 0 else 0.0)
    buy_fee = cfg.fees.one_way(notional) if notional else 0.0
    sell_fee = buy_fee
    rt = buy_fee + sell_fee
    be = (rt / notional) if notional else 0.0
    risk_amount = max(0.0, (sig.price - sig.stop)) * shares if shares else 0.0
    max_loss_pct = (risk_amount / cfg.capital) if cfg.capital else 0.0

    if not decision.allowed:
        status = BLOCKED
        blocks = decision.blocks
        status_reason = "；".join(c.detail for c in blocks) if blocks else "风控未通过"
    elif board.decision == "block":
        status = REVIEW
        status_reason = board.summary
    elif board.status == "unavailable":
        status = REVIEW
        status_reason = ("AI 复核不可用（" + board.summary +
                         "）。规则闸门已通过，是否执行由你决定。")
    else:
        status = EXECUTABLE
        status_reason = "规则风控闸门与 AI 交叉复核均已通过。"

    steps = _build_steps(cfg, sig, shares, status)

    disclaimers = [
        "本工单由程序按你设定的规则生成，不构成投资建议。",
        "免费行情接口可能延迟或出错，下单前请在券商 App 里核对一次现价。",
        f"所有价格与数量以你券商 App 显示的为准；本工单只描述意图。",
    ]
    if sig.t0:
        disclaimers.append("该标的按名称/代码判断为 T+0（跨境或黄金类），"
                           "当天可卖；请在券商 App 确认，判断可能有误。")
    else:
        disclaimers.append("该标的按名称/代码判断为 T+1（A股股票型），当天买入当天不能卖。")
    if notional and cfg.fees.is_min_commission_bound(notional):
        disclaimers.append(
            f"这笔交易被『最低佣金 {cfg.fees.min_commission:.0f} 元』卡住，"
            f"实际费率远高于名义费率。换一家免五的券商能显著改善。")

    return Ticket(
        ticket_id=datetime.now().strftime("T%Y%m%d-%H%M%S"),
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        action=action,
        code=sig.code,
        name=sig.name,
        price=round(sig.price, 3),
        shares=shares,
        notional=round(notional, 2),
        t0=sig.t0,
        lot_cost=round(sig.lot_cost, 2),
        stop=round(sig.stop, 3),
        target=round(sig.target, 3),
        buy_fee=round(buy_fee, 2),
        sell_fee=round(sell_fee, 2),
        round_trip_cost=round(rt, 2),
        breakeven_pct=round(be, 6),
        breakeven_price=round(sig.price * (1 + be), 3) if sig.price else 0.0,
        risk_amount=round(risk_amount, 2),
        max_loss_pct_of_capital=round(max_loss_pct, 6),
        required_win_rate=round(decision.required_win_rate, 4),
        gap_loss=round(decision.gap_loss, 2),
        gap_loss_pct=round(decision.gap_loss_pct, 6),
        status=status,
        status_reason=status_reason,
        valid_until=_next_trading_day_label(),
        risk=decision.to_dict(),
        ai=board.to_dict(),
        signal=sig.to_dict(),
        steps=steps,
        disclaimers=disclaimers,
    )


def _build_steps(cfg: Config, sig: Signal, shares: int, status: str) -> list[str]:
    if status == BLOCKED:
        return [
            "不要下单。上面的拦截原因需要先解决。",
            "如果是费用占比被拦：去券商 App 查清『场内基金佣金』是不是免五。"
            "如果是免五，把 config.json 里的 min_commission 改成 0，重跑一次。",
            "如果是买不起 1 手：说明这个标的超出本金范围，换候选池里更便宜的那只。",
            "如果是熔断被拦：今天不要再看盘了。",
        ]
    if shares <= 0:
        return ["本次没有可执行的手数，不要下单。"]

    how = "限价委托"
    return [
        f"打开券商 App 或同花顺，进入『买入』页面。",
        f"输入代码 {sig.code[2:]}（{sig.name}）。",
        f"委托方式选『{how}』，委托价填 {sig.price:.3f} 元"
        f"（不要用市价单，ETF 盘口薄的时候市价会吃亏）。",
        f"委托数量填 {shares} 份（ETF 一手 = 100 份，必须是 100 的整数倍）。",
        f"核对预计金额约 {shares * sig.price:.2f} 元 + 手续费。",
        f"确认成交后，回到本项目运行 python run.py fill 登记这笔成交，"
        f"否则系统不知道你持仓了。",
        f"挂好条件：跌破 {sig.stop:.3f} 元就卖，涨到 {sig.target:.3f} 元考虑止盈。",
        f"工单有效期到 {_next_trading_day_label()}；过期价格就变了，要重新生成。",
    ]


# ---------------------------------------------------------------- 渲染

def render_markdown(t: Ticket) -> str:
    L: list[str] = []
    L.append(f"# 交易工单 {t.ticket_id}")
    L.append("")
    L.append(f"**状态：{t.status}** — {t.status_reason}")
    L.append("")
    L.append(f"生成时间 {t.created_at} ｜ 有效期至 {t.valid_until}")
    L.append("")
    L.append("## 下单内容")
    L.append("")
    L.append("| 项目 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 方向 | {t.action} |")
    L.append(f"| 代码 | {t.code[2:]} |")
    L.append(f"| 名称 | {t.name} |")
    L.append(f"| 参考价 | {t.price:.3f} 元 |")
    L.append(f"| 数量 | {t.shares} 份 |")
    L.append(f"| 金额 | {t.notional:.2f} 元 |")
    L.append(f"| 交易制度 | {'T+0（当天可卖）' if t.t0 else 'T+1（当天不可卖）'} |")
    L.append(f"| 止损位 | {t.stop:.3f} 元 |")
    L.append(f"| 止盈位 | {t.target:.3f} 元 |")
    L.append("")
    L.append("## 这笔交易的钱花在哪")
    L.append("")
    L.append("| 项目 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 买入手续费 | {t.buy_fee:.2f} 元 |")
    L.append(f"| 卖出手续费 | {t.sell_fee:.2f} 元 |")
    L.append(f"| 往返总费用 | {t.round_trip_cost:.2f} 元 |")
    L.append(f"| **盈亏平衡涨幅** | **{t.breakeven_pct*100:.3f}%** |")
    L.append(f"| 盈亏平衡价 | {t.breakeven_price:.3f} 元 |")
    L.append(f"| 止损触发的亏损 | {t.risk_amount:.2f} 元（占本金 {t.max_loss_pct_of_capital*100:.2f}%） |")
    if t.required_win_rate:
        L.append(f"| **打平所需胜率** | **{t.required_win_rate*100:.0f}%**"
                 f"（止损净亏和止盈净赚都是含费的，费用对亏赚不对称） |")
    if t.gap_loss:
        L.append(f"| T+1 跳空 1×ATR 的亏损 | {t.gap_loss:.2f} 元"
                 f"（占本金 {t.gap_loss_pct*100:.1f}%） |")
    L.append("")
    L.append("## 风控闸门逐条结论")
    L.append("")
    for c in t.risk.get("checks", []):
        mark = {"ok": "✅", "warn": "⚠️", "block": "⛔"}.get(c["level"], "•")
        L.append(f"- {mark} **{c['name']}**：{c['detail']}")
    L.append("")
    L.append("## 多模型复核意见")
    L.append("")
    L.append(f"结论：{t.ai.get('summary','')}")
    L.append("")
    for r in t.ai.get("results", []):
        L.append(f"### {r['model_id']} — {r['verdict']}"
                 f"（把握 {float(r.get('confidence') or 0)*100:.0f}%，"
                 f"{r.get('elapsed_sec', 0)} 秒）")
        if r["status"] != "ok":
            L.append(f"未取得意见：{r.get('error','')}")
        else:
            L.append(f"- 最大风险：{r['top_risk']}")
            L.append(f"- 判断依据：{r['reason']}")
            L.append(f"- 建议改动：{r['suggested_fix']}")
        L.append("")
    L.append("## 操作步骤")
    L.append("")
    for i, s in enumerate(t.steps, 1):
        L.append(f"{i}. {s}")
    L.append("")
    L.append("## 必须知道的事")
    L.append("")
    for d in t.disclaimers:
        L.append(f"- {d}")
    L.append("")
    return "\n".join(L)


def render_html(t: Ticket) -> str:
    e = html.escape

    def checks_html() -> str:
        rows = []
        for c in t.risk.get("checks", []):
            cls = {"ok": "ok", "warn": "warn", "block": "block"}.get(c["level"], "")
            label = {"ok": "通过", "warn": "警告", "block": "拦截"}.get(c["level"], "")
            rows.append(
                f'<tr class="{cls}"><td class="k">{e(c["name"])}</td>'
                f'<td class="v">{e(label)}</td><td>{e(c["detail"])}</td></tr>'
            )
        return "\n".join(rows)

    def ai_html() -> str:
        cards = []
        for r in t.ai.get("results", []):
            v = r["verdict"]
            cls = {"approve": "ok", "caution": "warn", "reject": "block"}.get(v, "")
            if r["status"] != "ok":
                body = f'<p class="err">{e(r.get("error",""))}</p>'
            else:
                body = (
                    f'<p><span class="lab">最大风险</span>{e(r["top_risk"])}</p>'
                    f'<p><span class="lab">判断依据</span>{e(r["reason"])}</p>'
                    f'<p><span class="lab">建议改动</span>{e(r["suggested_fix"])}</p>'
                )
            cards.append(
                f'<div class="card {cls}"><div class="ch"><b>{e(r["model_id"])}</b>'
                f'<span class="badge {cls}">{e(v)}</span>'
                f'<span class="dim">{float(r.get("confidence") or 0)*100:.0f}% ｜ '
                f'{r.get("elapsed_sec",0)}s</span></div>{body}</div>'
            )
        return "\n".join(cards)

    steps = "\n".join(f"<li>{e(s)}</li>" for s in t.steps)
    notes = "\n".join(f"<li>{e(d)}</li>" for d in t.disclaimers)
    gap_note = ""
    if t.gap_loss:
        gap_note = (f"若这是 T+1 标的且次日跳空 1 倍 ATR，"
                    f"亏损约 {t.gap_loss:.2f} 元（本金的 {t.gap_loss_pct*100:.1f}%）。")
    status_cls = {"可执行": "ok", "已拦截": "block", "需人工决断": "warn"}.get(t.status, "")
    asset = "T+0 当天可卖" if t.t0 else "T+1 当天不可卖"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>交易工单 {e(t.ticket_id)}</title>
<style>
  :root {{ --ink:#1c1c1e; --dim:#6b7280; --line:#e5e7eb; --accent:#1a4d78;
           --ok:#15803d; --warn:#b45309; --block:#b91c1c; --bg:#fff; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:28px 32px; background:var(--bg); color:var(--ink);
          font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:820px; margin:0 auto; }}
  h1 {{ font-size:20px; margin:0 0 4px; letter-spacing:.01em; }}
  h2 {{ font-size:14px; margin:26px 0 10px; padding-bottom:6px;
        border-bottom:1px solid var(--line); color:var(--accent); font-weight:600; }}
  .meta {{ color:var(--dim); font-size:12.5px; margin-bottom:16px; }}
  .status {{ display:inline-block; padding:3px 10px; border-radius:4px; font-weight:600;
             font-size:13px; margin-bottom:6px; }}
  .status.ok {{ background:#ecfdf5; color:var(--ok); }}
  .status.warn {{ background:#fffbeb; color:var(--warn); }}
  .status.block {{ background:#fef2f2; color:var(--block); }}
  .reason {{ background:#f9fafb; border-left:3px solid var(--accent); padding:10px 14px;
             margin:10px 0 0; font-size:13px; color:#374151; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line);
           vertical-align:top; }}
  th {{ color:var(--dim); font-weight:500; font-size:12px; }}
  td.k {{ width:180px; color:#374151; }}
  td.v {{ width:70px; }}
  tr.ok td.v {{ color:var(--ok); }}
  tr.warn td.v {{ color:var(--warn); }}
  tr.block td.v {{ color:var(--block); font-weight:600; }}
  .big {{ display:flex; gap:26px; flex-wrap:wrap; margin:6px 0 2px; }}
  .big div {{ min-width:130px; }}
  .big .n {{ font-size:22px; font-weight:600; color:var(--accent); letter-spacing:-.01em; }}
  .big .l {{ font-size:12px; color:var(--dim); }}
  .card {{ border:1px solid var(--line); border-radius:6px; padding:12px 14px;
           margin-bottom:10px; }}
  .card.ok {{ border-left:3px solid var(--ok); }}
  .card.warn {{ border-left:3px solid var(--warn); }}
  .card.block {{ border-left:3px solid var(--block); }}
  .ch {{ display:flex; align-items:center; gap:8px; margin-bottom:6px; }}
  .badge {{ font-size:11px; padding:1px 7px; border-radius:3px; text-transform:uppercase; }}
  .badge.ok {{ background:#ecfdf5; color:var(--ok); }}
  .badge.warn {{ background:#fffbeb; color:var(--warn); }}
  .badge.block {{ background:#fef2f2; color:var(--block); }}
  .dim {{ color:var(--dim); font-size:12px; margin-left:auto; }}
  .lab {{ display:inline-block; width:66px; color:var(--dim); font-size:12.5px; }}
  .card p {{ margin:4px 0; font-size:13px; }}
  .err {{ color:var(--block); font-size:12.5px; }}
  ol,ul {{ padding-left:20px; margin:8px 0; }}
  li {{ margin:5px 0; font-size:13px; }}
  .foot {{ margin-top:28px; padding-top:12px; border-top:1px solid var(--line);
           color:var(--dim); font-size:12px; }}
  @media print {{ body {{ padding:0; font-size:12px; }} h2 {{ page-break-after:avoid; }} }}
</style></head><body><div class="wrap">

<h1>交易工单 {e(t.ticket_id)}</h1>
<div class="meta">生成 {e(t.created_at)} ｜ 有效期至 {e(t.valid_until)}</div>
<span class="status {status_cls}">{e(t.status)}</span>
<div class="reason">{e(t.status_reason)}</div>

<h2>下单内容</h2>
<div class="big">
  <div><div class="n">{e(t.code[2:])}</div><div class="l">{e(t.name)}</div></div>
  <div><div class="n">{t.shares}</div><div class="l">份（1 手 = 100 份）</div></div>
  <div><div class="n">{t.price:.3f}</div><div class="l">参考价（元）</div></div>
  <div><div class="n">{t.notional:.2f}</div><div class="l">金额（元）</div></div>
</div>
<table>
  <tr><td class="k">交易制度</td><td colspan="2">{asset}</td></tr>
  <tr><td class="k">止损位</td><td colspan="2">{t.stop:.3f} 元</td></tr>
  <tr><td class="k">止盈位</td><td colspan="2">{t.target:.3f} 元</td></tr>
</table>

<h2>这笔交易的钱花在哪</h2>
<div class="big">
  <div><div class="n">{t.round_trip_cost:.2f}</div><div class="l">往返总费用（元）</div></div>
  <div><div class="n">{t.breakeven_pct*100:.3f}%</div><div class="l">盈亏平衡涨幅</div></div>
  <div><div class="n">{t.breakeven_price:.3f}</div><div class="l">盈亏平衡价（元）</div></div>
  <div><div class="n">{t.max_loss_pct_of_capital*100:.2f}%</div><div class="l">止损时占本金</div></div>
  <div><div class="n">{t.required_win_rate*100:.0f}%</div><div class="l">打平所需胜率</div></div>
</div>
<p class="meta">买入手续费 {t.buy_fee:.2f} 元 ＋ 卖出手续费 {t.sell_fee:.2f} 元。
止损触发时亏 {t.risk_amount:.2f} 元。{gap_note}</p>

<h2>风控闸门逐条结论</h2>
<table>
  <tr><th>检查项</th><th>结论</th><th>说明</th></tr>
  {checks_html()}
</table>

<h2>多模型复核意见</h2>
<p class="meta">{e(t.ai.get("summary",""))}</p>
{ai_html()}

<h2>操作步骤</h2>
<ol>{steps}</ol>

<h2>必须知道的事</h2>
<ul>{notes}</ul>

<div class="foot">etf-copilot · 本工单由程序按预设规则生成，不构成投资建议。</div>
</div></body></html>"""


def write_ticket(cfg: Config, t: Ticket) -> dict[str, str]:
    os.makedirs(cfg.out_dir, exist_ok=True)
    base = os.path.join(cfg.out_dir, f"ticket-{t.ticket_id}")
    paths = {
        "json": base + ".json",
        "md": base + ".md",
        "html": base + ".html",
    }
    with open(paths["json"], "w", encoding="utf-8") as fh:
        json.dump(t.to_dict(), fh, ensure_ascii=False, indent=2)
    with open(paths["md"], "w", encoding="utf-8") as fh:
        fh.write(render_markdown(t))
    with open(paths["html"], "w", encoding="utf-8") as fh:
        fh.write(render_html(t))
    latest = os.path.join(cfg.out_dir, "ticket-latest.html")
    with open(latest, "w", encoding="utf-8") as fh:
        fh.write(render_html(t))
    paths["latest"] = latest
    return paths
