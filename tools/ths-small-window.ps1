# 把同花顺启动成一个小窗，常驻但不打扰。
#
# 为什么需要这个
# --------------
# `run.py look` 要读同花顺的界面，所以它得一直开着。
# 但全屏开着会占地方、抢焦点。这个脚本让它停在屏幕角落的一个小窗里，
# 之后用 PrintWindow 截图，不弹窗、不抢焦点。
#
# 用法
# ----
#   powershell -File tools\ths-small-window.ps1
#   powershell -File tools\ths-small-window.ps1 -Width 480 -Height 640
#   powershell -File tools\ths-small-window.ps1 -Corner bottomright
#
# 已经开着的同花顺会被重新摆到角落，不会重开一个。
param(
    [int]$Width = 460,
    [int]$Height = 620,
    [ValidateSet('topleft', 'topright', 'bottomleft', 'bottomright')]
    [string]$Corner = 'bottomright',
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W32Win {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

[W32Win]::SetProcessDPIAware() | Out-Null

$exe = 'C:\同花顺软件\同花顺\hexinlauncher.exe'
$workdir = 'C:\同花顺软件\同花顺'
if (-not (Test-Path $exe)) { Write-Error "找不到同花顺: $exe"; exit 1 }

function Get-ThsWindow {
    Get-Process | Where-Object {
        $_.ProcessName -like 'hexin*' -and $_.MainWindowHandle -ne 0
    } | Sort-Object WorkingSet64 -Descending | Select-Object -First 1
}

$p = Get-ThsWindow
if (-not $p -and -not $NoStart) {
    Write-Host "同花顺没在跑，启动中…"
    Start-Process -FilePath $exe -WorkingDirectory $workdir | Out-Null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        $p = Get-ThsWindow
        if ($p) { break }
    }
}

if (-not $p) { Write-Error "同花顺没有起来，或者主窗口还没创建"; exit 2 }

# 等它真正加载完。刚启动时主窗口是「登录到全部行情主站」，
# 那时候摆尺寸没用 —— 登录完成后窗口会自己变回全屏。
# 实测踩过：脚本跑了、尺寸也设了，几秒后又被改回全屏。
for ($i = 0; $i -lt 40; $i++) {
    $p = Get-ThsWindow
    if (-not $p) { break }
    $t = $p.MainWindowTitle
    if ($t -and $t -notmatch '登录|连接|正在|加载|启动') { break }
    Start-Sleep -Milliseconds 500
}

$p = Get-ThsWindow
if (-not $p) { Write-Error "同花顺窗口消失了"; exit 3 }
$h = $p.MainWindowHandle

# 最小化状态不能直接摆位置，先还原。这一步会短暂弹窗，但只在首次设置时发生。
if ([W32Win]::IsIconic($h)) { [W32Win]::ShowWindow($h, 9) | Out-Null; Start-Sleep -Milliseconds 400 }

$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$margin = 12
switch ($Corner) {
    'topleft'     { $x = $b.X + $margin;                    $y = $b.Y + $margin }
    'topright'    { $x = $b.X + $b.Width - $Width - $margin; $y = $b.Y + $margin }
    'bottomleft'  { $x = $b.X + $margin;                    $y = $b.Y + $b.Height - $Height - $margin }
    default       { $x = $b.X + $b.Width - $Width - $margin; $y = $b.Y + $b.Height - $Height - $margin }
}

# SWP_NOACTIVATE(0x10) | SWP_NOZORDER(0x04) —— 不激活、不改层级，避免抢焦点
$flags = 0x10 -bor 0x04

# 摆三次，每次间隔 1 秒。同花顺加载完可能还会自己重排一次布局，
# 只摆一次会被它改回去（实测）。
for ($i = 0; $i -lt 3; $i++) {
    [W32Win]::SetWindowPos($h, [IntPtr]::Zero, $x, $y, $Width, $Height, $flags) | Out-Null
    Start-Sleep -Milliseconds 1000
}

$r = New-Object W32Win+RECT
[W32Win]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.Right - $r.Left
$hh = $r.Bottom - $r.Top

Write-Host ("同花顺已摆到 {0}：{1}x{2} @ ({3},{4})  进程 {5} (PID {6})" -f `
    $Corner, $w, $hh, $x, $y, $p.ProcessName, $p.Id)
Write-Host ("窗口标题: {0}" -f $p.MainWindowTitle)
Write-Host ""
Write-Host "现在可以随时读它（不会弹窗、不会抢焦点）："
Write-Host "  python run.py look"
