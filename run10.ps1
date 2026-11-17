# 随机抽 10 道 MIPLIB-NL 题，分 5 批并行跑（每批 2 道）
#
# 用法（在仓库根目录执行）：
#   powershell -ExecutionPolicy Bypass -File .\run10.ps1
#
# 特性：
#   - 随机抽题，但自动跳过两种“必崩/无意义”的题：
#       a) optimal_value 不是数字的（"infeasible" / "impossible"）→ 会触发 verifier 的 ValueError
#       b) 没有 CSV 数据文件的 → 走不到“符号 IR 读数据”这条链路
#   - 5 个独立进程并行，每批日志单独一个文件（UTF-8）
#   - 用作者原配置（config.yaml 的 deepseek-flash），不覆盖模型

$ErrorActionPreference = "Stop"

$p   = "C:\Users\roman\Desktop\nl2opt_review\nl2opt-main"
$py  = "C:\Users\roman\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$ds  = "D:\Downloads\MIPLIB-NL\dataset"
$out = Join-Path $p "runs10"

$env:PYTHONPATH       = "$p\src;C:\Users\roman\Desktop\nl2opt_review\pylibs"
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
Remove-Item Env:DEEPSEEK_MODEL -ErrorAction SilentlyContinue   # 用 config.yaml 的 deepseek-flash
$env:PYTHONUTF8 = "1"

Set-Location $p
New-Item -ItemType Directory -Force -Path $out | Out-Null

Write-Host "=== 1) 筛候选题（有 instance.json + 有 CSV + optimal_value 是数字）===" -ForegroundColor Cyan
$cands = @()
foreach ($d in Get-ChildItem $ds -Directory) {
    $ij = Join-Path $d.FullName "instance.json"
    if (-not (Test-Path $ij)) { continue }
    $csvs = @(Get-ChildItem (Join-Path $d.FullName "data") -Filter *.csv -ErrorAction SilentlyContinue)
    if ($csvs.Count -eq 0) { continue }
    try { $meta = Get-Content $ij -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
    $v = $meta.optimal_value
    if ($null -eq $v) { continue }          # 没有参考答案 → 会被判成“无答案跳过”，不该入选
    $asNum = $null
    try { $asNum = [double]$v } catch { $asNum = $null }
    if ($null -eq $asNum) { continue }
    $cands += $d.Name
}
Write-Host ("    候选 {0} 道（总 223）" -f $cands.Count)

Write-Host "=== 2) 随机抽 10 道 ===" -ForegroundColor Cyan
$pick = $cands | Get-Random -Count 10

Write-Host "=== 3) 分 5 批 ===" -ForegroundColor Cyan
$batches = @()
for ($i = 0; $i -lt 10; $i += 2) {
    $batches += , @($pick[$i], $pick[$i + 1])
}
for ($b = 0; $b -lt $batches.Count; $b++) {
    Write-Host ("    批 {0}: {1}" -f ($b + 1), ($batches[$b] -join "  +  "))
}

Write-Host "`n=== 4) 并行启动 5 个进程 ===" -ForegroundColor Cyan
$started = @()
for ($b = 0; $b -lt $batches.Count; $b++) {
    $names = $batches[$b]
    $log   = Join-Path $out ("batch{0}.log" -f ($b + 1))
    $lines = @("==== 批 $($b+1) ====")
    foreach ($n in $names) {
        $lines += ("& '{0}' -m nl2opt eval --data '{1}' --use-llm --out '{2}'" -f $py, (Join-Path $ds $n), (Join-Path $out "$n.json"))
        $lines += '""'
        $lines += ("Write-Host '---- 完成 {0} ----'" -f $n)
    }
    $script = $lines -join "`n"
    $proc = Start-Process -FilePath "powershell" `
        -ArgumentList @("-NoProfile", "-Command", $script) `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput $log `
        -RedirectStandardError (Join-Path $out ("batch{0}.err.log" -f ($b + 1)))
    $started += [pscustomobject]@{ Batch = $b + 1; Pid = $proc.Id; Log = $log; Instances = ($names -join " + ") }
}

$started | Format-Table -AutoSize

Write-Host "=== 实时监控（Ctrl+C 只停止监控，不停止后台任务）===" -ForegroundColor Yellow
Write-Host "    单独看某批： Get-Content `"$out\batch1.log`" -Encoding UTF8 -Wait -Tail 20" -ForegroundColor DarkGray
Write-Host ""

while ($true) {
    $alive = @(Get-Process -Id ($started.Pid) -ErrorAction SilentlyContinue).Count
    $done  = @(Get-ChildItem $out -Filter "*.json" -ErrorAction SilentlyContinue).Count
    Write-Host ("[{0}] 存活进程 {1}/5   已产出结果 {2}/10" -f (Get-Date -Format "HH:mm:ss"), $alive, $done)
    if ($alive -eq 0) { break }
    Start-Sleep -Seconds 20
}

Write-Host "`n=== 全部结束，逐批摘要 ===" -ForegroundColor Cyan
for ($b = 1; $b -le 5; $b++) {
    Write-Host "---- 批 $b ----" -ForegroundColor DarkCyan
    $log = Join-Path $out ("batch{0}.log" -f $b)
    if (Test-Path $log) {
        Get-Content $log -Encoding UTF8 | Select-String -Pattern "成功|False|True|通过率|ValueError" | ForEach-Object { "    " + $_.Line.Trim() }
    }
}
Write-Host "`n结果 JSON 在: $out" -ForegroundColor Green