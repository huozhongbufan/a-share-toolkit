"""行情抓取。只用标准库，避免任何依赖安装问题。

三个源，按可靠性排序，自动降级：
  1. 腾讯 qt.gtimg.cn       —— 实时快照，GB2312，最稳
  2. 新浪 hq.sinajs.cn      —— 备源，需要 Referer
  3. 东财 push2.eastmoney   —— 全市场 ETF 列表（构建池子用）

这些是公开的免费行情接口，属于非官方爬虫接口：
有限流、无 Level-2、可能随时变动。不适合作为唯一数据源做真金白银的决策。
"""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from typing import Iterable

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# 行情接口一律直连，不走代理：走代理反而更慢，而且代理挂掉时会静默失败。
_DIRECT = urllib.request.ProxyHandler({})


class QuoteError(Exception):
    pass


@dataclass
class Quote:
    code: str          # 带市场前缀，如 sh510300
    name: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    volume_shou: float     # 成交量（手）
    amount_wan: float      # 成交额（万元）
    float_mv_yi: float = 0.0   # 场内规模（亿元），用于过滤有清盘风险的小基金
    bid1: float = 0.0      # 买一价
    ask1: float = 0.0      # 卖一价
    ts: str = ""
    source: str = ""

    @property
    def pct(self) -> float:
        if not self.prev_close:
            return 0.0
        return (self.price - self.prev_close) / self.prev_close

    @property
    def spread_pct(self) -> float:
        """买卖价差占价格的比例。ETF 通常很小，但流动性差的能吃掉利润。"""
        if not self.price or not self.bid1 or not self.ask1:
            return 0.0
        return (self.ask1 - self.bid1) / self.price

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pct"] = round(self.pct, 6)
        d["spread_pct"] = round(self.spread_pct, 6)
        return d


def normalize_code(code: str) -> str:
    """把各种写法统一成 sh510300 / sz159915。

    接受：510300 / sh510300 / SH510300 / 510300.SH
    规则：5、6、9 开头在上海；0、1、2、3 开头在深圳。
    """
    c = code.strip().lower().replace(" ", "")
    m = re.match(r"^(\d{6})\.(sh|sz)$", c)
    if m:
        return f"{m.group(2)}{m.group(1)}"
    if re.match(r"^(sh|sz)\d{6}$", c):
        return c
    if re.match(r"^\d{6}$", c):
        head = c[0]
        market = "sh" if head in "569" else "sz"
        return f"{market}{c}"
    raise QuoteError(f"无法识别的代码: {code!r}")


def _http_get(url: str, referer: str | None = None, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    req.add_header("Accept-Encoding", "gzip")
    opener = urllib.request.build_opener(_DIRECT)
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def _decode_gb(raw: bytes) -> str:
    for enc in ("gb2312", "gbk", "gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# 腾讯快照字段位置（用 ~ 分隔后的下标）
_T = {
    "name": 1, "code": 2, "price": 3, "prev_close": 4, "open": 5,
    "volume": 6, "bid1": 9, "ask1": 19, "ts": 30,
    "high": 33, "low": 34, "amount": 37, "float_mv": 44,
}


def _split_codes(codes: Iterable[str]) -> tuple[list[str], list[str]]:
    """把代码分成「能查的」和「查不了的」。

    批量查询里混进一个美股代码（MSFT）不该让整批失败 —— 之前就是这样，
    一个 MSFT 直接抛异常，8 个能查的也全没拿到。
    """
    ok: list[str] = []
    bad: list[str] = []
    for c in codes:
        try:
            ok.append(normalize_code(c))
        except QuoteError:
            bad.append(c)
    return ok, bad


def fetch_tencent(codes: Iterable[str]) -> dict[str, Quote]:
    codes, _bad = _split_codes(codes)
    if not codes:
        return {}
    out: dict[str, Quote] = {}
    # 单次请求不要塞太多代码，分批更稳
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        url = "http://qt.gtimg.cn/q=" + ",".join(batch)
        text = _decode_gb(_http_get(url))
        for line in text.split(";"):
            line = line.strip()
            # 每一行长这样：v_sh000001="1~上证指数~000001~..."
            # 前缀是腾讯自己给的，直接用，不要按代码首位去猜 ——
            # 000001 既是 sh000001（上证指数）也是 sz000001（平安银行），
            # 靠猜会把指数认成股票。
            m = re.match(r'v_([a-z]{2})(\w+)=', line)
            if not m:
                continue
            prefix, code6 = m.group(1), m.group(2)
            _, _, payload = line.partition("=")
            f = payload.strip().strip('"').split("~")
            if len(f) < 40 or not f[1]:
                continue

            def num(idx: int) -> float:
                try:
                    return float(f[idx])
                except (ValueError, IndexError):
                    return 0.0

            key = f"{prefix}{code6}"
            out[key] = Quote(
                code=key,
                name=f[_T["name"]],
                price=num(_T["price"]),
                prev_close=num(_T["prev_close"]),
                open=num(_T["open"]),
                high=num(_T["high"]),
                low=num(_T["low"]),
                volume_shou=num(_T["volume"]),
                amount_wan=num(_T["amount"]),
                float_mv_yi=num(_T["float_mv"]),
                bid1=num(_T["bid1"]),
                ask1=num(_T["ask1"]),
                ts=f[_T["ts"]] if len(f) > _T["ts"] else "",
                source="tencent",
            )
        time.sleep(0.12)
    return out


def fetch_sina(codes: Iterable[str]) -> dict[str, Quote]:
    """新浪备源。字段顺序和腾讯完全不同，这里单独解析。"""
    codes, _bad = _split_codes(codes)
    if not codes:
        return {}
    out: dict[str, Quote] = {}
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    text = _decode_gb(_http_get(url, referer="https://finance.sina.com.cn/"))
    for line in text.split(";"):
        m = re.match(r'\s*var hq_str_(\w+)="(.*)"', line)
        if not m:
            continue
        key, payload = m.group(1), m.group(2)
        f = payload.split(",")
        if len(f) < 32 or not f[0]:
            continue

        def num(idx: int) -> float:
            try:
                return float(f[idx])
            except (ValueError, IndexError):
                return 0.0

        # 新浪: 0名称 1开 2昨收 3现价 4高 5低 6买一 7卖一 ... 8成交量(股) 9成交额(元)
        vol_shares = num(8)
        out[key] = Quote(
            code=key,
            name=f[0],
            price=num(3),
            prev_close=num(2),
            open=num(1),
            high=num(4),
            low=num(5),
            volume_shou=vol_shares / 100.0,
            amount_wan=num(9) / 10000.0,
            bid1=num(6),
            ask1=num(7),
            ts=f"{f[30]} {f[31]}" if len(f) > 31 else "",
            source="sina",
        )
    return out


def fetch_quotes(codes: Iterable[str]) -> dict[str, Quote]:
    """带降级链的快照抓取。腾讯失败自动切新浪。"""
    codes = list(codes)
    try:
        got = fetch_tencent(codes)
        if got:
            return got
    except Exception:
        pass
    return fetch_sina(codes)


def fetch_etf_list(min_amount_wan: float = 0.0) -> list[dict]:
    """东财全市场 ETF 列表（约 1600 只）。

    返回字段：code / name / price / pct / amount_wan。用于动态构建候选池，
    而不是把 ETF 代码硬编码在脚本里。
    """
    base = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pn={pn}&pz=200&po=1&np=1&fltt=2&invt=2&fid=f6"
        "&fs=b:MK0021,b:MK0022,b:MK0023,b:MK0024"
        "&fields=f12,f13,f14,f2,f3,f6"
    )
    rows: list[dict] = []
    for pn in range(1, 12):
        url = base.format(pn=pn)
        raw = _http_get(url, referer="https://quote.eastmoney.com/")
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise QuoteError(f"东财列表解析失败: {exc}") from exc
        data = payload.get("data") or {}
        diff = data.get("diff") or []
        if not diff:
            break
        for it in diff:
            code6 = str(it.get("f12", ""))
            price = it.get("f2")
            amount = it.get("f6")
            if not code6 or price in (None, "-") or amount in (None, "-"):
                continue
            amount_wan = float(amount) / 10000.0
            if amount_wan < min_amount_wan:
                continue
            rows.append({
                "code": normalize_code(code6),
                "name": str(it.get("f14", "")),
                "price": float(price),
                "pct": float(it.get("f3") or 0.0),
                "amount_wan": amount_wan,
            })
        time.sleep(0.15)
    return rows


# ---------------------------------------------------------------- 日 K 线

KLINE_URL = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq"


@dataclass
class Bar:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float


def fetch_daily_bars(code: str, count: int = 320) -> list[Bar]:
    """前复权日 K。返回按日期升序排列的 Bar 列表。

    腾讯返回 [date, open, close, high, low, volume]，注意是 open/close/high/low
    这个顺序，不是常见的 OHLC。
    """
    code = normalize_code(code)
    n = max(30, min(int(count), 1500))
    url = KLINE_URL.format(code=code, n=n)
    raw = _http_get(url)
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise QuoteError(f"{code} 日K解析失败: {exc}") from exc
    node = (payload.get("data") or {}).get(code) or {}
    rows = node.get("qfqday") or node.get("day") or []
    bars: list[Bar] = []
    for r in rows:
        if len(r) < 6:
            continue
        try:
            bars.append(Bar(
                date=str(r[0]),
                open=float(r[1]),
                close=float(r[2]),
                high=float(r[3]),
                low=float(r[4]),
                volume=float(r[5]),
            ))
        except (TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b.date)
    return bars
