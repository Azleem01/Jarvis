# Restart Azleem so it picks up source changes.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File restart.ps1
#
# Why this exists: Python loads modules into memory once at startup, so editing
# a .py file does nothing to a running Azleem. And because Azleem holds a
# single-instance mutex, just launching another copy exits with "already
# running" and quietly leaves the stale one in charge. That combination once
# cost a full debugging round -- fixes verified on disk, tested green, and the
# assistant kept reproducing the old behaviour.
#
# This kills the running instance, waits for it to actually go, relaunches, and
# then prints the new banner so you can SEE which build is live.

[CmdletBinding()]
param(
    # Stop the running instance without starting a new one.
    [switch]$StopOnly,
    # Run with a visible console (python) instead of headless (pythonw).
    [switch]$Console,
    # Show what would be stopped, and stop nothing. This machine runs several
    # other pythonw apps, so verify the selection before trusting it.
    [switch]$DryRun,

    # --- Test seams. Never used in normal operation. -------------------------
    # A JSON array of { ProcessId, CommandLine, CreationDate } standing in for
    # the live Win32_Process query. Tests drive THIS matcher rather than a
    # re-typed approximation of it -- a transcription of the old rules into
    # bash once looked wrong when the script was in fact right, and a
    # transcription can just as easily look right when the script is wrong.
    [string]$ProcessSource,
    # The identity file to trust. Defaults to logs\azleem.pid.
    [string]$PidPath
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$mainPy = Join-Path $root "main.py"

if (-not (Test-Path $mainPy)) {
    Write-Error "main.py not found next to this script ($root)."
    exit 1
}

if (-not $PidPath) { $PidPath = Join-Path $root "logs\azleem.pid" }

# --- Find Azleem, and ONLY Azleem -------------------------------------------
# This machine runs several other pythonw apps (clip smart, my cluely). Match on
# the command line containing THIS directory's main.py, never on process name.
#
# This used to have a third rule: a bare "pythonw.exe main.py" naming no other
# directory was assumed to be ours by elimination, because Win32_Process exposes
# no working directory and Azleem's auto-start entry ran bare. The comment
# claimed the machine's other python apps "are all launched with a fully-
# qualified script path". That was false -- "my cluely" runs bare, and spawns a
# bare child -- so -DryRun on 2026-07-28 marked both of them WOULD STOP.
#
# Guessing is now gone. Azleem writes logs\azleem.pid at startup (main.py
# _claim_pid_file), so it identifies itself; anything still bare and unclaimed
# is left alone and reported, never killed.

function Get-Candidates {
    $raw = if ($ProcessSource) {
        @(Get-Content -LiteralPath $ProcessSource -Raw | ConvertFrom-Json)
    } else {
        @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'main\.py' })
    }
    foreach ($p in $raw) {
        # Unknown start time sorts earliest, so it can never make a process look
        # NEWER than the pid file and lose a legitimate claim.
        $created = [datetime]::MinValue
        if ($p.CreationDate) { $created = [datetime]$p.CreationDate }
        [pscustomobject]@{
            ProcessId    = [int]$p.ProcessId
            CommandLine  = [string]$p.CommandLine
            CreationDate = $created
        }
    }
}

function Get-Claim {
    # logs\azleem.pid: PID on line 1, the main.py it is running on line 2.
    if (-not (Test-Path -LiteralPath $PidPath)) { return $null }
    $lines = @(Get-Content -LiteralPath $PidPath -ErrorAction SilentlyContinue)
    if ($lines.Count -lt 1) { return $null }
    $claimed = 0
    if (-not [int]::TryParse($lines[0].Trim(), [ref]$claimed)) { return $null }
    # Line 2 stops a pid file written by a DIFFERENT checkout of Azleem from
    # being honoured against this one.
    if ($lines.Count -ge 2 -and $lines[1].Trim() -and $lines[1].Trim() -ne $mainPy) {
        return $null
    }
    [pscustomobject]@{
        ProcessId = $claimed
        Written   = (Get-Item -LiteralPath $PidPath).LastWriteTime
    }
}

function Get-AzleemVerdicts {
    # Anchored to a path boundary so a sibling directory such as
    # C:\Users\aleem\JARVIS_backup\main.py is not treated as ours. Built
    # segment by segment because escaping $root whole makes its separators
    # literal backslashes, and "python C:/Users/aleem/JARVIS/main.py" is a
    # perfectly ordinary way to launch us.
    $segments = ($root -split '[\\/]') | ForEach-Object { [regex]::Escape($_) }
    $escaped = ($segments -join '[\\/]') + '[\\/]'
    $claim = Get-Claim

    foreach ($p in (Get-Candidates)) {
        $stop = $false
        if ($claim -and $p.ProcessId -eq $claim.ProcessId -and
                $p.CreationDate -le $claim.Written.AddSeconds(2)) {
            # It told us who it is, and it is old enough to have written the
            # file itself. A recycled PID would post-date a stale claim, which
            # is what the timestamp check catches.
            $stop = $true
            $why = 'claimed logs\azleem.pid'
        }
        elseif ($p.CommandLine -match $escaped) {
            $stop = $true
            $why = 'command line names this directory'
        }
        elseif ($p.CommandLine -match '[\\/]main\.py') {
            $why = 'runs a main.py in another directory'
        }
        else {
            $why = 'bare "main.py", unclaimed -- cannot prove it is ours'
        }
        [pscustomobject]@{
            ProcessId   = $p.ProcessId
            CommandLine = $p.CommandLine
            Stop        = $stop
            Why         = $why
        }
    }
}

# Anything bare and unclaimed is what the old rule would have killed. Say so
# out loud, because the alternative to a false kill is a missed one: an Azleem
# that predates the pid file will not be found, and the user needs to know why.
function Write-SkipNotice {
    param($Verdicts, $Stopping)
    $bare = @($Verdicts | Where-Object { -not $_.Stop -and $_.Why -like 'bare*' })
    if ($bare.Count -eq 0) { return }
    Write-Host ""
    Write-Warning ("Left {0} unclaimed bare 'main.py' process(es) alone:" -f $bare.Count)
    foreach ($b in $bare) {
        Write-Host ("    PID {0,-7} {1}" -f $b.ProcessId, $b.CommandLine)
    }
    if ($Stopping -eq 0) {
        Write-Host "  No Azleem was found. If one of these IS Azleem, it started"
        Write-Host "  before it could claim logs\azleem.pid -- stop it by hand once,"
        Write-Host "  then use this script (it relaunches with a qualified path)."
    }
}

if ($DryRun) {
    Write-Host "Every python/pythonw process running a main.py:`n"
    $verdicts = @(Get-AzleemVerdicts)
    foreach ($v in $verdicts) {
        $label = if ($v.Stop) { "WOULD STOP" } else { "leave alone" }
        "  {0,-12} PID {1,-7} {2}" -f $label, $v.ProcessId, $v.CommandLine
        "  {0,-12}           ^-- {1}" -f "", $v.Why
    }
    Write-SkipNotice -Verdicts $verdicts -Stopping @($verdicts | Where-Object Stop).Count
    Write-Host "`nNothing was stopped (-DryRun)."
    exit 0
}

$verdicts = @(Get-AzleemVerdicts)
$running = @($verdicts | Where-Object Stop)
Write-SkipNotice -Verdicts $verdicts -Stopping $running.Count

if ($running.Count -eq 0) {
    Write-Host "No running Azleem found."
} else {
    foreach ($p in $running) {
        Write-Host ("Stopping Azleem  PID {0}  ({1})" -f $p.ProcessId, $p.Why)
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        } catch {
            Write-Warning ("Could not stop PID {0}: {1}" -f $p.ProcessId, $_)
        }
    }

    # Wait for the OS to release the process (and with it the singleton mutex).
    # Relaunching too early makes the new copy exit as a duplicate.
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-AzleemVerdicts | Where-Object Stop).Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    }
    if (@(Get-AzleemVerdicts | Where-Object Stop).Count -gt 0) {
        Write-Error "Azleem did not exit; not starting a second copy."
        exit 1
    }
    Write-Host "Stopped."
}

if ($StopOnly) { exit 0 }

# --- Relaunch ----------------------------------------------------------------
$log = Join-Path $root "logs\azleem.log"
$sizeBefore = if (Test-Path $log) { (Get-Item $log).Length } else { 0 }

$exeName = if ($Console) { "python.exe" } else { "pythonw.exe" }
$exe = (Get-Command $exeName -ErrorAction SilentlyContinue)
if (-not $exe) {
    Write-Error "$exeName is not on PATH."
    exit 1
}

Write-Host "Starting Azleem ($exeName)..."
# Launch with the fully-qualified script path, not a bare "main.py". A bare
# invocation is now left alone by the matcher rather than guessed at, so a
# qualified one is what makes the next run able to find this process even
# before it has written logs\azleem.pid.
Start-Process -FilePath $exe.Source -ArgumentList "`"$mainPy`"" -WorkingDirectory $root | Out-Null

# --- Confirm which build is actually live ------------------------------------
# The whole point of the restart is that the NEW code is running, so print the
# banner rather than assuming. Headless startup loads Whisper before it logs.
if ($Console) {
    Write-Host "Started in a console window; its banner shows the live build."
    exit 0
}

Write-Host "Waiting for the startup banner..."
$deadline = (Get-Date).AddSeconds(60)
$banner = $null
while ((Get-Date) -lt $deadline) {
    if (Test-Path $log) {
        # @() matters: with a single match, $fresh is a bare string and
        # $fresh[-1] would index its last CHARACTER instead of the last line.
        $fresh = @(Get-Content -LiteralPath $log -Tail 60 |
            Where-Object { $_ -match '^===== Azleem started' })
        # Only accept a banner written after this restart began.
        if ($fresh.Count -gt 0 -and (Get-Item $log).Length -gt $sizeBefore) {
            $banner = $fresh[-1]
            break
        }
    }
    Start-Sleep -Milliseconds 500
}

Write-Host ""
if ($banner) {
    Write-Host "Azleem is live:"
    Write-Host "  $banner"
    Write-Host ""
    Write-Host "Source on disk:"
    & $exe.Source -m build_info 2>$null | ForEach-Object { Write-Host "  $_" }
    Write-Host ""
    # Confirm the new instance claimed its PID, since the next restart's ability
    # to find it without guessing depends on that file existing.
    $claim = Get-Claim
    if ($claim) {
        Write-Host ("Identified itself: PID {0} in logs\azleem.pid" -f $claim.ProcessId)
    } else {
        Write-Warning "No logs\azleem.pid claim; the next restart will match on the path instead."
    }
    Write-Host ""
    Write-Host "The build stamps above must match. If they differ, the running"
    Write-Host "process is not the code you just edited."
} else {
    Write-Warning "No banner appeared within 60s. Check $log"
    exit 1
}
