"""截屏 + 本地视觉模型，用来读同花顺界面。

为什么需要它
------------
同花顺界面里有些信息**任何 API 都拿不到**：

    · 你自己的自选股列表
    · 自定义板块
    · 条件选股的筛选结果
    · 分时图 / K 线的形态（这个本身就是视觉的）
    · 盘口异动、板块热度的呈现方式

这些走截屏 + 视觉模型是合理的。

但同时要清楚它**不该干什么**：

    · 不替代行情接口。实时价格、日线数据、代码名称，API 更准更快更稳
    · **不把读出的数字直接喂进交易决策**。4B 模型读数字会出错，
      一笔交易按错的数字算，比没有数据更危险

所以整条链是这样的：

    截图 → 视觉模型读出「有哪些标的」→ 用行情 API 拉真实数据 → 才进决策
             ↑
      截图只负责「告诉你该去看哪些」，事实永远以 API 为准
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime

from .config import Config

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

# 同花顺主程序名。也可能是别的券商的行情端，所以做成可配。
DEFAULT_PROCESSES = ("hexin", "hexinlauncher")

_CAPTURE_PS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_capture.ps1")


def _capture_script() -> str:
    """读截图脚本。

    放在独立文件里而不是内嵌字符串：里面有约 60 行 PowerShell，
    混在 Python 里既不好改也不好排错。

    必须加 BOM：Windows PowerShell 5.1 读 .ps1 时没有 BOM 会按 GBK 解码，
    中文注释会把语法打乱（实测报「字符串缺少终止符」）。

    而且**只能有一个** BOM：重复的 BOM 会让 param() 块不被识别，
    所有参数变成 null，最后在 $bmp.Save() 处报 ArgumentNullException。
    实测踩过一次，排查了很久。
    """
    with open(_CAPTURE_PS, "r", encoding="utf-8") as fh:
        src = fh.read()
    return "\ufeff" + src.lstrip("\ufeff")



class ScreenError(Exception):
    pass


@dataclass
class Capture:
    path: str
    width: int = 0
    height: int = 0
    target: str = "screen"
    window_title: str = ""
    captured_at: str = ""
    bytes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _pwsh() -> str:
    for cand in ("pwsh", "powershell"):
        p = shutil.which(cand)
        if p:
            return p
    raise ScreenError("找不到 PowerShell")


def capture(target: str = "window", process: str | None = None,
            out_path: str | None = None, timeout: int = 40) -> Capture:
    """抓一张图。

    target: "window" 抓指定进程的主窗口，"screen" 抓整屏。
    窗口没在运行时抛 ScreenError，不返回黑图或旧图。
    """
    proc = process or DEFAULT_PROCS[0]
    path = out_path or os.path.join(tempfile.gettempdir(),
                                    f"cap-{datetime.now():%Y%m%d-%H%M%S}.png")
    script = os.path.join(tempfile.gettempdir(), "_dsh_capture.ps1")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(_capture_script())

    argv = [_pwsh(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
            "-Target", target, "-OutPath", path, "-ProcName", proc]
    try:
        proc_res = subprocess.run(argv, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ScreenError(f"截图超时（{timeout}s）")
    except OSError as exc:
        raise ScreenError(f"调用 PowerShell 失败：{exc}")

    out = (proc_res.stdout or "").strip()
    if proc_res.returncode != 0 or not out:
        err = out or (proc_res.stderr or "").strip()[-300:]
        raise ScreenError(f"截图失败：{err}")

    meta: dict = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                meta = json.loads(line)
            except json.JSONDecodeError:
                pass
    if meta.get("error"):
        if meta["error"] == "process_not_running":
            raise ScreenError(f"进程 {proc} 没有在运行。先把同花顺打开。")
        raise ScreenError(f"截图失败：{meta['error']}")
    if not os.path.exists(path):
        raise ScreenError("截图文件没有生成")

    return Capture(
        path=path,
        width=int(meta.get("width") or 0),
        height=int(meta.get("height") or 0),
        target=target,
        window_title=str(meta.get("title") or ""),
        captured_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        bytes=os.path.getsize(path),
    )


DEFAULT_PROCS = DEFAULT_PROCESSES


def list_windows() -> list[dict]:
    """列出有窗口的进程，方便确认该抓哪个。"""
    argv = [_pwsh(), "-NoProfile", "-Command",
            "Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | "
            "Select-Object ProcessName, Id, MainWindowTitle | ConvertTo-Json -Compress"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return []
    try:
        data = json.loads((r.stdout or "").strip() or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return data


# ---------------------------------------------------------------- 视觉模型

def _b64(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def vision_available(cfg: Config) -> tuple[str, str]:
    """返回 (可以用来看图的模型名, 说明)。

    只有 `ollama show <model>` 里带 vision 的才能看图。
    """
    from .ai_review import _ollama_exe  # 复用同一套探测
    exe = shutil.which("ollama") or _ollama_exe()
    if not exe:
        return "", "找不到 ollama"

    names: list[str] = []
    for m in cfg.ai.prefilter.models:
        if m.model:
            names.append(m.model)
    # 常见视觉模型兜底：本地没配也能用
    for cand in ("qwen3.5:4b", "qwen2.5vl:7b", "minicpm-v", "llava:7b", "glm4v:9b"):
        if cand not in names:
            names.append(cand)

    for n in names:
        try:
            r = subprocess.run([exe, "show", n], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if r.returncode == 0 and "vision" in (r.stdout or "").lower():
            return n, "ok"
    return "", "本地没有装了 vision 能力的模型（可用 qwen3.5:4b / qwen2.5vl:7b / minicpm-v）"


def _ollama_generate(body: dict, timeout: int) -> dict:
    """调 ollama，并处理思考型模型的坑。

    qwen3.5 这类模型**默认开着思考模式**，会把 num_predict 全烧在 thinking 上，
    结果是 done_reason=length、response 几乎为空 —— 看起来像「模型看不了图」，
    实际是「它想到了但没来得及说话」。实测同一张图：

        默认       9.6s  600 token 全用于思考  response 被截断
        think=false 1.1s  干净输出

    所以这里统一先带 think=false；如果该模型不支持这个参数（老模型会报错），
    去掉再试一次。
    """
    payload = dict(body)
    payload["think"] = False
    for attempt in (0, 1):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(OLLAMA_URL, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if attempt == 0 and exc.code == 400 and "think" in detail.lower():
                payload.pop("think", None)   # 该模型不支持 think，去掉重试
                continue
            raise ScreenError(f"ollama 报错 {exc.code}：{detail[:200]}")
        except urllib.error.URLError as exc:
            raise ScreenError(f"连不上 ollama：{getattr(exc, 'reason', exc)}")
        except (TimeoutError, OSError) as exc:
            raise ScreenError(f"视觉模型超时或失败：{exc}")
        except json.JSONDecodeError as exc:
            raise ScreenError(f"ollama 返回的不是 JSON：{exc}")
    raise ScreenError("ollama 调用失败")


def ask_vision(cfg: Config, image_path: str, prompt: str,
               model: str | None = None, timeout: int = 300,
               num_predict: int = 800) -> str:
    """把一张图连同问题丢给本地视觉模型，返回原始文本。"""
    if not os.path.exists(image_path):
        raise ScreenError(f"图片不存在：{image_path}")
    if model is None:
        model, why = vision_available(cfg)
        if not model:
            raise ScreenError(why)

    payload = _ollama_generate({
        "model": model,
        "prompt": prompt,
        "images": [_b64(image_path)],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": num_predict},
    }, timeout)

    resp = payload.get("response") or ""
    if not resp.strip():
        # 空输出时把原因说清楚，不假装「没有内容」
        reason = payload.get("done_reason", "")
        n_think = len(payload.get("thinking") or "")
        raise ScreenError(
            f"视觉模型返回空。done_reason={reason}"
            + (f"，思考了 {n_think} 字但没输出结论" if n_think else "")
            + "。可能是 num_predict 太小被思考耗尽，调大 num_predict 或确认 think=false 生效。")
    return resp


WATCHLIST_PROMPT = """这是股票软件「同花顺」的截图，中间有一张「自选股」表格。

表格最左边有一列是**序号**（1、2、3……），那一列不要读。
要从**代码**那一列开始读。

每行输出五个字段，用竖线分隔：
代码|名称|涨幅|现价|备注

规则：
- 只读表格里的行，不要读左边的菜单、推荐内容、新闻
- **不要输出序号列**
- 代码照抄（例如 MSFT、515250、000001、399006）
- 涨幅和现价照抄屏幕上的数字，看不清就写 ?
- 名称照抄，如果被省略号截断了就照抄带省略号的
- 不要计算、不要换算单位、不要补充你猜的信息
- 没有表格或看不清，就只输出 NO_TABLE

只输出表格行，不要任何解释，不要 markdown 表格线。"""


def _strip_row_number(parts: list[str]) -> list[str]:
    """模型有时会把最左边的「序号」列也读进来，导致整体右移一格。

    判据：第一个字段是 1~2 位纯数字。真实代码不会是 1~2 位数字
    （A 股 / ETF 是 6 位，美股是字母，同花顺的自选股序号是 1、2、3…）。
    """
    if len(parts) >= 2 and parts[0].isdigit() and len(parts[0]) <= 2:
        return parts[1:]
    return parts


@dataclass
class WatchItem:
    code: str
    name: str
    pct: str = ""
    price: str = ""
    note: str = ""
    raw_code: str = ""           # 截图里的原始代码，保留以便排查
    parse_error: str = ""        # 代码解析失败的原因，不静默丢
    verified: bool = False       # 是否已用行情 API 核对
    api_name: str = ""
    api_price: float = 0.0
    api_pct: float = 0.0
    mismatch: str = ""           # 截图 vs API 的差异说明

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize_code(raw: str, name: str = "") -> str:
    """截图里得到的代码补上市场前缀。

    ⚠️ 有个真实的坑：**000001 既是上证指数也是平安银行。**
        上证指数 → sh000001（腾讯口径）/ 1A0001（同花顺口径）
        平安银行 → sz000001
    只看代码分不出来，必须结合名称里的「指数」「指」来判断。
    早期版本把 000001 上证指数认成了 sz000001 平安银行，
    所以这里把名称也纳进来。
    """
    c = raw.strip().upper()
    if not c:
        return ""
    if c.isalpha():
        return c                       # 美股代码，不加前缀
    digits = "".join(ch for ch in c if ch.isdigit())
    if len(digits) != 6:
        return ""                      # 位数不对，交给上层记录为「解析不了」

    is_index = ("指数" in name) or name.endswith("指")
    if digits.startswith("399") or digits.startswith(("15", "16", "18")):
        return "sz" + digits
    if digits.startswith("000"):
        # 上证指数是 sh000001，深市股票才是 sz000xxx
        return ("sh" if is_index else "sz") + digits
    if digits[0] in "569":
        return "sh" + digits
    if digits[0] in "0123":
        return "sz" + digits
    return ""


def read_watchlist(cfg: Config, cap: Capture,
                   model: str | None = None) -> list[WatchItem]:
    """从截图里读出同花顺的自选股列表。

    ⚠️ 读出来的价格和涨幅**不可信**，只当作「有哪些标的」的线索。
    要数字请走 `verify_with_api()`。
    """
    text = ask_vision(cfg, cap.path, WATCHLIST_PROMPT, model=model)
    if "NO_TABLE" in text:
        return []

    items: list[WatchItem] = []
    for line in text.splitlines():
        line = line.strip().strip("|").strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        parts = _strip_row_number(parts)
        if len(parts) < 2:
            continue
        raw_code = parts[0]
        name = parts[1] if len(parts) > 1 else ""

        # 视觉模型有时会把表头也当成一行数据读出来（代码栏是「代码」、名称栏是「名称」）。
        # 那不是标的，丢掉，否则结果里会多一行「代码 名称 — — 未核对」。
        if _is_header_row(raw_code, name):
            continue

        code = _normalize_code(raw_code, name)
        items.append(WatchItem(
            code=code,
            name=name,
            pct=parts[2] if len(parts) > 2 else "",
            price=parts[3] if len(parts) > 3 else "",
            note=parts[4] if len(parts) > 4 else "",
            raw_code=raw_code,
            # 解析不出来就记下原因，不静默丢掉 —— 丢掉的话你会以为模型没读到
            parse_error="" if code else f"代码「{raw_code}」无法识别（位数或格式不对）",
        ))
    return items


_HEADER_WORDS = {"代码", "名称", "现价", "涨幅", "序号", "代码名称",
                 "名称代码", "股票代码", "股票名称"}


def _is_header_row(raw_code: str, name: str) -> bool:
    """判断这一行是不是表头。

    实测 qwen3.5:4b 读同花顺自选股时，会把「代码 名称 ...」这一行
    也当成数据行输出，结果里多出一条「代码 名称 — — 未核对」。
    """
    a = (raw_code or "").strip()
    b = (name or "").strip()
    if a in _HEADER_WORDS and b in _HEADER_WORDS:
        return True
    if a == "代码" or a == "序号":
        return True
    # 代码栏里出现中文且完全不含数字 → 不是代码
    if a and not any(ch.isdigit() for ch in a) and a in _HEADER_WORDS:
        return True
    return False


def verify_with_api(items: list[WatchItem]) -> list[WatchItem]:
    """用行情接口核对截图读出来的东西。

    **这一步是必须的。** 视觉模型读数字会出错，而交易不能建立在错的数字上。
    核对完，`api_*` 字段才是事实，`pct`/`price` 只是模型的读法。

    对不上的会写进 `mismatch`，让用户看见，而不是悄悄覆盖。
    """
    from .quotes import fetch_quotes

    a_codes = [i.code for i in items if i.code and not i.code.isalpha()]
    if not a_codes:
        return items
    try:
        quotes = fetch_quotes(a_codes)
    except Exception:
        return items

    for it in items:
        q = quotes.get(it.code)
        if not q:
            continue
        it.verified = True
        it.api_name = q.name
        it.api_price = round(q.price, 4)
        it.api_pct = round(q.pct * 100, 2)

        notes = []
        # 名称核对。截图里的长名称常被省略号截断（「科创创新药ETF...」），
        # 这种情况按前缀比对，不算不符 —— 否则每次都是假警报。
        shot_name = it.name.strip().rstrip(".．…·")
        if shot_name and q.name:
            truncated = it.name.strip().endswith(("...", "…", ".."))
            if truncated:
                if not q.name.startswith(shot_name) and shot_name not in q.name:
                    notes.append(f"名称前缀不符：截图「{it.name}」/ 接口「{q.name}」")
            elif shot_name not in q.name and q.name not in shot_name:
                notes.append(f"名称不符：截图「{it.name}」/ 接口「{q.name}」")
        # 价格核对（差 0.5% 以上视为读错）
        try:
            shot_px = float(it.price)
        except (TypeError, ValueError):
            shot_px = None
        if shot_px and q.price:
            dev = abs(shot_px - q.price) / q.price
            if dev > 0.005:
                notes.append(f"价格偏差 {dev*100:.1f}%：截图 {shot_px} / 接口 {q.price:.3f}")
        if notes:
            it.mismatch = "；".join(notes)
    return items
