"""配置加载与校验。

配置只有一个来源：项目根目录的 config.json。
其它模块一律通过 Config 读参数，不硬编码任何费率或风控阈值。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


def _expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


class ConfigError(Exception):
    """配置本身有问题，必须让用户看见，不能静默兜底。"""


@dataclass
class Fees:
    """场内基金费率。

    关键点：ETF 免征印花税，也没有股票那样的过户费。
    成本几乎全部来自佣金，而佣金有『最低 X 元』的下限。
    对 500 元本金来说，5 元最低佣金 = 单边 1% = 往返 2%，这是头号成本项。
    """

    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.0
    transfer_fee_rate: float = 0.0
    slippage_bps: float = 2.0

    def one_way(self, notional: float) -> float:
        """单边总成本（佣金 + 印花税 + 过户费 + 滑点）。"""
        if notional <= 0:
            return 0.0
        commission = max(notional * self.commission_rate, self.min_commission)
        tax = notional * (self.stamp_duty_rate + self.transfer_fee_rate)
        slip = notional * (self.slippage_bps / 10000.0)
        return commission + tax + slip

    def round_trip(self, notional: float) -> float:
        """一次买入 + 一次同额卖出。"""
        return 2.0 * self.one_way(notional)

    def breakeven_pct(self, notional: float) -> float:
        """价格要涨多少才刚好不亏。这是给用户看的最重要的一个数。"""
        if notional <= 0:
            return 0.0
        return self.round_trip(notional) / notional

    def is_min_commission_bound(self, notional: float) -> bool:
        """这笔单子是否被『最低佣金』卡住了。

        被卡住意味着实际费率远高于名义费率 —— 小额本金的典型困境。
        """
        if self.commission_rate <= 0:
            return self.min_commission > 0
        return notional * self.commission_rate < self.min_commission

    def effective_rate(self, notional: float) -> float:
        """单边实际费率（含最低佣金影响）。"""
        if notional <= 0:
            return 0.0
        return self.one_way(notional) / notional


@dataclass
class Risk:
    max_position_pct: float = 0.70
    max_total_exposure_pct: float = 0.95
    daily_stop_loss_pct: float = -0.02
    max_drawdown_pct: float = -0.12
    max_trades_per_month: int = 4
    max_fee_ratio_per_trade: float = 0.006
    min_edge_multiple: float = 3.0
    stop_loss_atr: float = 2.0
    take_profit_atr: float = 3.0
    cooldown_days_after_stop: int = 3
    # 按『止损净亏 / 止盈净赚』倒推的盈亏平衡胜率上限。
    # 费用对亏和赚是不对称的：亏被放大、赚被缩小，所以这个数字通常远高于
    # 赔率的倒数。超过上限说明这笔交易在概率上不成立。
    max_required_win_rate: float = 0.55


@dataclass
class UniverseCfg:
    auto_max_lot_cost: bool = True
    cash_buffer_pct: float = 0.02
    min_avg_turnover_wan: float = 3000.0
    min_listed_days: int = 60
    exclude_keywords: list[str] = field(default_factory=list)
    only_t0: bool = False
    manual_whitelist: list[str] = field(default_factory=list)
    manual_blacklist: list[str] = field(default_factory=list)


@dataclass
class StrategyCfg:
    ma_fast: int = 20
    ma_slow: int = 60
    mom_window: int = 20
    pct_window: int = 250
    max_price_percentile: float = 0.75
    min_atr_pct: float = 0.008
    max_atr_pct: float = 0.06
    top_n: int = 3
    # 收盘价连续跌破 MA_fast 多少天才算趋势走坏。设 1 会被日常波动反复扫出去,
    # 每次扫出去都要付一次往返手续费。回测里这个参数的影响比想象中大。
    exit_confirm_days: int = 2


@dataclass
class AIModel:
    id: str
    kind: str  # "http"（OpenAI 兼容直连）| "claude" | "ollama"
    home: str = ""
    cmd: str = ""
    model: str = ""
    timeout: int = 240
    # 单次调用成本（人民币）。订阅制和本地模型填 0，按次计费的填实测值。
    cost_cny_per_call: float = 0.0
    # ---- 直连用 ----
    # 为什么不用 codex CLI：实测 codex 会忽略 config.toml 里的 model_provider，
    # 直接连 chatgpt.com。结果「DeepSeek」和「Kimi」两个复核员其实是同一个
    # ChatGPT 在答两次 —— 所谓「独立复核」根本不独立。改成直连 HTTP 后
    # 才真的是三个不同厂商的模型。
    base_url: str = ""
    key_from: str = ""       # 从这个 toml 里读 experimental_bearer_token
    temperature: float | None = None   # 有些模型（kimi-k3）只接受 1
    json_mode: bool = True   # 用 response_format={"type":"json_object"}
    max_tokens: int = 3000   # 思考型模型的 reasoning token 也算在这里，给小了会截断
    # ollama 专用。实测同一个开关在两种模型上行为相反：
    #   qwen3.5:4b   think=False 才有干净输出（否则烧光 num_predict 后返回空）
    #   deepseek-r1  think=True 才把思考过程和正文分开（False 会把 <think> 混进正文）
    think: bool | None = None
    # 只记录意见，不参与赞成/否决计票。用于明显弱于 API 模型的本地小模型。
    advisory: bool = False


@dataclass
class BudgetCfg:
    """API 花费管控。

    两个上限好理解；`max_cost_ratio_of_trade` 这条才是关键 ——
    **复核一个候选的费用，必须和它要下的注成比例。**
    一笔 ¥400 的交易花 ¥1.29 去复核，等于 0.32%，比往返手续费还贵，
    这种时候就不该叫贵的模型。
    """
    enabled: bool = True
    daily_limit_cny: float = 2.0
    monthly_limit_cny: float = 30.0
    max_cost_ratio_of_trade: float = 0.002


@dataclass
class PrefilterCfg:
    """第 1 层：本地小模型。

    它的活是**压缩文本信息**（公告/新闻/长文本 → 结构化标签 + 一页简报），
    不参与投票。理由：7B 级别的模型排序不稳定、不可复现，用它筛数值指标
    比确定性规则慢三个数量级还不准；但批量文本处理正是它不可替代的地方
    ——零成本、数据不出本机。
    """
    enabled: bool = False
    models: list[AIModel] = field(default_factory=list)
    embedding: str = ""


@dataclass
class AICfg:
    """第 2 层：API 大模型独立复核。

    三个模型并行、互相看不见对方的结论、只能否决不能加仓。
    串行链会顺序锚定（后一个模型看到前一个的结论就会被带偏），所以刻意并行。
    """
    enabled: bool = True
    reviewers: list[AIModel] = field(default_factory=list)
    prefilter: PrefilterCfg = field(default_factory=PrefilterCfg)
    budget: BudgetCfg = field(default_factory=BudgetCfg)
    min_approvals: int = 1
    veto_confidence: float = 0.8


@dataclass
class Config:
    account: dict[str, Any]
    fees: Fees
    risk: Risk
    universe: UniverseCfg
    strategy: StrategyCfg
    ai: AICfg
    data_dir: str
    out_dir: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def capital(self) -> float:
        return float(self.account.get("total_capital", 0.0))

    @property
    def cash(self) -> float:
        return float(self.account.get("cash", 0.0))

    def max_affordable_lot_cost(self) -> float:
        """1 手（100 份）最多能花多少钱。

        必须留出佣金和缓冲，否则会出现『算出来刚好够、下单时差 3 块钱』。
        """
        buf = 1.0 - self.universe.cash_buffer_pct
        usable = self.cash * buf - self.fees.min_commission
        return max(usable, 0.0)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    sec = raw.get(name, {})
    if not isinstance(sec, dict):
        raise ConfigError(f"config.json 里的 {name} 必须是一个对象")
    return {k: v for k, v in sec.items() if not k.startswith("_")}


def _plain(obj: Any) -> Any:
    """递归去掉以下划线开头的说明字段（config.json 里用 `_role` 之类写注释）。"""
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj


def _build(cls, data: dict[str, Any], name: str):
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"config.json 的 {name} 段有无法识别的字段: {sorted(unknown)}；"
            f"可用字段: {sorted(known)}。拼错字段名会导致参数静默失效，所以这里直接报错。"
        )
    return cls(**data)


def load_config(path: str | None = None) -> Config:
    cfg_path = _expand(path or DEFAULT_CONFIG_PATH)
    if not os.path.exists(cfg_path):
        raise ConfigError(f"找不到配置文件: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    account = _section(raw, "account")
    fees = _build(Fees, _section(raw, "fees"), "fees")
    risk = _build(Risk, _section(raw, "risk"), "risk")
    uni = _build(UniverseCfg, _section(raw, "universe"), "universe")
    strat = _build(StrategyCfg, _section(raw, "strategy"), "strategy")

    ai_raw = _section(raw, "ai")
    reviewers = [_build(AIModel, _plain(m), "ai.reviewers[]")
                 for m in ai_raw.pop("reviewers", [])]
    pre_raw = _plain(ai_raw.pop("prefilter", {}))
    pre_models = [_build(AIModel, _plain(m), "ai.prefilter.models[]")
                  for m in pre_raw.pop("models", [])]
    prefilter = _build(PrefilterCfg, pre_raw, "ai.prefilter")
    prefilter.models = pre_models
    budget = _build(BudgetCfg, _plain(ai_raw.pop("budget", {})), "ai.budget")
    ai = _build(AICfg, ai_raw, "ai")
    ai.reviewers = reviewers
    ai.prefilter = prefilter
    ai.budget = budget

    paths = _section(raw, "paths")
    data_dir = os.path.join(PROJECT_ROOT, paths.get("data_dir", "data"))
    out_dir = os.path.join(PROJECT_ROOT, paths.get("out_dir", "out"))

    cfg = Config(
        account=account,
        fees=fees,
        risk=risk,
        universe=uni,
        strategy=strat,
        ai=ai,
        data_dir=data_dir,
        out_dir=out_dir,
        raw=raw,
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.capital <= 0:
        raise ConfigError("account.total_capital 必须大于 0")
    if cfg.cash < 0:
        raise ConfigError("account.cash 不能为负数")
    if not (0 < cfg.risk.max_position_pct <= 1):
        raise ConfigError("risk.max_position_pct 必须在 (0, 1]")
    if cfg.risk.max_total_exposure_pct < cfg.risk.max_position_pct:
        raise ConfigError("risk.max_total_exposure_pct 不能小于 max_position_pct")
    if cfg.risk.daily_stop_loss_pct >= 0:
        raise ConfigError("risk.daily_stop_loss_pct 必须是负数，例如 -0.02")
    if cfg.risk.max_drawdown_pct >= 0:
        raise ConfigError("risk.max_drawdown_pct 必须是负数，例如 -0.12")
    if cfg.fees.commission_rate < 0 or cfg.fees.min_commission < 0:
        raise ConfigError("费率不能为负数")
    if cfg.strategy.ma_fast >= cfg.strategy.ma_slow:
        raise ConfigError("strategy.ma_fast 必须小于 ma_slow")
    if cfg.strategy.min_atr_pct >= cfg.strategy.max_atr_pct:
        raise ConfigError("strategy 的 min_atr_pct 必须小于 max_atr_pct")


def ensure_dirs(cfg: Config) -> None:
    for d in (cfg.data_dir, cfg.out_dir):
        os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(cfg.data_dir, "bars"), exist_ok=True)
