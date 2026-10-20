"""API 花费预算与台账。

目标很朴素：**让每天的 AI 花费有上限、看得见、且和交易金额成比例。**

三条规则
--------
1. **日/月预算硬上限。** 花完了就降级，不会偷偷继续烧。
2. **花费必须和这笔交易成比例。** 一笔 ¥400 的交易花 ¥1.29 去复核，
   等于 0.32% —— 比往返手续费还贵。这种情况下不该调用贵的模型。
3. **降级要看得见。** 因为预算不足而跳过的模型，复核意见里必须写明
   「因预算跳过」，不能表现为「模型同意了」。

成本口径（实测，人民币）
------------------------
    deepseek  ¥0.002  直连 api.deepseek.com，1414~1970 tokens/次
    kimi      ¥0.02   直连 api.moonshot.cn，kimi-k3，3400~4400 tokens/次
    claude    ¥1.29   Claude Code，input 27k~35k 是它自己的系统提示词
    本地模型   ¥0      本机算力

⚠ 20261020 更正：原来这里写「deepseek/kimi 走 codex 订阅，¥0」是错的。
实测 codex CLI 会忽略 config.toml 里的 model_provider，直接连 chatgpt.com，
所以那两个「独立复核员」其实是同一个 ChatGPT 在答两次，而且一分钱没记。
改成直连 HTTP 之后才真的是两家不同厂商，也才有真实成本。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, date

from .config import Config

LEDGER = "ai_spend.jsonl"


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    cost_cny: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _today() -> str:
    return date.today().isoformat()


class BudgetGuard:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bc = cfg.ai.budget
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.path = os.path.join(cfg.data_dir, LEDGER)

    # ---------------------------------------------------------- 台账

    def entries(self) -> list[dict]:
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

    def record(self, model_id: str, cost_cny: float, *,
               notional: float = 0.0, code: str = "", note: str = "") -> None:
        if cost_cny <= 0 and not note:
            return
        ev = {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date": _today(),
            "model": model_id,
            "cost_cny": round(cost_cny, 4),
            "notional": round(notional, 2),
            "code": code,
            "note": note,
        }
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def spent_today(self) -> float:
        t = _today()
        return round(sum(e.get("cost_cny", 0.0) for e in self.entries()
                         if e.get("date") == t), 4)

    def spent_month(self) -> float:
        m = _today()[:7]
        return round(sum(e.get("cost_cny", 0.0) for e in self.entries()
                         if str(e.get("date", ""))[:7] == m), 4)

    def spent_total(self) -> float:
        return round(sum(e.get("cost_cny", 0.0) for e in self.entries()), 4)

    # ---------------------------------------------------------- 判定

    def can_afford(self, model_cost_cny: float, notional: float = 0.0) -> Decision:
        """该不该为这一笔交易花这个模型的钱。"""
        if not self.bc.enabled:
            return Decision(True, "预算管控已关闭", model_cost_cny)
        if model_cost_cny <= 0:
            return Decision(True, "免费模型", 0.0)

        today_left = self.bc.daily_limit_cny - self.spent_today()
        month_left = self.bc.monthly_limit_cny - self.spent_month()
        if today_left < model_cost_cny:
            return Decision(False,
                            f"今日预算剩 ¥{max(today_left,0):.2f}，"
                            f"不够这次 ¥{model_cost_cny:.2f}", model_cost_cny)
        if month_left < model_cost_cny:
            return Decision(False,
                            f"本月预算剩 ¥{max(month_left,0):.2f}，"
                            f"不够这次 ¥{model_cost_cny:.2f}", model_cost_cny)

        # 花费必须和交易金额成比例
        if notional > 0 and self.bc.max_cost_ratio_of_trade > 0:
            budget_for_this = notional * self.bc.max_cost_ratio_of_trade
            if model_cost_cny > budget_for_this:
                need = model_cost_cny / self.bc.max_cost_ratio_of_trade
                return Decision(
                    False,
                    f"这笔交易 ¥{notional:.0f}，按 "
                    f"{self.bc.max_cost_ratio_of_trade*100:.1f}% 的比例，"
                    f"最多只能花 ¥{budget_for_this:.2f} 复核，"
                    f"而这个模型要 ¥{model_cost_cny:.2f}。"
                    f"（交易金额到 ¥{need:.0f} 以上才划算）",
                    model_cost_cny)
        return Decision(True, "预算充足", model_cost_cny)

    # ---------------------------------------------------------- 报告

    def report(self) -> dict:
        entries = self.entries()
        month = _today()[:7]
        m_entries = [e for e in entries if str(e.get("date", ""))[:7] == month]

        by_model: dict[str, dict] = {}
        for e in m_entries:
            m = e.get("model", "?")
            d = by_model.setdefault(m, {"calls": 0, "cost_cny": 0.0})
            d["calls"] += 1
            d["cost_cny"] = round(d["cost_cny"] + e.get("cost_cny", 0.0), 4)

        days = len({e.get("date") for e in m_entries}) or 0
        avg_day = self.spent_month() / days if days else 0.0

        return {
            "today": self.spent_today(),
            "month": self.spent_month(),
            "total": self.spent_total(),
            "daily_limit": self.bc.daily_limit_cny,
            "monthly_limit": self.bc.monthly_limit_cny,
            "today_pct": (self.spent_today() / self.bc.daily_limit_cny
                          if self.bc.daily_limit_cny else 0),
            "month_pct": (self.spent_month() / self.bc.monthly_limit_cny
                          if self.bc.monthly_limit_cny else 0),
            "days_with_spend": days,
            "avg_per_spend_day": round(avg_day, 4),
            "projected_month": round(avg_day * 22, 2) if days else 0.0,
            "by_model": by_model,
            "cost_ratio": self.bc.max_cost_ratio_of_trade,
        }


def model_cost_cny(cfg: Config, model_id: str) -> float:
    """从配置里取该模型的单次成本。配置里没写就按 0 算（订阅制/本地）。"""
    for m in cfg.ai.reviewers:
        if m.id == model_id:
            return float(getattr(m, "cost_cny_per_call", 0.0) or 0.0)
    return 0.0


def min_notional_for(cfg: Config, model_id: str) -> float:
    """这个模型要多大的一笔交易才「花得起」。"""
    cost = model_cost_cny(cfg, model_id)
    ratio = cfg.ai.budget.max_cost_ratio_of_trade
    if cost <= 0 or ratio <= 0:
        return 0.0
    return round(cost / ratio, 0)
