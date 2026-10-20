"""第 1 层 · 本地小模型（ollama）负责「从同花顺检索信息」。

它干什么
--------
在同花顺本地那 7682 只标的里，按自然语言找东西：

    "茅台"            → sh600519 贵州茅台
    "白酒类 ETF"      → 相关的几只
    "新能源"          → 名称里带新能源的
    "红利"            → 红利类 ETF / 股票

为什么用本地小模型而不是写死规则
--------------------------------
同花顺的代码表是**人写的中文名称**，检索需求也是中文口语，两边没有正规对应关系：

    "白酒"  → 名称里是「消费ETF」「酿酒」而不是「白酒」
    "银行"  → 有「银行ETF」「招商银行」「江苏银行」
    "防守"  → 名称里根本没有这个词

纯字符串匹配只能处理第一种。确定性预筛负责「捞得足够宽」，
本地小模型负责「从宽的里面挑得准」。**这正是第 1 层该干的活：文本筛选。**

⚠️ 一条硬规则：**模型不产生代码，只从候选里挑。**
模型返回的任何代码都必须出现在本地候选集里，否则直接丢弃并告警。
同花顺的表是唯一事实来源，模型只在表内做选择 —— 编造代码在这个系统里零容忍。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict

from .config import Config
from . import ths

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

# 查询里的口语成分，预筛时先剥掉，避免把「帮我找」当成关键词
_STOP_PATTERNS = (
    "帮我", "给我", "找一下", "查一下", "搜一下", "看看", "有没有",
    "我想", "我要", "想要", "找", "查", "搜", "一下", "的", "是", "哪只", "哪些",
)

_KIND_WORDS = {
    "ETF": "ETF", "etf": "ETF", "基金": "ETF", "lof": "ETF", "LOF": "ETF",
    "股票": "股票", "个股": "股票", "股": "股票",
    "指数": "指数",
}

# 类别按组过滤。用户嘴上说「ETF」，实际指的是「场内基金」——
# 白酒基金LOF 就是这种情况：说「白酒类ETF」却只有 LOF，硬按 ETF 过滤会得到 0 结果。
_KIND_GROUPS = {
    "ETF": {"ETF", "LOF", "基金"},
    "股票": {"股票"},
    "指数": {"指数"},
}


@dataclass
class Hit:
    code: str
    name: str
    kind: str
    score: float = 0.0
    why: str = ""
    matched: bool = False     # 名称里是否真的含有查询关键词（防止模型编造相关性）
    price: float = 0.0        # 可选：补行情后填
    source: str = "ths_local"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SearchResult:
    query: str
    keywords: list[str] = field(default_factory=list)
    kind_filter: str = ""
    n_table: int = 0
    n_prescreen: int = 0
    used_llm: bool = False
    hits: list[Hit] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "keywords": self.keywords,
            "kind_filter": self.kind_filter,
            "n_table": self.n_table,
            "n_prescreen": self.n_prescreen,
            "used_llm": self.used_llm,
            "hits": [h.to_dict() for h in self.hits],
            "note": self.note,
        }


def _clean_query(q: str) -> str:
    out = q.strip()
    for p in _STOP_PATTERNS:
        out = out.replace(p, " ")
    return re.sub(r"\s+", " ", out).strip()


def _keywords(q: str) -> list[str]:
    """把查询切成检索用的关键词。

    三个坑，都是实测踩出来的：

    1. **类别词不能当名称关键词。** "ETF" 出现在所有 ETF 名字里，一旦当成
       关键词就会把 60 个候选全灌成 ETF，模型在噪声里只能硬凑答案。
       类别词只用来做类别过滤。
    2. **3~4 字的词组要拆开再切。** "白酒类" 整串匹配不到 "白酒基金"，
       必须额外产出 "白酒" / "酒类" 这样的二字窗口。
    3. 单字丢掉 —— 太泛，会把半个表捞进来。
    """
    toks: list[str] = []
    for m in re.finditer(r"[A-Za-z0-9]{2,}", q):
        toks.append(m.group(0))

    han = re.sub(r"[A-Za-z0-9\s]", " ", q)
    for chunk in han.split():
        if len(chunk) < 2:
            continue
        if len(chunk) <= 3:
            toks.append(chunk)
            if len(chunk) == 3:
                # "白酒类" 额外产出 "白酒"、"酒类"
                toks.extend(chunk[i:i + 2] for i in range(2))
        else:
            toks.append(chunk)
            toks.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))

    kind_set = set(_KIND_WORDS)
    seen, out = set(), []
    for t in toks:
        if len(t) >= 2 and t not in seen and t not in kind_set:
            seen.add(t)
            out.append(t)
    return out


EXPAND_PROMPT = """用户想在 A 股 / ETF 里找某种标的，但他用的词可能不是名称里的词。

例子：用户说「防守型」，名称里不会写「防守型」，但会写「红利」「银行」「黄金」「低波」。
例子：用户说「吃药喝酒」，名称里会写「医药」「白酒」。

用户说：%s

请给出 3~5 个**最可能出现在中文基金/股票名称里**的关键词（2~4 个汉字）。
只输出 JSON，不要解释：

{"keywords": ["红利", "银行"]}"""


def expand_keywords(model: str, query: str, timeout: int = 60) -> list[str]:
    """本地小模型把口语查询翻译成「名称里可能出现的词」。

    这是模型在这一层最该干的活：**语义映射**。
    它只产出关键词，不产出代码 —— 代码永远来自同花顺本地表，
    所以就算它翻译错了，最坏结果也只是搜不到，不会搜出假标的。
    """
    data = _call_ollama(model, EXPAND_PROMPT % query, timeout)
    if not data:
        return []
    raw = data.get("keywords")
    if not isinstance(raw, list):
        return []
    out = []
    for k in raw:
        k = str(k).strip()
        if 2 <= len(k) <= 6 and k not in out:
            out.append(k)
    return out[:5]


def prescreen(query: str, table: dict[str, ths.Instrument],
              limit: int = 60) -> tuple[list[ths.Instrument], list[str], str]:
    """确定性预筛：捞得足够宽，交给模型去挑。

    只放行**实质性命中**的候选 —— 名称里必须真的含有某个关键词（≥2 字）。
    泛泛命中（比如只匹配到类别词）不算，宁可为空也不灌噪声，
    因为灌噪声的结果是模型硬凑答案（实测过：「白酒类ETF」曾被返回成
    金融地产ETF + 医药ETF）。
    """
    cleaned = _clean_query(query)
    kws = _keywords(cleaned)

    kind_filter = ""
    for w, k in _KIND_WORDS.items():
        if w in query:
            kind_filter = k
            break

    pool = list(table.values())
    if kind_filter:
        allowed = _KIND_GROUPS.get(kind_filter, {kind_filter})
        sub = [i for i in pool if i.kind in allowed]
        if sub:
            pool = sub
        else:
            kind_filter = ""      # 组内一个都没有，就别过滤了，别把结果卡成空

    scored: list[tuple[int, str, ths.Instrument]] = []
    raw = cleaned.replace(" ", "")
    for inst in pool:
        score = 0
        # 整串直接命中优先级最高
        if raw and len(raw) >= 2 and raw in inst.name:
            score += 100 + len(raw)
        for k in kws:
            if k in inst.name:
                score += 10 + len(k)
        if score:
            scored.append((score, inst.code, inst))

    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [inst for _, _, inst in scored[:limit]]
    return picked, kws, kind_filter


SYSTEM = """你在帮用户从一份 A 股/ETF 代码表里检索标的。

规则：
1. **只能从给你的候选里挑**，绝对不许编造代码或名称。
2. 判断依据只看名称字面与类别的相关性，不猜测行情、不预测涨跌。
3. 如果候选里没有一个真正相关的，就返回空数组，不要凑数。
4. 最多挑 %d 个，按相关度从高到低。

只输出 JSON，不要解释文字、不要 markdown 代码块：
{"picks": [{"code": "sh600519", "score": 0.95, "why": "名称直接匹配「茅台」"}]}
score 是 0~1 的相关度。why 用一句话说明为什么相关。"""


# 要「先想再答」的模型。对它们：
#   · think=False 是必须的 —— 实测 deepseek-r1:8b 在 think=True 下，
#     不管 num_predict 给 1600 还是 2400，token 全烧在思考上，response 返回空
#     （done_reason=length）。think=False + format=json 反而干净。
#   · num_predict 要给大 —— 700 不够它想完再答。
_REASONER_MARKS = ("r1", "reasoner", "qwen3", "thinking")


def _is_reasoner(model: str) -> bool:
    m = (model or "").lower()
    return any(k in m for k in _REASONER_MARKS)


def _call_ollama(model: str, prompt: str, timeout: int = 90) -> dict | None:
    body_obj = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.0,
            "num_predict": 1600 if _is_reasoner(model) else 700,
        },
    }
    if _is_reasoner(model):
        body_obj["think"] = False
    body = json.dumps(body_obj).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    except json.JSONDecodeError:
        return None
    raw = payload.get("response") or ""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def table_age_hours() -> float | None:
    """本地代码表有多旧（小时）。这些文件只在同花顺运行时更新。"""
    import os
    import time

    for p in (r"C:\同花顺软件\同花顺\stockname\stockname_16_0.txt",
              r"C:\同花顺软件\stockname\stockname_16_0.txt"):
        if os.path.exists(p):
            return (time.time() - os.path.getmtime(p)) / 3600.0
    return None


def live_refresh(cands: list[ths.Instrument]) -> tuple[dict, bool]:
    """用实时行情刷新候选的名称和价格。

    为什么要这一步
    --------------
    本地 `stockname\\*.txt` **只在同花顺运行时**更新。几天不开同花顺，
    新上市的标的不会出现，改过名的还叫旧名字 —— 那就是在读老数据。

    做法：本地表只用来**粗筛**（离线、快、够用），筛完的几十个候选再去
    实时行情里核对名称和价格。60 只一个请求，几十个候选一到两个请求搞定。

    返回 ({code: Quote}, 是否成功)。失败返回空 dict，调用方退回本地名称。
    """
    if not cands:
        return {}, True
    from .quotes import fetch_quotes

    try:
        quotes = fetch_quotes([c.code for c in cands])
    except Exception:
        return {}, False
    return quotes or {}, bool(quotes)


def llm_rerank(query: str, cands: list[ths.Instrument], model: str,
               top_k: int = 8, timeout: int = 90,
               keywords: list[str] | None = None
               ) -> tuple[list[Hit], bool, int]:
    """让本地小模型从候选里挑。返回 (hits, 是否真的用了模型)。

    两道校验，都必要：

    1. **模型给的代码必须出现在候选集里**，否则丢弃。同花顺的表是唯一
       事实来源，模型不许编造代码。
    2. **标记名称是否真的命中关键词**。7B 模型有个通病：宁可凑数也不返回空。
       实测「白酒类ETF」它返回了金融地产ETF + 医药ETF 并编了理由。
       所以每个结果都带 `matched` 标记，界面上要能区分
       「名称直接命中」和「模型推断的相关」。
    """
    if not cands:
        return [], False, 0

    by_code = {i.code: i for i in cands}
    kws = keywords or []
    listing = "\n".join(f"{i.code}\t{i.name}\t{i.kind}" for i in cands)
    prompt = (SYSTEM % min(top_k, len(cands))) + \
        f"\n\n用户查询：{query}\n\n候选（代码\\t名称\\t类别）：\n{listing}\n"

    data = _call_ollama(model, prompt, timeout)
    if not data or "picks" not in data:
        return [], False, 0

    hits: list[Hit] = []
    invalid = 0
    for p in data["picks"]:
        code = str(p.get("code", "")).strip()
        inst = by_code.get(code)
        if inst is None:
            invalid += 1
            continue
        try:
            score = float(p.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        matched = any(k in inst.name for k in kws)
        hits.append(Hit(
            code=inst.code, name=inst.name, kind=inst.kind,
            score=round(max(0.0, min(1.0, score)), 3),
            why=str(p.get("why", ""))[:120],
            matched=matched,
        ))
    hits.sort(key=lambda h: (-int(h.matched), -h.score))
    # 注意：老版本是 invalid == 0 and bool(hits) —— 只要模型给了一个
    # 代码缺失的 pick，整批正确的都被丢掉。实测 deepseek-r1 会这样：
    # 5 个候选里 1 个没写 code，剩下 4 个全对，却被判为「失败」。
    # 现在的判定是「有有效结果就算成功」，把无效个数单独返回给上层提示。
    return hits[:top_k], bool(hits), invalid


def search(cfg: Config, query: str, top_k: int = 8,
           use_llm: bool = True, timeout: int = 90) -> SearchResult:
    """在同花顺本地代码表里按自然语言检索。

    流程：本地表（零请求）→ 确定性预筛 → 本地小模型精排 → 代码校验
    """
    t0 = time.time()
    table = ths.tradable_universe()
    res = SearchResult(query=query, n_table=len(table))

    cands, kws, kind_filter = prescreen(query, table)
    res.keywords = kws
    res.kind_filter = kind_filter
    res.n_prescreen = len(cands)

    # 本地模型按顺序降级。老版本只用 models[0]，第一个模型挂了就退化成
    # 纯关键词匹配。现在一路试到底，并把实际生效的模型写进 note。
    models: list[str] = []
    if use_llm and cfg.ai.prefilter.enabled:
        models = [m.model for m in cfg.ai.prefilter.models if m.model]
    model = models[0] if models else ""

    # 名称直接命中为空时，让模型把口语翻译成「名称里可能出现的词」再搜一次。
    # "防守型" → 红利/银行/黄金。这在纯规则下是搜不到的。
    expanded: list[str] = []
    if not cands and models:
        for m in models:
            expanded = expand_keywords(m, query, timeout=min(timeout, 60))
            if expanded:
                model = m
                break
        if expanded:
            cands, _, kind_filter2 = prescreen(" ".join(expanded), table)
            res.keywords = expanded
            res.kind_filter = kind_filter2
            res.n_prescreen = len(cands)

    if not cands:
        res.note = (f"同花顺代码表（{len(table)} 只）里没有名称命中 "
                    f"{kws or ['(无有效关键词)']} 的标的。"
                    + (f"已让本地模型扩展到 {expanded}，仍然没有。" if expanded else "")
                    + " 注意本地表只有代码和名称、没有行情 —— "
                      "「低价」「涨得多」这类条件要去行情那一层问。")
        return res

    if not models:
        res.note = "未启用本地小模型，仅返回确定性预筛结果。"
        res.hits = _finalize(cands[:top_k], res.keywords, query, {})
        return res

    hits, ok, used, invalid = [], False, model, 0
    for m in models:
        hits, ok, inv = llm_rerank(query, cands, m, top_k=top_k, timeout=timeout,
                                   keywords=res.keywords)
        if ok:
            used, invalid = m, inv
            break
    model = used
    if ok:
        res.used_llm = True
        res.hits = hits
        res.note = f"本地小模型 {model} 从 {len(cands)} 个候选里挑出 {len(hits)} 个"
        if expanded:
            res.note = (f"名称未直接命中，模型把查询扩展为 {expanded} 后"
                        f"从 {len(cands)} 个候选里挑出 {len(hits)} 个")
        n_infer = sum(1 for h in hits if not h.matched)
        if n_infer:
            res.note += (f"；其中 {n_infer} 个是模型推断的相关（名称未直接命中），"
                         f"已标注，请自己判断")
    else:
        res.hits = [Hit(code=i.code, name=i.name, kind=i.kind, score=0.5,
                        matched=any(k in i.name for k in res.keywords))
                    for i in cands[:top_k]]
        res.note = (f"本地小模型 {model} 未能给出有效结果，"
                    f"退回确定性预筛（前 {len(res.hits)} 个）。这不是模型的选择。")

    # ---- 收尾两道，都必须做 ----
    res.hits, live_ok = _finalize(res.hits, cands, res.keywords, query, top_k)
    if live_ok:
        res.note += "；名称与价格已用实时行情核对"
    else:
        res.note += "；⚠ 实时行情拉取失败，名称和价格来自本地表，可能是旧的"

    age = table_age_hours()
    if age is not None and age > 72:
        res.note += (f"；⚠ 本地代码表已 {age/24:.1f} 天没更新，"
                     f"新上市的标的不在里面 —— 开一次同花顺就会刷新")

    res.note += f"  耗时 {time.time() - t0:.1f}s"
    return res


def _finalize(hits: list[Hit], cands: list[ths.Instrument],
              keywords: list[str], query: str,
              top_k: int = 8) -> tuple[list[Hit], bool]:
    """收尾：实时刷新 + 确定性命中强制保留 + 精确命中置顶。

    为什么不能只靠模型
    ------------------
    实测 `find 农业银行`：预筛把农业银行排在候选第一位（154 分，第二名 12 分），
    但模型挑出来的是中信银行、建设银行、浙商银行 —— **唯独漏了农业银行**。
    模型把「农业银行」当成了「银行」这个泛词。

    名称等价是**确定性事实**，不该交给模型去猜。所以：
      · 整串命中的候选（粗排分 ≥100）→ 强制保留，钉在最前
      · 模型挑的 → 追加在后面，去重
      · 模型只负责「在剩下的里面挑相关的」
    """
    qn = (query or "").strip()

    # ① 确定性命中：整串出现在名称里的
    pinned: list[Hit] = []
    if qn:
        for i in cands:
            if qn in i.name:
                pinned.append(Hit(code=i.code, name=i.name, kind=i.kind,
                                  score=0.99, why=f"名称包含「{qn}」",
                                  matched=True))

    # ② 模型挑的
    seen = {h.code for h in pinned}
    merged = pinned + [h for h in hits if h.code not in seen]
    merged = merged[:max(top_k, len(pinned))]

    if not merged:
        return merged, True

    # ③ 实时刷新名称与价格
    quotes, live_ok = live_refresh(
        [ths.Instrument(code=h.code, name=h.name, kind=h.kind) for h in merged])

    for h in merged:
        q = quotes.get(h.code)
        if q is None:
            h.source = "本地表"
            continue
        h.name = q.name
        h.price = q.price
        h.source = "实时"
        h.matched = any(k in h.name for k in keywords) or (qn and qn in h.name)

    # ④ 精确命中置顶
    if qn:
        merged.sort(key=lambda h: (
            -int(h.name == qn),
            -int(qn in h.name or h.name in qn),
            -int(h.matched),
            -h.score,
        ))
    return merged, live_ok


def resolve_rename(query: str, install: str | None = None) -> list[dict]:
    """查同花顺的历史名称表，把旧名映射到现在的代码。

    同花顺在 `stockname_*_1.txt` 里保留了曾用名（`@h1` / `@h2` 标记）。
    所以「深发展A」能查到它现在是 000001 平安银行 —— 这是本地表才能做的事，
    任何在线接口都不会告诉你这个。

    纯本地、确定性，不需要模型。
    """
    root = install or ths.find_install()
    if not root:
        return []
    sn = os.path.join(root, "stockname")
    current = ths.tradable_universe(root)
    q = query.strip()
    if not q:
        return []

    out: list[dict] = []
    for fname, market in (("stockname_16_1.txt", "sh"), ("stockname_32_1.txt", "sz")):
        path = os.path.join(sn, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="gb18030", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if "=" not in line or line.startswith(("[", "ConfigVer")):
                        continue
                    code6, _, rest = line.partition("=")
                    if q not in rest:
                        continue
                    full = f"{market}{code6}"
                    cur = current.get(full)
                    # 取第一个曾用名做展示
                    old = rest.split("|")[0].split("@")[0].strip()
                    out.append({
                        "code": full,
                        "old_name": old,
                        "current_name": cur.name if cur else "",
                        "kind": cur.kind if cur else "",
                        "found_in": fname,
                    })
        except OSError:
            continue
    return out
