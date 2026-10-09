# Windows ログオン時に Webアプリ＋自動分析（16:00〜18:00 の処理）を起動するタスクを登録する
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Py = Join-Path $Backend ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Py)) { Write-Host "先に setup.bat を実行してください" -ForegroundColor Red; Read-Host; exit 1 }
$action = New-ScheduledTaskAction -Execute $Py -Argument "-m stock_analysis run-all" -WorkingDirectory $Backend
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "StockAnalysis" -Action $action -Trigger $trigger -Settings $settings `
    -Description "日本株AI分析: Webアプリと大引け後の自動分析" -Force | Out-Null
Start-ScheduledTask -TaskName "StockAnalysis"
Write-Host "登録しました。次回から Windows にサインインすると自動で起動します。" -ForegroundColor Green
Write-Host "注意: 15:30〜18:30 にPCがスリープしていると分析されません（設定 → 電源 → スリープ を「なし」推奨）。"
Read-Host "Enter で閉じる"
