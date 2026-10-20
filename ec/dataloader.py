"""日线数据加载与本地缓存。

缓存策略：同一天内不重复请求。行情接口是免费公开的爬虫接口，
不该被我们的循环打成 DDoS。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict

from .config import Config
from .quotes import Bar, fetch_daily_bars


class BarStore:
    def __init__(self, cfg: Config, ttl_sec: int = 6 * 3600):
        self.dir = os.path.join(cfg.data_dir, "bars")
        self.ttl = ttl_sec
        os.makedirs(self.dir, exist_ok=True)
        self._mem: dict[str, list[Bar]] = {}
        self.fetches = 0
        self.hits = 0

    def _path(self, code: str) -> str:
        return os.path.join(self.dir, f"{code}.json")

    def get(self, code: str, count: int = 320) -> list[Bar]:
        if code in self._mem:
            self.hits += 1
            return self._mem[code]

        path = self._path(code)
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < self.ttl:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rows = json.load(fh)
                bars = [Bar(**r) for r in rows]
                if len(bars) >= 60:
                    self.hits += 1
                    self._mem[code] = bars
                    return bars
            except (OSError, json.JSONDecodeError, TypeError):
                pass

        bars = self._fetch(code, count)
        self.fetches += 1
        if bars:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump([asdict(b) for b in bars], fh)
            except OSError:
                pass
        self._mem[code] = bars
        return bars

    def _fetch(self, code: str, count: int) -> list[Bar]:
        """抓日线：腾讯主 + 同花顺备。

        实测腾讯的日线接口对个别代码会返回 302→501，一次就把整个循环打断。
        同花顺的 `d.10jqka.com.cn` 是独立域名，可以兜底。
        两边字段核对过：510300 在 20261014 的收盘/成交量/成交额完全一致。
        """
        try:
            bars = fetch_daily_bars(code, count)
            if bars:
                return bars
        except Exception:
            pass
        try:
            from . import ths
            raw = ths.fetch_daily(code)
            if raw:
                return [Bar(date=b.date, open=b.open, high=b.high,
                            low=b.low, close=b.close, volume=b.volume)
                        for b in raw]
        except Exception:
            pass
        return []

    def recent_closes(self, code: str, n: int = 20) -> list[float]:
        bars = self.get(code)
        return [round(b.close, 3) for b in bars[-n:]]

    def latest_date(self, code: str) -> str:
        bars = self.get(code)
        return bars[-1].date if bars else ""
