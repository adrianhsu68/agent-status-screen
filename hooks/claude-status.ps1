# Claude Code hook: send the current state and a short activity label to agent-status-screen (UDP).
# Usage (from settings.json hooks): powershell -NoProfile -ExecutionPolicy Bypass -File claude-status.ps1 [RUN|WAIT|DONE|OFF]
#   The hook JSON on stdin decides the state by hook_event_name; the argument is only a fallback.
# Packet: `CLAUDE:STATE` or `CLAUDE:STATE|label`. Only the tool name + file name / first command word is sent,
#   never your prompt, full commands, file contents or tool output.
# UDP does not wait for a reply: when the server is not running the packet is simply dropped.
# Target: AGENT_STATUS_HOST / AGENT_STATUS_PORT environment variables, default 127.0.0.1:57999.
# Debug: set AGENT_STATUS_DRYRUN=1 to print the packet instead of sending it.
param([string]$State = 'RUN')

$TargetHost = if ($env:AGENT_STATUS_HOST) { $env:AGENT_STATUS_HOST } else { '127.0.0.1' }
$TargetPort = if ($env:AGENT_STATUS_PORT) { [int]$env:AGENT_STATUS_PORT } else { 57999 }

function Short([string]$s, [int]$max = 40) {
    if (-not $s) { return '' }
    $s = ($s -replace '[^\x20-\x7E]', '?').Trim()        # ASCII only
    if ($s.Length -gt $max) { $s = $s.Substring(0, $max) }
    return $s
}

# Internal tools that mean nothing on the screen (desktop app task list, wrap-up, chapters): keep the last label
$QuietTools = 'update_plan|wrap_up|mark_chapter|ToolSearch'

function Leaf([string]$p) {
    if (-not $p) { return '' }
    return ($p.TrimEnd('\', '/') -split '[\\/]')[-1]
}

# Split on ; && || and newlines that are outside quotes
function Split-Commands([string]$cmd) {
    $parts = New-Object System.Collections.Generic.List[string]
    $buf = New-Object System.Text.StringBuilder
    $quote = [char]0
    for ($i = 0; $i -lt $cmd.Length; $i++) {
        $c = $cmd[$i]
        if ($quote -ne [char]0) {
            [void]$buf.Append($c)
            if ($c -eq $quote) { $quote = [char]0 }
        } elseif ($c -eq [char]0x22 -or $c -eq [char]0x27) {
            $quote = $c; [void]$buf.Append($c)
        } elseif ($c -eq ';' -or $c -eq "`r" -or $c -eq "`n") {
            $parts.Add($buf.ToString()); [void]$buf.Clear()
        } elseif ($i + 1 -lt $cmd.Length -and ($cmd.Substring($i, 2) -eq '&&' -or $cmd.Substring($i, 2) -eq '||')) {
            $parts.Add($buf.ToString()); [void]$buf.Clear(); $i++
        } else {
            [void]$buf.Append($c)
        }
    }
    $parts.Add($buf.ToString())
    return $parts
}

# First "real" command: skip `cd ...`, `X=...` and `$x = ...` / `$env:X = ...`, but only when that segment is
# simple (one plain or fully quoted value, no escapes, substitutions or a value ending in a backslash);
# otherwise the segment boundaries cannot be trusted (e.g. TOKEN="a\";secret") and '' is returned.
# The name is returned only when it is a plain file-name-like word, so only the tool name is shown otherwise.
$SkipStart = '^\s*(?:(?:cd|pushd|set-location)(?:\s|$)|\$?(?:\w+:)?\w+\s*=)'
$SimpleSkip = @'
^\s*(?:(?:cd|pushd|set-location)\s*|\$?(?:\w+:)?\w+\s*=\s*)(?:"(?:[^"`$]*[^"`$\\])?"|'[^']*'|(?:[^\s"'`$();&|]*[^\s"'`$();&|\\])?)\s*$
'@.Trim()
function MainCommand([string]$cmd) {
    foreach ($seg in (Split-Commands $cmd)) {
        if (-not $seg.Trim()) { continue }
        if ($seg -match $SkipStart) {
            if ($seg -match $SimpleSkip) { continue }
            return ''
        }
        if ($seg -notmatch '^\s*&?\s*(\x22([^\x22]*)\x22|\x27([^\x27]*)\x27|(\S+))') { return '' }   # quoted paths may contain spaces
        $name = Leaf ($Matches[2] + $Matches[3] + $Matches[4])
        if ($name -cmatch '^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$') { return $name }
        return ''
    }
    return ''
}

function ToolSummary($tool, $in) {
    switch -Regex ($tool) {
        '^(Bash|PowerShell)$' { return ("$tool " + (MainCommand $in.command)).Trim() }
        '^(Edit|Write|Read|NotebookEdit)$' { return "$tool " + (Leaf $in.file_path) }
        '^Grep$' { return ('search ' + (Leaf $in.path)).Trim() }     # not the pattern
        '^Glob$' { return 'find ' + (Leaf $in.pattern) }
        '^mcp__' { return ($tool -split '__')[-1] }
        '^(Agent|Task)$' { return 'Agent' }                          # not the description (free text)
        default { return $tool }
    }
}

$msg = $State
try {
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    $raw = [Console]::In.ReadToEnd()
    if ($raw) {
        $j = $raw | ConvertFrom-Json
        switch ($j.hook_event_name) {
            'UserPromptSubmit' { $msg = 'RUN|thinking...' }
            'PreToolUse' {
                if ($j.tool_name -eq 'AskUserQuestion') { $msg = 'WAIT|question' }
                elseif ($j.tool_name -match "(^|__)($QuietTools)$") { $msg = 'RUN' }
                else { $msg = 'RUN|' + (Short (ToolSummary $j.tool_name $j.tool_input)) }
            }
            'PostToolUse' { $msg = 'RUN' }
            'Notification' {
                if ($j.message -match 'permission to use (\S+)') { $msg = 'WAIT|approve ' + (Short $Matches[1]) }
                else { $msg = 'WAIT|needs you' }
            }
            'Stop' { $msg = 'DONE' }
            'SessionEnd' { $msg = 'OFF|' }
        }
    }
} catch {
    if ($env:AGENT_STATUS_DRYRUN) { Write-Output "error: $($_.Exception.Message)" }
}

$msg = 'CLAUDE:' + $msg
if ($env:AGENT_STATUS_DRYRUN) { Write-Output $msg; exit 0 }

try {
    $udp = New-Object System.Net.Sockets.UdpClient
    $bytes = [System.Text.Encoding]::ASCII.GetBytes($msg)
    [void]$udp.Send($bytes, $bytes.Length, $TargetHost, $TargetPort)
    $udp.Close()
} catch {
}
exit 0
