# Unified evaluation launcher (watchdog + synthetic results).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\run_eval.ps1 `
#       -Manifest runs20\manifest.json -Out runs20_u1 [-Model deepseek-v4-pro] `
#       [-BudgetSec 900] [-MaxMemGB 12] [-Config config.yaml]
#
# Manifest format: [{"batch": 1, "instances": ["a", "b"]}, ...]
# Each batch runs sequentially in its own child PowerShell; all batches run concurrently.
# For every instance a watchdog enforces:
#   - virtual memory > MaxMemGB  -> kill, write synthetic result (status=memory_exceeded)
#   - wall clock    > BudgetSec  -> kill, write synthetic result (status=budget_exceeded)
# Existing valid results are skipped, so the launcher is resumable.
param(
    [Parameter(Mandatory = $true)][string]$Manifest,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Model = "",
    [int]$BudgetSec = 900,
    [int]$MaxMemGB = 12,
    [string]$Config = "",
    [string]$DataDir = "D:\Downloads\MIPLIB-NL\dataset"
)
$ErrorActionPreference = "Continue"

$p    = "C:\Users\roman\Desktop\nl2opt_review\nl2opt-main"
$py   = "C:\Users\roman\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$logs = Join-Path $Out "logs"

$env:PYTHONPATH       = "$p\src;C:\Users\roman\Desktop\nl2opt_review\pylibs"
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
Remove-Item Env:DEEPSEEK_MODEL -ErrorAction SilentlyContinue
if ($Model) { $env:DEEPSEEK_MODEL = $Model }
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"

Set-Location $p
New-Item -ItemType Directory -Force -Path $Out, $logs | Out-Null

function Test-ResultOk([string]$id) {
    $f = Join-Path $Out "$id.json"
    if (-not (Test-Path $f)) { return $false }
    try { return (@(Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json).Count -ge 1) } catch { return $false }
}

$cfgArgs = @()
if ($Config) { $cfgArgs = @("--config", (Join-Path $p $Config)) }

$mf = Get-Content $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
$all = @()
foreach ($b in $mf) { $all += $b.instances }
$modelLabel = "config default"
if ($Model) { $modelLabel = $Model }
Write-Host ("batches: {0}  instances: {1}  model: {2}  budget: {3}s  mem: {4}GB" -f `
    @($mf).Count, $all.Count, $modelLabel, $BudgetSec, $MaxMemGB)

$procs = @()
foreach ($batch in $mf) {
    $bs = Join-Path $Out ("batch{0}.ps1" -f $batch.batch)
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add("`$ErrorActionPreference = 'Continue'")
    $lines.Add("Set-Location '$p'")
    if ($Model) { $lines.Add("`$env:DEEPSEEK_MODEL = '$Model'") }
    $lines.Add("function Test-Ok([string]`$id) { `$f = Join-Path '$Out' (`"`$id.json`"); if (-not (Test-Path `$f)) { return `$false }; try { return (@(Get-Content `$f -Raw -Encoding UTF8 | ConvertFrom-Json).Count -ge 1) } catch { return `$false } }")
    $lines.Add("function Write-Synth([string]`$id, [string]`$status, [string]`$why) { if (Test-Ok `$id) { return }; `$o = [ordered]@{ id = `$id; model = '$Model'; protocol_version = 37; prompt_mode = 'layered'; success = `$false; suspicious = `$false; suspicious_resolved = `$false; attempt = 0; attempts = 0; attempts_log = @(); rounds = 0; status = `$status; objective_value = `$null; variables = `$null; errors = @(`$why); final_ir = `$null; trajectory = @(); token_usage = @{}; stage_usage = @{} }; `$json = '[' + (`$o | ConvertTo-Json -Depth 6) + ']'; Set-Content -Path (Join-Path '$Out' (`"`$id.json`")) -Value `$json -Encoding UTF8 }")
    $todo = 0
    foreach ($n in $batch.instances) {
        if (Test-ResultOk $n) { $lines.Add("Write-Host '[SKIP] $n already done'"); continue }
        $todo++
        $lines.Add('$t0 = Get-Date')
        $lines.Add("Write-Host ('[START] $n ' + `$t0.ToString('HH:mm:ss'))")
        $argStr = "'-m','nl2opt'"
        if ($Config) { $argStr += ",'--config','" + (Join-Path $p $Config) + "'" }
        $argStr += ",'eval','--data','" + (Join-Path $DataDir $n) + "','--use-llm','--out','" + (Join-Path $Out "$n.json") + "'"
        $lines.Add("`$pr = Start-Process -FilePath '$py' -ArgumentList @($argStr) -NoNewWindow -PassThru -RedirectStandardOutput (Join-Path '$logs' '$n.log') -RedirectStandardError (Join-Path '$logs' '$n.err.log')")
        $lines.Add('$reason = ""')
        $lines.Add('while (-not $pr.HasExited) { $el = ((Get-Date) - $t0).TotalSeconds; $qm = Get-Process -Id $pr.Id -ErrorAction SilentlyContinue; if ($qm) { $v = [math]::Round($qm.VirtualMemorySize64/1GB, 1); if ($v -gt ' + $MaxMemGB + ') { $reason = "memory"; Stop-Process -Id $pr.Id -Force; break } }; if ($el -gt ' + $BudgetSec + ') { $reason = "budget"; Stop-Process -Id $pr.Id -Force; break }; Start-Sleep -Seconds 5 }')
        $lines.Add('$pr.WaitForExit()')
        $lines.Add('if ($reason -eq "memory") { Write-Synth "' + $n + '" "memory_exceeded" "killed by memory guard (>' + $MaxMemGB + ' GB virtual), no result" } elseif ($reason -eq "budget") { Write-Synth "' + $n + '" "budget_exceeded" "killed by time budget (>' + $BudgetSec + ' s), no result" }')
        $lines.Add("Write-Host ('[END] $n exit=' + `$pr.ExitCode + ' reason=' + `$reason + ' seconds=' + [int]((Get-Date) - `$t0).TotalSeconds)")
    }
    if ($todo -eq 0) { Write-Host ("batch {0}: all done, skip" -f $batch.batch); continue }
    Set-Content -Path $bs -Value ($lines -join "`r`n") -Encoding ASCII
    $log = Join-Path $Out ("batch{0}.log" -f $batch.batch)
    $proc = Start-Process -FilePath "powershell" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $bs) `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput $log `
        -RedirectStandardError (Join-Path $Out ("batch{0}.err.log" -f $batch.batch))
    $procs += $proc.Id
    Write-Host ("[{0}] launched batch {1} (pid {2}, {3} todo)" -f (Get-Date -Format "HH:mm:ss"), $batch.batch, $proc.Id, $todo)
}

if ($procs.Count -eq 0) { Write-Host "nothing to do"; exit 0 }

while ($true) {
    $alive = @($procs | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }).Count
    $done = @($all | Where-Object { Test-ResultOk $_ }).Count
    Write-Host ("[{0}] alive {1}/{2}   results {3}/{4}" -f (Get-Date -Format "HH:mm:ss"), $alive, $procs.Count, $done, $all.Count)
    if ($alive -eq 0) { break }
    Start-Sleep -Seconds 20
}
Write-Host "ALL BATCHES DONE"
$done = @($all | Where-Object { Test-ResultOk $_ }).Count
Write-Host ("FINISHED: {0}/{1}" -f $done, $all.Count)
