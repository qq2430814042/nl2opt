# Randomly sample 20 MIPLIB-NL instances, run them in 5 parallel batches (4 per batch).
# Resumable: reuses runs20/manifest.json if present and skips instances that already have
# a valid result JSON, so an interrupted run can simply be relaunched.
# Usage (from repo root): powershell -ExecutionPolicy Bypass -File .\run20.ps1

$ErrorActionPreference = "Stop"

$p   = "C:\Users\roman\Desktop\nl2opt_review\nl2opt-main"
$py  = "C:\Users\roman\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$ds  = "D:\Downloads\MIPLIB-NL\dataset"
$out = Join-Path $p "runs20"

$env:PYTHONPATH       = "$p\src;C:\Users\roman\Desktop\nl2opt_review\pylibs"
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
Remove-Item Env:DEEPSEEK_MODEL -ErrorAction SilentlyContinue
$env:PYTHONUTF8 = "1"

Set-Location $p
New-Item -ItemType Directory -Force -Path $out | Out-Null

$manifestPath = Join-Path $out "manifest.json"

function Test-ResultOk([string]$id) {
    $f = Join-Path $out "$id.json"
    if (-not (Test-Path $f)) { return $false }
    try {
        $raw = Get-Content $f -Raw -Encoding UTF8
        $d = $raw | ConvertFrom-Json
        return ($null -ne $d -and @($d).Count -ge 1)
    } catch {
        Write-Host ("    [WARN] corrupt result {0}.json, will rerun" -f $id) -ForegroundColor Yellow
        Remove-Item $f -Force
        return $false
    }
}

if (Test-Path $manifestPath) {
    Write-Host "=== 1) reuse existing manifest ===" -ForegroundColor Cyan
    $manifest = Get-Content $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $batches = @()
    foreach ($b in $manifest) { $batches += , @($b.instances) }
} else {
    Write-Host "=== 1) scan candidates (instance.json + csv data + numeric optimal_value) ===" -ForegroundColor Cyan
    $cands = @()
    foreach ($d in Get-ChildItem $ds -Directory) {
        $ij = Join-Path $d.FullName "instance.json"
        if (-not (Test-Path $ij)) { continue }
        $csvs = @(Get-ChildItem (Join-Path $d.FullName "data") -Filter *.csv -ErrorAction SilentlyContinue)
        if ($csvs.Count -eq 0) { continue }
        try { $meta = Get-Content $ij -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
        $v = $meta.optimal_value
        if ($null -eq $v) { continue }
        $asNum = $null
        try { $asNum = [double]$v } catch { $asNum = $null }
        if ($null -eq $asNum) { continue }
        $cands += $d.Name
    }
    Write-Host ("    candidates: {0} / 223" -f $cands.Count)

    Write-Host "=== 2) random pick 20 ===" -ForegroundColor Cyan
    $pick = @($cands | Get-Random -Count 20)
    $batches = @()
    for ($i = 0; $i -lt 20; $i += 4) {
        $batches += , @($pick[$i], $pick[$i + 1], $pick[$i + 2], $pick[$i + 3])
    }
    $parts = for ($b = 0; $b -lt $batches.Count; $b++) {
        $items = ($batches[$b] | ForEach-Object { '"' + $_ + '"' }) -join ", "
        '  {{"batch": {0}, "instances": [{1}]}}' -f ($b + 1), $items
    }
    Set-Content -Path $manifestPath -Value ("[`r`n" + ($parts -join ",`r`n") + "`r`n]") -Encoding UTF8
}

Write-Host "=== 3) batches (skip finished) ===" -ForegroundColor Cyan
for ($b = 0; $b -lt $batches.Count; $b++) {
    Write-Host ("    batch {0}: {1}" -f ($b + 1), ($batches[$b] -join "  +  "))
}

Write-Host "`n=== 4) launch parallel processes ===" -ForegroundColor Cyan
$started = @()
for ($b = 0; $b -lt $batches.Count; $b++) {
    $log = Join-Path $out ("batch{0}.log" -f ($b + 1))
    $bs  = Join-Path $out ("batch{0}.ps1" -f ($b + 1))
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add("`$ErrorActionPreference = 'Continue'")
    $lines.Add("Set-Location '$p'")
    $todo = 0
    foreach ($n in $batches[$b]) {
        if (Test-ResultOk $n) {
            $lines.Add('Write-Host ("[SKIP] ' + $n + ' already done")')
            continue
        }
        $todo++
        $lines.Add('$t0 = Get-Date')
        $lines.Add('Write-Host ("[START] ' + $n + ' " + $t0.ToString("HH:mm:ss"))')
        $lines.Add(("& '{0}' -m nl2opt eval --data '{1}' --use-llm --out '{2}'" -f $py, (Join-Path $ds $n), (Join-Path $out "$n.json")))
        $lines.Add('Write-Host ("[END] ' + $n + ' exit=$LASTEXITCODE seconds=" + [int]((Get-Date) - $t0).TotalSeconds)')
    }
    if ($todo -eq 0) {
        Write-Host ("    batch {0}: all done, skipped" -f ($b + 1)) -ForegroundColor DarkGray
        continue
    }
    Set-Content -Path $bs -Value ($lines -join "`r`n") -Encoding ASCII
    $proc = Start-Process -FilePath "powershell" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $bs) `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput $log `
        -RedirectStandardError (Join-Path $out ("batch{0}.err.log" -f ($b + 1)))
    $started += [pscustomobject]@{ Batch = $b + 1; Pid = $proc.Id; Log = $log; Todo = $todo }
}

$started | Format-Table -AutoSize

if ($started.Count -eq 0) {
    Write-Host "=== nothing to do, all 20 results present ===" -ForegroundColor Green
    exit 0
}

Write-Host "=== monitoring (Ctrl+C stops monitoring only) ===" -ForegroundColor Yellow
while ($true) {
    $alive = @(Get-Process -Id ($started.Pid) -ErrorAction SilentlyContinue).Count
    $done  = @(Get-ChildItem $out -Filter "*.json" -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne "manifest.json" }).Count
    Write-Host ("[{0}] alive {1}/{2}   results {3}/20" -f (Get-Date -Format "HH:mm:ss"), $alive, $started.Count, $done)
    if ($alive -eq 0) { break }
    Start-Sleep -Seconds 30
}

Write-Host "`n=== ALL DONE ===" -ForegroundColor Cyan
Get-ChildItem $out -Filter "*.json" | Where-Object { $_.Name -ne "manifest.json" } | Select-Object Name, Length | Format-Table -AutoSize