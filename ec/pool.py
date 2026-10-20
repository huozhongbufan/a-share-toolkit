"""精选 ETF 池。

为什么是静态列表,而不是每次去爬全市场列表:

  1. **列表接口不可靠**。东财的 clist 接口在连续请求几次之后就会开始直接掐断
     连接(实测:第 4 次请求起 RemoteDisconnected),腾讯的批量行情接口则一直稳定。
  2. **ETF 池变化极慢**。一只 ETF 上市、退市、更名的周期是年,不是天。每天去
     爬一遍 1600 只的列表,收益和成本完全不成比例。
  3. **T+0 需要人工确认**。靠名称猜『这只跨境 ETF 是不是 T+0』会出错,而
     T+0/T+1 直接影响能不能当天止损。这里把确认过的结果当成数据写下来,
     名称启发式只作为未知代码的兜底。

所以:池子是人工维护的数据,行情是实时抓的。要加新标的,直接往下面加一行。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PoolEntry:
    code: str
    name: str
    category: str
    t0: bool          # True = 当天可卖；False = T+1
    note: str = ""


# 分类: broad=宽基, style=风格/红利, industry=行业主题, cross=跨境, gold=黄金商品, cash=货币
POOL: list[PoolEntry] = [
    # ---------------- 宽基 ----------------
    PoolEntry("sh510300", "沪深300ETF华泰柏瑞", "broad", False, "A股核心宽基,规模最大"),
    PoolEntry("sh510310", "沪深300ETF易方达", "broad", False, "费率最低的300之一"),
    PoolEntry("sz159919", "沪深300ETF嘉实", "broad", False, ""),
    PoolEntry("sh510050", "上证50ETF华夏", "broad", False, "大盘蓝筹"),
    PoolEntry("sz159922", "中证500ETF嘉实", "broad", False, "中盘"),
    PoolEntry("sh563300", "中证2000ETF华泰柏瑞", "broad", False, "小微盘,波动大"),
    PoolEntry("sh588000", "科创50ETF华夏", "broad", False, "科创板,高波动"),
    PoolEntry("sh588080", "科创50ETF易方达", "broad", False, ""),
    PoolEntry("sz159915", "创业板ETF易方达", "broad", False, "创业板,高波动"),
    PoolEntry("sz159949", "创业板50ETF华安", "broad", False, ""),
    PoolEntry("sz159781", "科创创业ETF易方达", "broad", False, "双创"),
    # ---------------- 风格 / 红利 ----------------
    PoolEntry("sh510880", "红利ETF华泰柏瑞", "style", False, "高股息,防守型"),
    PoolEntry("sh515180", "红利ETF易方达", "style", False, ""),
    PoolEntry("sz159905", "红利ETF工银", "style", False, ""),
    PoolEntry("sh512800", "银行ETF华宝", "style", False, "高股息,低波动"),
    # ---------------- 行业主题 ----------------
    PoolEntry("sz159928", "消费ETF汇添富", "industry", False, "主要消费"),
    PoolEntry("sh512010", "医药ETF易方达", "industry", False, "全指医药"),
    PoolEntry("sz159929", "医药ETF汇添富", "industry", False, ""),
    PoolEntry("sz159938", "医药ETF广发", "industry", False, ""),
    PoolEntry("sz159883", "医疗器械ETF永赢", "industry", False, "细分,波动大"),
    PoolEntry("sh512000", "券商ETF华宝", "industry", False, "牛市放大器"),
    PoolEntry("sh512880", "证券ETF国泰", "industry", False, ""),
    PoolEntry("sh512480", "半导体ETF国联安", "industry", False, "高波动"),
    PoolEntry("sh512760", "芯片ETF国泰", "industry", False, ""),
    PoolEntry("sz159995", "芯片ETF华夏", "industry", False, ""),
    PoolEntry("sz159801", "芯片ETF广发", "industry", False, ""),
    PoolEntry("sh515000", "科技ETF华宝", "industry", False, ""),
    PoolEntry("sz159819", "人工智能ETF易方达", "industry", False, ""),
    PoolEntry("sh515790", "光伏ETF华泰柏瑞", "industry", False, "周期性强"),
    PoolEntry("sz159755", "电池ETF广发", "industry", False, ""),
    PoolEntry("sh516160", "新能源ETF南方", "industry", False, ""),
    PoolEntry("sh512660", "军工ETF国泰", "industry", False, "事件驱动"),
    PoolEntry("sz159611", "电力ETF广发", "industry", False, "公用事业"),
    PoolEntry("sz159869", "游戏ETF华夏", "industry", False, ""),
    PoolEntry("sz159805", "传媒ETF鹏华", "industry", False, ""),
    PoolEntry("sz159865", "养殖ETF国泰", "industry", False, "猪周期"),
    PoolEntry("sz159825", "农业ETF富国", "industry", False, ""),
    PoolEntry("sz159766", "旅游ETF富国", "industry", False, ""),
    # ---------------- 跨境（T+0） ----------------
    PoolEntry("sh513050", "中概互联网ETF易方达", "cross", True, "T+0,中概股"),
    PoolEntry("sz159920", "恒生ETF华夏", "cross", True, "T+0,港股大盘"),
    PoolEntry("sz159892", "恒生医药ETF华夏", "cross", True, "T+0,港股医药"),
    PoolEntry("sz159941", "纳指ETF广发", "cross", True, "T+0,美股科技"),
    PoolEntry("sz159632", "纳斯达克ETF华安", "cross", True, "T+0"),
    PoolEntry("sz159509", "纳指科技ETF景顺", "cross", True, "T+0"),
    PoolEntry("sz159687", "亚太精选ETF南方", "cross", True, "T+0"),
    PoolEntry("sh513310", "中韩半导体ETF华泰柏瑞", "cross", True, "T+0,高波动"),
    PoolEntry("sh513120", "港股创新药ETF广发", "cross", True, "T+0"),
    # ---------------- 黄金（T+0） ----------------
    PoolEntry("sh518880", "黄金ETF华安", "gold", True, "T+0,1手约850元,500元买不起"),
    PoolEntry("sz159934", "黄金ETF易方达", "gold", True, "T+0,买不起"),
    PoolEntry("sz159937", "黄金ETF博时", "gold", True, "T+0,买不起"),
]

BY_CODE: dict[str, PoolEntry] = {p.code: p for p in POOL}

CATEGORY_LABEL = {
    "broad": "宽基",
    "style": "风格红利",
    "industry": "行业主题",
    "cross": "跨境",
    "gold": "黄金商品",
}


def lookup(code: str) -> PoolEntry | None:
    return BY_CODE.get(code)


def codes() -> list[str]:
    return [p.code for p in POOL]


def by_category() -> dict[str, list[PoolEntry]]:
    out: dict[str, list[PoolEntry]] = {}
    for p in POOL:
        out.setdefault(p.category, []).append(p)
    return out
