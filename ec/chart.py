"""K线 / 分时数据（腾讯行情，免费、无需授权、我们已经在用）。

接口已验证（2026-10-20 实测）：
  · 日K：https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh515250,day,,,120,qfq
        返回 qfqday 行格式 = [日期, 开, 收, 高, 低, 成交量] —— **注意不是 OHLC 顺序**，
        是 开/收/高/低。弄反了 K 线会整个错位。
  · 分时：https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh515250
        返回 data.data 行格式 = [HHMM, 价格, 累计成交量, 累计成交额]
        午休时只到 11:30，下午开盘后继续追加。

零第三方依赖：urllib + json。
"""
from __future__ import annotations

import json
import urllib.request

KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
             "?param={code},day,,,{n},{fq}")
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}"


def fetch_kline(code: str, n: int = 120, fq: str = "qfq") -> dict:
    """日K线。返回 {"code", "rows": [(date, open, close, high, low, vol), ...]}。

    行里是 开/收/高/低 顺序（腾讯的格式），这里保持原样，
    画图时按位置取，别按 OHLC 想当然。
    """
    url = KLINE_URL.format(code=code, n=n, fq=fq)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    data = (d.get("data") or {}).get(code) or {}
    raw = data.get(f"{fq}day") or data.get("day") or []
    rows = []
    for row in raw:
        if len(row) < 6:
            continue
        try:
            rows.append((str(row[0]), float(row[1]), float(row[2]),
                         float(row[3]), float(row[4]), float(row[5])))
        except (TypeError, ValueError):
            continue
    return {"code": code, "rows": rows}


def fetch_minute(code: str) -> dict:
    """当日分时。返回 {"code", "rows": [(hhmm, price, cum_vol, cum_amount), ...]}。

    昨收不在这里 —— 用 quotes.fetch_quotes 的 prev_close 补。
    """
    url = MINUTE_URL.format(code=code)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    data = ((d.get("data") or {}).get(code) or {}).get("data") or {}
    raw = data.get("data") or []
    rows = []
    for row in raw:
        if not row:
            continue
        try:
            parts = str(row).split()
            rows.append((parts[0], float(parts[1]), float(parts[2]),
                         float(parts[3])))
        except (IndexError, TypeError, ValueError):
            continue
    return {"code": code, "rows": rows}


def sma(vals: list[float], window: int) -> list[float | None]:
    """简单均线，前 window-1 个位置为 None。"""
    out: list[float | None] = [None] * len(vals)
    if len(vals) < window:
        return out
    s = sum(vals[:window])
    out[window - 1] = s / window
    for i in range(window, len(vals)):
        s += vals[i] - vals[i - window]
        out[i] = s / window
    return out
