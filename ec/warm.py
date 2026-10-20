"""把本地模型钉在内存里，别让它闲着就卸载。

问题
----
ollama 默认 `keep_alive=5m` —— 五分钟没人用就卸载，下次调用要重新加载。
实测加载一次 qwen2.5:7b 要 **4.8 秒**，这 4.8 秒就是「不实时在线」的代价。

解决
----
发一个极小的请求，带上 `keep_alive: -1`。实测：
  · `expires_at` 变成 2319 年（等于永不卸载）
  · 之后**不带 keep_alive 的普通请求不会把它改回 5 分钟**（测过）

所以跑一次 `warm` 就够，正常代码路径不用改。

内存是硬约束
------------
这台机器 15.7 GB 总内存，可用只有 4.8 GB。五个模型加起来 19.9 GB ——
**装不下全部**。所以这里不做「全量常驻」，而是：

  1. 先查可用内存
  2. 只钉能放下的
  3. 放不下就明确拒绝，并告诉你还差多少

宁可拒绝，也不要让它去 swap —— 换页之后比卸载重载还慢。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import time
import urllib.error
import urllib.request

OLLAMA = "http://127.0.0.1:11434"

# 常用的三档。同一时间只能钉一组（内存不够两档同时在线）。
PRESETS: dict[str, list[str]] = {
    "chat": ["qwen2.5:7b"],       # ask：工具调用 + find 的降级备选
    "vision": ["qwen3.5:4b"],     # look：唯一有视觉的
    "find": ["deepseek-r1:8b"],   # find：检索最准，但 51 秒，常驻意义不大
}

SIZE_GB = {
    "qwen2.5:7b": 4.76,
    "qwen3.5:4b": 3.3,
    "deepseek-r1:8b": 5.2,
    "glm4:9b": 5.5,
    "bge-m3:latest": 1.2,
}


class MemStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD),
        ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def memory() -> dict:
    """物理内存（GB）。用 kernel32，不引入 psutil。"""
    st = MemStatus()
    st.dwLength = ctypes.sizeof(MemStatus)
    ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    if not ok:
        return {}
    gb = 1024 ** 3
    return {
        "total_gb": round(st.ullTotalPhys / gb, 1),
        "avail_gb": round(st.ullAvailPhys / gb, 1),
        "percent": int(st.dwMemoryLoad),
    }


def loaded() -> list[dict]:
    """当前加载在内存里的模型。"""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=10) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return []
    out = []
    for m in d.get("models") or []:
        out.append({
            "name": m.get("name"),
            "size_gb": round((m.get("size") or 0) / 1e9, 2),
            "expires_at": (m.get("expires_at") or "")[:19],
            "pinned": str(m.get("expires_at") or "").startswith("2319") or
                      str(m.get("expires_at") or "").startswith("9999"),
        })
    return out


def _gen(model: str, keep_alive, timeout: int = 300) -> tuple[bool, float, str]:
    """发一个最小请求。返回 (成功, 耗时, 错误)。"""
    body = {
        "model": model,
        "prompt": "ok",
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"num_predict": 1},
    }
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True, time.time() - t0, ""
    except urllib.error.HTTPError as exc:
        return False, time.time() - t0, f"HTTP {exc.code}"
    except Exception as exc:
        return False, time.time() - t0, str(exc)[:80]


def pin(models: list[str], verbose=None) -> dict:
    """把模型钉进内存。内存不够时先释放不要的，还不够就拒绝。"""
    say = verbose or (lambda *_: None)
    mem = memory()
    if not mem:
        return {"ok": False, "error": "读不到内存信息"}

    already = {m["name"] for m in loaded()}
    need = sum(SIZE_GB.get(m, 5.0) for m in models if m not in already)
    avail = mem["avail_gb"]

    say(f"  可用内存 {avail} GB / 总 {mem['total_gb']} GB，需要 {need:.1f} GB")

    # 内存不够时，先把「不是这次要钉的」模型释放掉。
    # 用户不该被要求先手动 --unload 一次 —— 那是把实现细节推给他。
    if need > avail:
        evict = [m["name"] for m in loaded() if m["name"] not in models]
        if evict:
            say(f"  内存不够，先释放不用的：{', '.join(evict)}")
            unpin(evict, verbose=verbose)
            time.sleep(0.5)
            mem = memory()
            avail = mem["avail_gb"]
            say(f"  释放后可用 {avail} GB")

    if need > avail:
        return {
            "ok": False,
            "error": (f"内存不够：需要 {need:.1f} GB，可用只有 {avail} GB，"
                      f"差 {need - avail:.1f} GB。"
                      f"关掉抖音/ChatGPT/mpv/Lively 大约能省 2.5 GB，"
                      f"或者用 --set 选一个小一点的档位。"),
            "need_gb": round(need, 1),
            "avail_gb": avail,
        }

    results = []
    for m in models:
        if m in already:
            say(f"  {m} 已经在内存里，跳过")
            results.append({"model": m, "ok": True, "elapsed": 0.0, "note": "已在内存"})
            continue
        say(f"  加载 {m} …")
        ok, el, err = _gen(m, -1)
        results.append({"model": m, "ok": ok, "elapsed": round(el, 1), "error": err})
        say(f"    {'✓' if ok else '✗'} {el:.1f}s {err}")
    return {"ok": all(r["ok"] for r in results), "results": results, "mem": memory()}


def unpin(models: list[str] | None = None, verbose=None) -> dict:
    """释放。keep_alive=0 让 ollama 立刻卸载。"""
    say = verbose or (lambda *_: None)
    targets = models or [m["name"] for m in loaded()]
    if not targets:
        return {"ok": True, "released": [], "note": "本来就没有加载的模型"}
    released = []
    for m in targets:
        ok, _el, err = _gen(m, 0, timeout=60)
        say(f"  {m}: {'已释放' if ok else err}")
        released.append({"model": m, "ok": ok, "error": err})
    return {"ok": all(r["ok"] for r in released), "released": released,
            "mem": memory()}
