# Resumable launcher: run every instance in runs20/manifest.json that has no valid result yet,
# with up to -MaxPar concurrent python processes (rolling queue). Safe to relaunch after a
# host restart: finished instances are skipped.
# Usage: powershell -ExecutionPolicy Bypass -File .\launch_parallel.ps1 [-MaxPar 8]

param([int]$MaxPar = 3, [int]$MaxMemGB = 10, [string]$ConfigPath = "")

$ErrorActionPreference = "Stop"

$p    = "C:\Users\roman\Desktop\nl2opt_review\nl2opt-main"
$py   = "C:\Users\roman\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$ds   = "D:\Downloads\MIPLIB-NL\dataset"
$out  = Join-Path $p "runs20"
$logs = Join-Path $out "logs"

$env:PYTHONPATH       = "$p\src;C:\Users\roman\Desktop\nl2opt_review\pylibs"
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
Remove-Item Env:DEEPSEEK_MODEL -ErrorAction SilentlyContinue
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"

New-Item -ItemType Directory -Force -Path $logs | Out-Null
Set-Location $p

function Test-ResultOk([string]$id) {
    $f = Join-Path $out "$id.json"
    if (-not (Test-Path $f)) { return $false }
    try { return (@(Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json).Count -ge 1) } catch { return $false }
}

$manifest = Get-Content (Join-Path $out "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$all = @()
foreach ($b in $manifest) { $all += $b.instances }

$queueList = New-Object System.Collections.ArrayList
foreach ($id in $all) { if (-not (Test-ResultOk $id)) { [void]$queueList.Add($id) } }

Write-Host ("todo: {0} / {1}   maxpar={2}" -f $queueList.Count, $all.Count, $MaxPar)
if ($queueList.Count -eq 0) { Write-Host "ALL DONE"; exit 0 }

$running = @{}
while ($queueList.Count -gt 0 -or $running.Count -gt 0) {
    while ($running.Count -lt $MaxPar -and $queueList.Count -gt 0) {
        $id = $queueList[0]
        $queueList.RemoveAt(0)
        if (Test-ResultOk $id) { continue }
        $cfgArgs = @()
        if ($ConfigPath) { $cfgArgs = @("--config", (Join-Path $p $ConfigPath)) }
        $proc = Start-Process -FilePath $py `
            -ArgumentList (@("-m", "nl2opt") + $cfgArgs + @("eval", "--data", (Join-Path $ds $id), "--use-llm", "--out", (Join-Path $out "$id.json"))) `
            -NoNewWindow -PassThru `
            -RedirectStandardOutput (Join-Path $logs "$id.log") `
            -RedirectStandardError  (Join-Path $logs "$id.err.log")
        $running[$proc.Id] = $id
        Write-Host ("[{0}] START {1}" -f (Get-Date -Format "HH:mm:ss"), $id)
    }
    Start-Sleep -Seconds 5
    foreach ($procId in @($running.Keys)) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if (-not $proc) {
            $id = $running[$procId]
            $running.Remove($procId)
            Write-Host ("[{0}] EXIT {1}" -f (Get-Date -Format "HH:mm:ss"), $id)
            continue
        }
        $virtGB = [math]::Round($proc.VirtualMemorySize64 / 1GB, 1)
        if ($virtGB -gt $MaxMemGB) {
            $id = $running[$procId]
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            $running.Remove($procId)
            Write-Host ("[{0}] KILL {1} (virtual {2} GB > {3} GB; protect host memory)" -f (Get-Date -Format "HH:mm:ss"), $id, $virtGB, $MaxMemGB) -ForegroundColor Red
        }
    }
}

$done = @($all | Where-Object { Test-ResultOk $_ }).Count
Write-Host ("FINISHED: {0}/{1} results present" -f $done, $all.Count)