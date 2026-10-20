"""同花顺数据源。

桌面上的同花顺客户端本身不提供 API，但它做了两件对我们有用的事：

  1. **把全市场代码表落盘成纯文本**
     `stockname\\stockname_16_0.txt` 是上海，`stockname_32_0.txt` 是深圳。
     GBK 编码，格式 `600519=贵州茅台|贵州茅台@f`。零请求、零限流、完全离线。
     —— 这解决了「全市场代码表从哪来」这个最大的问题。

  2. **它自己的行情域名 `d.10jqka.com.cn` 是公开的**
     日线 / 分时 / 实时快照 / 五档都能取，实测可用，数据与我们原有的
     腾讯源逐字段核对一致（510300 在 20261014：收盘 4.294、量 8,864,445 手、
     额 382,782 万，两个源完全相同）。

关于**本地实时缓存** `realtime\\<市场>\\stocknow.dat`：
    文件头是 `hd1.0`，私有二进制格式，需要逆向；而且只在同花顺运行时更新
    （实测某次检查时它已过期 12.7 小时）。**不采用。**
    走公开接口拿到的是同样的数据，还不用开着客户端。

限制：同花顺接口**一次只能查一只**（批量请求 404）。所以定位是
「精确查询用同花顺，全市场批量扫描仍然用腾讯」。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
REFERER = "http://stockpage.10jqka.com.cn/"
BASE = "http://d.10jqka.com.cn/v6"

# 常见安装位置。用户可能装在 C 或 D。
DEFAULT_ROOTS = (
    r"C:\同花顺软件\同花顺",
    r"D:\同花顺软件\同花顺",
    r"C:\Program Files\同花顺",
    r"C:\Program Files (x86)\同花顺",
)

# 同花顺按「内部市场号 + 品种号」分成很多段落，每段一个品种类别。
# 这张表是实测出来的（2026-10-14 的文件，ConfigVer 日期与当天一致）：
#
#   stockname_16_0.txt  上海      stockname_32_0.txt  深圳
#     16_16   203  指数          32_32   378  指数
#     16_17  2238  A股           32_33  2908  A股
#     16_18    42  B股           32_34    39  B股
#     16_19 22693  债券          32_35 20273  债券      ← 占了大头，必须排除
#     16_20  1375  场内基金      32_36  1165  场内基金
#     16_22    84  ST股          32_37     2  退市股
#
# ST 和退市股单独成段，正好拿来排雷 —— 不用去猜名字里有没有 "ST"。
SECTION_MAP: dict[tuple[str, str], tuple[str, str]] = {
    ("stockname_16_0.txt", "name_16_16"): ("sh", "指数"),
    ("stockname_16_0.txt", "name_16_17"): ("sh", "股票"),
    ("stockname_16_0.txt", "name_16_20"): ("sh", "基金"),
    ("stockname_16_0.txt", "name_16_22"): ("sh", "ST股"),
    ("stockname_32_0.txt", "name_32_32"): ("sz", "指数"),
    ("stockname_32_0.txt", "name_32_33"): ("sz", "股票"),
    ("stockname_32_0.txt", "name_32_36"): ("sz", "基金"),
    ("stockname_32_0.txt", "name_32_37"): ("sz", "退市"),
}

# 只读 _0 文件。_1 文件是**历史名称表**（`000001=深发展A@h1|S深发展A@h2`，
# @h1/@h2 是曾用名），读了会用旧名覆盖新名。踩过一次，记在这里。
CODE_TABLE_FILES = ("stockname_16_0.txt", "stockname_32_0.txt")

# 场内基金段里混着 ETF / LOF / 分级基金，按名称细分
def _fund_kind(name: str) -> str:
    if "ETF" in name:
        return "ETF"
    if "LOF" in name:
        return "LOF"
    if name.endswith(("A", "B")) and "分级" in name:
        return "分级基金"
    return "基金"


class ThsError(Exception):
    pass


def find_install() -> str | None:
    """找到同花顺安装目录。"""
    env = os.environ.get("THS_HOME")
    if env and os.path.isdir(env):
        return env
    for root in DEFAULT_ROOTS:
        if os.path.isdir(os.path.join(root, "stockname")):
            return root
    return None


# ---------------------------------------------------------------- 本地代码表

@dataclass
class Instrument:
    code: str        # sh600519 / sz000001
    name: str
    kind: str        # 股票 | ETF | 指数 | 未知

    def to_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "kind": self.kind}


def _classify(code6: str, name: str) -> str:
    """粗分类。只看代码段和名称，够用且不依赖外部数据。"""
    if code6.startswith(("1A", "1B", "1C")) or "指数" in name:
        return "指数"
    if code6.startswith(("51", "56", "58", "50", "15", "16", "18")) or "ETF" in name:
        return "ETF"
    if code6[0] in "0369" or code6.startswith(("60", "68", "00", "30", "20")):
        return "股票"
    return "未知"


def load_code_table(install: str | None = None,
                    kinds: tuple[str, ...] | None = None) -> dict[str, Instrument]:
    """读同花顺本地的全市场代码表。纯本地操作，不发任何请求。

    kinds 可以只取某几类，例如 `kinds=("股票", "ETF")` —— 这样拿到的是
    干净的 A股 + ETF 全市场清单，不含债券、B股、指数。
    """
    root = install or find_install()
    if not root:
        raise ThsError(
            "找不到同花顺安装目录。装了的话把路径写到环境变量 THS_HOME，"
            "例如 set THS_HOME=C:\\同花顺软件\\同花顺"
        )
    sn_dir = os.path.join(root, "stockname")
    if not os.path.isdir(sn_dir):
        raise ThsError(f"{sn_dir} 不存在")

    out: dict[str, Instrument] = {}
    for fname in CODE_TABLE_FILES:
        path = os.path.join(sn_dir, fname)
        if not os.path.exists(path):
            continue
        try:
            # 同花顺用 GBK。gb18030 兼容面更广，解不出再退回 replace。
            with open(path, "r", encoding="gb18030", errors="replace") as fh:
                section = ""
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("["):
                        section = line.strip("[]")
                        continue
                    if line.startswith("ConfigVer") or "=" not in line:
                        continue

                    mapped = SECTION_MAP.get((fname, section))
                    if mapped is None:
                        continue          # 债券段、B股段等等，直接跳过
                    market, kind = mapped

                    code6, _, rest = line.partition("=")
                    if not code6 or not rest:
                        continue
                    # 格式 600519=贵州茅台|贵州茅台@f
                    name = rest.split("|")[0].split("@")[0].strip()
                    if not name:
                        continue
                    if kind == "基金":
                        kind = _fund_kind(name)
                    full = f"{market}{code6}"
                    out[full] = Instrument(full, name, kind)
        except OSError as exc:
            raise ThsError(f"读取 {path} 失败: {exc}") from exc

    if kinds:
        out = {k: v for k, v in out.items() if v.kind in kinds}
    if not out:
        raise ThsError(f"{sn_dir} 里没有可用的代码表文件")
    return out


def tradable_universe(install: str | None = None) -> dict[str, Instrument]:
    """可以交易的品种：A股 + ETF + LOF。

    有意排除：指数（不可交易）、B股（另开账户）、债券（不在策略范围）、
    ST股和退市股（风控直接排除，但要单独拿到用于提示）。
    """
    return load_code_table(install, kinds=("股票", "ETF", "LOF", "基金"))


def risk_excluded(install: str | None = None) -> dict[str, Instrument]:
    """被风控排除的品种 —— ST 和退市。同花顺单独成段，直接取。"""
    return load_code_table(install, kinds=("ST股", "退市"))


# ---------------------------------------------------------------- 行情接口

def _get(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": REFERER, "Accept": "*/*",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _unwrap(text: str) -> dict:
    """同花顺返回的是 JSONP：quotebridge_xxx({...})"""
    m = re.search(r"\((\{.*\})\)\s*;?\s*$", text.strip(), re.S)
    if not m:
        raise ThsError(f"不是预期的 JSONP 格式: {text[:80]!r}")
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise ThsError(f"JSON 解析失败: {exc}") from exc


def _ths_code(code: str) -> str:
    """sh600519 → hs_600519（同花顺把沪深都记作 hs_）"""
    c = code.lower()
    if c.startswith(("sh", "sz")):
        return "hs_" + c[2:]
    return "hs_" + c


@dataclass
class ThsSnapshot:
    code: str
    name: str
    date: str
    price: float
    open: float
    high: float
    low: float
    volume: float       # 股
    amount: float       # 元
    source: str = "ths"

    @property
    def volume_shou(self) -> float:
        return self.volume / 100.0

    def to_dict(self) -> dict:
        return {**self.__dict__, "volume_shou": self.volume_shou}


def fetch_snapshot(code: str, timeout: int = 10) -> ThsSnapshot:
    """实时快照（`today.js`，实测 283 字节/只）。

    收盘后返回的是最近一个交易日的收盘数据。字段编号是同花顺的私有编码，
    下面用到的几个已通过与腾讯源逐字段核对确认：

        1=日期  7=开盘  8=最高  9=最低  11=现价  13=成交量(股)  19=成交额(元)
    """
    payload = _unwrap(_get(f"{BASE}/line/{_ths_code(code)}/01/today.js", timeout))
    node = payload.get(_ths_code(code))
    if not isinstance(node, dict):
        # 少数情况直接平铺在顶层
        node = payload if "11" in payload else None
    if not node:
        raise ThsError(f"返回结构里没有 {_ths_code(code)}: {list(payload)[:5]}")

    def f(key: str) -> float:
        try:
            return float(node.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    return ThsSnapshot(
        code=code,
        name=str(node.get("name") or code),
        date=str(node.get("1") or ""),
        open=f("7"), high=f("8"), low=f("9"),
        price=f("11"), volume=f("13"), amount=f("19"),
    )


@dataclass
class ThsBar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float       # 股
    amount: float       # 元


def fetch_daily(code: str, year: int | None = None, timeout: int = 15) -> list[ThsBar]:
    """日线。不传 year 取最近约 140 个交易日（last.js）。

    字段顺序（已与腾讯源核对）：
        date, open, high, low, close, volume(股), amount(元), 换手率, ..., ...
    """
    seg = f"{year}" if year else "last"
    payload = _unwrap(_get(f"{BASE}/line/{_ths_code(code)}/01/{seg}.js", timeout))
    data = payload.get("data")
    if not isinstance(data, str):
        raise ThsError(f"没有 data 字段: {list(payload)[:8]}")

    bars: list[ThsBar] = []
    for rec in data.split(";"):
        f = rec.split(",")
        if len(f) < 7 or len(f[0]) != 8:
            continue
        try:
            bars.append(ThsBar(
                date=f"{f[0][:4]}-{f[0][4:6]}-{f[0][6:]}",
                open=float(f[1]), high=float(f[2]),
                low=float(f[3]), close=float(f[4]),
                volume=float(f[5]), amount=float(f[6]),
            ))
        except (ValueError, IndexError):
            continue
    bars.sort(key=lambda b: b.date)
    return bars


def fetch_realhead(code: str, timeout: int = 10) -> dict:
    """实时盘口（含五档）。返回原始字段字典 —— 字段编号未全部核对过，
    所以原样给出，由调用方决定用哪些，不猜。"""
    payload = _unwrap(_get(f"{BASE}/realhead/{_ths_code(code)}/last.js", timeout))
    items = payload.get("items")
    return items if isinstance(items, dict) else payload


def available_sources(install: str | None = None) -> dict:
    """给工作台用的数据源自检。"""
    root = install or find_install()
    info: dict = {
        "install_dir": root,
        "code_table": False,
        "n_tradable": 0,
        "n_excluded": 0,
        "by_kind": {},
        "table_date": "",
    }
    if not root:
        return info
    sn = os.path.join(root, "stockname")
    for fname in CODE_TABLE_FILES:
        path = os.path.join(sn, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="gb18030", errors="replace") as fh:
                for line in fh:
                    if line.startswith("ConfigVer="):
                        # ConfigVer=20261014_2552009064，前缀就是数据日期
                        v = line.strip().split("=", 1)[1].split("_")[0]
                        if len(v) == 8 and v > info["table_date"]:
                            info["table_date"] = v
                        break
        except OSError:
            pass
    try:
        uni = tradable_universe(root)
        info["code_table"] = True
        info["n_tradable"] = len(uni)
        by_kind: dict[str, int] = {}
        for inst in uni.values():
            by_kind[inst.kind] = by_kind.get(inst.kind, 0) + 1
        info["by_kind"] = by_kind
        info["n_excluded"] = len(risk_excluded(root))
    except ThsError as exc:
        info["error"] = str(exc)
    return info
