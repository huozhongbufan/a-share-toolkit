param([string]$Target, [string]$OutPath, [string]$ProcName)
# 截图用的 PowerShell 脚本。由 ec/screen.py 写进临时目录后调用，不单独运行。
#
# param() 必须放在最前面（注释之前也不行，BOM 也不能重复）——
# 实测过：文件开头多一个 BOM，param 块就不被识别，$OutPath 变成 null，
# 最后在 $bmp.Save() 处报 ArgumentNullException。
#
# 为什么用 PrintWindow 而不是 CopyFromScreen
# ------------------------------------------
# 旧版是 CopyFromScreen + ShowWindow(SW_RESTORE) + SetForegroundWindow：
# 每次截图都会**把同花顺弹出来并抢走焦点**，打断用户正在做的事。
# 这与「让同花顺当个小窗挂着、随时能读」的用法直接冲突。
#
# PrintWindow 由窗口自己把内容画到内存 DC，特点：
#   · 不需要窗口在最前面（被别的窗口挡住也行）
#   · 不移动、不还原、不抢焦点
#   · 唯一例外：**最小化**的窗口通常返回空白 —— 这种情况明确报错，
#     让用户把它保持成小窗而不是最小化。
$ErrorActionPreference = 'Stop'
# 强制 stdout 用 UTF-8。实测：当父进程以隐藏窗口启动（如 Web 服务），
# 子 PowerShell 的码页是 GBK，ConvertTo-Json 输出的中文标题到 Python 手里
# 按 UTF-8 解码就成乱码（「同花顺」→「ͬ��˳」）。在终端里跑没事、服务里跑就烂，
# 正是因为码页不同。这里显式钉死，不再依赖父环境。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W32Cap {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

# 不要调 SetProcessDPIAware。
# 实测：调了之后 GetWindowRect 返回物理像素（2582x1449），
# 而 PrintWindow 只把内容画在左上角一块，右边和下面全是白边。
# 不调的话坐标是逻辑像素（1721x966），和 PrintWindow 输出尺寸一致，正好贴合。
$title = ''

if ($Target -eq 'window') {
    $p = Get-Process | Where-Object { $_.ProcessName -like "$ProcName*" -and $_.MainWindowHandle -ne 0 } |
         Sort-Object WorkingSet64 -Descending | Select-Object -First 1
    if (-not $p) { Write-Output '{"error":"process_not_running"}'; exit 2 }
    $h = $p.MainWindowHandle
    $title = $p.MainWindowTitle

    # 最小化时 PrintWindow 会返回空白。明确报错，不要保存一张黑图冒充成功。
    if ([W32Cap]::IsIconic($h)) {
        Write-Output '{"error":"window_minimized"}'
        exit 4
    }

    $r = New-Object W32Cap+RECT
    [W32Cap]::GetWindowRect($h, [ref]$r) | Out-Null
    $w = $r.Right - $r.Left
    $hh = $r.Bottom - $r.Top
} else {
    $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $w = $b.Width
    $hh = $b.Height
}

if ($w -le 0 -or $hh -le 0) { Write-Output '{"error":"bad_rect"}'; exit 3 }

$bmp = New-Object System.Drawing.Bitmap $w, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)

if ($Target -eq 'window') {
    $hdc = $g.GetHdc()
    # 2 = PW_RENDERFULLCONTENT，Chromium/Electron 类窗口必须带上，否则是空白
    $ok = [W32Cap]::PrintWindow($h, $hdc, 2)
    $g.ReleaseHdc($hdc)
    if (-not $ok) {
        # 少数老程序不支持 flags=2，退回 flags=0 再试一次
        $hdc = $g.GetHdc()
        $ok = [W32Cap]::PrintWindow($h, $hdc, 0)
        $g.ReleaseHdc($hdc)
    }
    if (-not $ok) { $g.Dispose(); $bmp.Dispose(); Write-Output '{"error":"printwindow_failed"}'; exit 5 }

    # 全黑说明内容没画上来（某些硬件加速窗口会这样），报错而不是给张空图
    $c1 = $bmp.GetPixel([int]($w/2), [int]($hh/2))
    $c2 = $bmp.GetPixel(5, 5)
    if ($c1.R -eq $c2.R -and $c1.G -eq $c2.G -and $c1.B -eq $c2.B -and $c1.R -lt 8) {
        $g.Dispose(); $bmp.Dispose(); Write-Output '{"error":"blank_capture"}'; exit 6
    }
} else {
    $g.CopyFromScreen(0, 0, 0, 0, (New-Object System.Drawing.Size $w, $hh))
}

$bmp.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()

$o = [ordered]@{ width = $w; height = $hh; title = $title; method = 'printwindow' }
$o | ConvertTo-Json -Compress
