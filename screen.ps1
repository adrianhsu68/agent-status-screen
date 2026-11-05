# agent-status-screen: start the server and show the page full screen (kiosk) on a small second screen
#
#   screen.cmd -Watch   keep watching (put a shortcut to this in shell:startup): starts the server, and when a
#                       small second screen appears (e.g. Win+K to a wireless display, mode "Extend") opens the
#                       kiosk on it; closes the kiosk when that screen goes away
#   screen.cmd          (double-click) start watching if not running; otherwise reopen the kiosk (= refresh)
#   screen.cmd -Stop    stop everything (watcher, kiosk, server)
#
# "Small second screen" = a non-primary monitor no wider than -MaxWidth pixels (smallest area wins), so a normal
# external monitor is left alone. In watch mode Alt+F4 on the kiosk only reopens it after 3 s; use -Stop.
# The browser uses its own profile folder (%LOCALAPPDATA%\AgentStatusScreen\browser), separate from your
# normal Chrome / Edge. Server output: %LOCALAPPDATA%\AgentStatusScreen\server.log
# Windows PowerShell 5.1 reads files without a BOM as the system code page: keep this file UTF-8 with BOM.

param([int]$Port = 8765, [int]$MaxWidth = 1366, [switch]$Stop, [switch]$Watch, [string]$Python = '')

$Root    = $PSScriptRoot
$Server  = Join-Path $Root 'server.py'
$DataDir = Join-Path $env:LOCALAPPDATA 'AgentStatusScreen'
$BrowserDir = Join-Path $DataDir 'browser'
$Log     = Join-Path $DataDir 'server.log'
$Url     = "http://127.0.0.1:$Port/"
New-Item -ItemType Directory -Force $DataDir | Out-Null

$Browser = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

# Enumerate monitors every time (WinForms Screen.AllScreens caches and does not see a display being added)
Add-Type -TypeDefinition @'
using System; using System.Collections.Generic; using System.Runtime.InteropServices;
public static class Monitors {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [StructLayout(LayoutKind.Sequential)] struct INFO { public int Size; public RECT Mon, Work; public int Flags; }
    delegate bool Proc(IntPtr h, IntPtr dc, ref RECT r, IntPtr d);
    [DllImport("user32.dll")] static extern bool EnumDisplayMonitors(IntPtr dc, IntPtr clip, Proc p, IntPtr d);
    [DllImport("user32.dll")] static extern bool GetMonitorInfo(IntPtr h, ref INFO i);
    // [X, Y, W, H] of every non-primary monitor
    public static List<int[]> Secondary() {
        var list = new List<int[]>();
        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, (IntPtr h, IntPtr dc, ref RECT r, IntPtr d) => {
            var i = new INFO(); i.Size = Marshal.SizeOf(typeof(INFO));
            if (GetMonitorInfo(h, ref i) && (i.Flags & 1) == 0)
                list.Add(new[] { i.Mon.L, i.Mon.T, i.Mon.R - i.Mon.L, i.Mon.B - i.Mon.T });
            return true;
        }, IntPtr.Zero);
        return list;
    }
}
'@

function Find-Screen {
    [Monitors]::Secondary() | Where-Object { $_[2] -le $MaxWidth } |
        Sort-Object { $_[2] * $_[3] } | Select-Object -First 1
}
function Get-Kiosk {
    Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($BrowserDir) }
}
function Get-Watcher {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Contains($PSCommandPath) -and $_.CommandLine -like '*-Watch*' }
}
function Get-Server {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='py.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($Server) }
}
function Test-Server {
    try { (Invoke-WebRequest "${Url}status" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { $false }
}
function Stop-Kiosk { Get-Kiosk | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }

function Start-Server {
    if (Get-Server) { return }
    # "python" may be the Microsoft Store stub; prefer the py launcher
    $exe = $Python
    $pre = @()
    if (-not $exe) {
        if (Get-Command py.exe -ErrorAction SilentlyContinue) { $exe = 'py.exe'; $pre = @('-3') }
        else { $exe = 'python.exe' }
    }
    Start-Process $exe -WindowStyle Hidden -WorkingDirectory $Root -RedirectStandardOutput $Log `
        -RedirectStandardError "$Log.err" -ArgumentList ($pre + @('-u', "`"$Server`""))
}
function Start-Watch {
    if (-not (Get-Watcher)) {
        $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Watch',
                  '-Port', $Port, '-MaxWidth', $MaxWidth)
        if ($Python) { $argv += @('-Python', "`"$Python`"") }
        Start-Process powershell.exe -WindowStyle Hidden -ArgumentList $argv
    }
}
function Open-Kiosk($s) {
    Stop-Kiosk
    Start-Sleep -Milliseconds 500
    # Open the window inside that screen; --kiosk then makes it full screen there
    $x = $s[0] + [int]($s[2] / 4); $y = $s[1] + [int]($s[3] / 4)
    Start-Process $Browser -ArgumentList @(
        "--user-data-dir=`"$BrowserDir`"", '--kiosk', "--window-position=$x,$y", '--window-size=640,360',
        '--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble',
        '--disable-features=Translate', '--noerrdialogs', '--disable-pinch', $Url)
}

if ($Stop) {
    Get-Watcher | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Stop-Kiosk
    Get-Server | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    return
}

if (-not $Browser) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show('Chrome / Edge not found.', 'agent-status-screen') | Out-Null
    return
}

if ($Watch) {
    if (Get-Watcher) { return }              # already watching
    $shownOn = ''                             # bounds of the screen the kiosk was opened on
    while ($true) {
        Start-Server                          # restart it if it was closed
        $s = Find-Screen
        $kiosk = Get-Kiosk
        # Reopen also when the target screen changed (e.g. one of two small screens went away)
        if ($s -and (-not $kiosk -or "$s" -ne $shownOn) -and (Test-Server)) { Open-Kiosk $s; $shownOn = "$s" }
        elseif (-not $s -and $kiosk) { Stop-Kiosk; $shownOn = '' }   # Windows moves the window to the main screen otherwise
        Start-Sleep -Seconds 3
    }
}

# Double-click: leave everything to the watcher (so two copies never open the kiosk at once)
if (Get-Watcher) { Stop-Kiosk } else { Start-Watch }
if (-not (Find-Screen)) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        "No small second screen yet. Connect it (Win+K, mode Extend) and the page opens there automatically.`n`nPage: $Url",
        'agent-status-screen') | Out-Null
}
