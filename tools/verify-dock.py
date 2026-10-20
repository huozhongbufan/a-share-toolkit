"""验证「贴着同花顺」这个核心承诺。

步骤：
  1. 读面板和同花顺的初始位置，确认相邻
  2. 把同花顺移到别处
  3. 等面板跟随，再读一次，确认它跟着走了
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

sys.path.insert(0, ".")

from ec import dock

U = dock.USER32
_proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def find_by_title(sub: str) -> tuple[int, tuple | None]:
    hits = []

    def cb(h, _):
        n = U.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            U.GetWindowTextW(h, b, n + 1)
            if sub in b.value:
                hits.append((h, b.value))
        return True

    U.EnumWindows(_proto(cb), 0)
    if not hits:
        return 0, None
    h, _t = hits[0]
    return h, dock.rect_of(h)


def move(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    # SWP_NOACTIVATE | SWP_NOZORDER
    U.SetWindowPos(hwnd, 0, x, y, w, h, 0x10 | 0x04)


def show(tag: str, ph, pr, th, tr) -> None:
    print(f"  {tag}")
    print(f"    同花顺  hwnd={th}  rect={tr}")
    print(f"    面板    hwnd={ph}  rect={pr}")
    if pr and tr:
        gap = tr[0] - (pr[0] + pr[2])
        print(f"    间隙 = 同花顺左 {tr[0]} − 面板右 {pr[0]+pr[2]} = {gap}px")


def main() -> int:
    ph, pr = find_by_title("etf-copilot")
    th, tr = find_by_title("同花顺")
    if not ph:
        print("✗ 面板没找到，先启动 python run.py dock")
        return 1
    if not th:
        print("✗ 同花顺没找到")
        return 2

    print("=== 1. 初始位置 ===")
    show("", ph, pr, th, tr)

    sx = tr[0]
    sy = tr[1]
    sw = tr[2]
    sh = tr[3]

    # 2. 把同花顺往右挪 200px（保持尺寸）
    newx = min(sx + 200, 1707 - sw - 20)
    print()
    print(f"=== 2. 把同花顺从 x={sx} 移到 x={newx} ===")
    move(th, newx, sy, sw, sh)
    time.sleep(0.5)
    _th2, tr2 = find_by_title("同花顺")
    print(f"    同花顺现在 rect={tr2}")

    # 3. 等面板跟随（它每 1.2 秒轮询一次）
    print()
    print("=== 3. 等 3 秒让面板跟随 ===")
    time.sleep(3)
    _ph3, pr3 = find_by_title("etf-copilot")
    show("跟随之后：", _ph3, pr3, _th2, tr2)

    # 4. 判定
    print()
    print("=== 判定 ===")
    if pr3 and tr2:
        gap = tr2[0] - (pr3[0] + pr3[2])
        moved = pr3[0] != (pr[0] if pr else None)
        print(f"  面板 x: {pr[0] if pr else '?'} → {pr3[0]}  跟随了={moved}")
        print(f"  间隙 {gap}px（期望 8px 左右）")
        if moved and 4 <= gap <= 14:
            print("  ✓ 通过：面板确实贴着同花顺，并且跟着移动了")
            rc = 0
        else:
            print("  ✗ 不通过")
            rc = 3
    else:
        print("  ✗ 读不到位置，无法判定")
        rc = 4

    # 还原同花顺位置，别把用户窗口留在奇怪的地方
    move(th, sx, sy, sw, sh)
    print()
    print("（已把同花顺还原到原位）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
