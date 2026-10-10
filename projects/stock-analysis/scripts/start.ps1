# Webアプリ＋自動分析を起動し、ブラウザで開く
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
Set-Location $Backend
if (-not (Test-Path ".venv\Scripts\python.exe")) { Write-Host "先に setup.bat を実行してください" -ForegroundColor Red; Read-Host; exit 1 }
$running = $false
try { $running = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/api/health" -TimeoutSec 2).StatusCode -eq 200 } catch {}
if (-not $running) {
    Start-Process -FilePath ".venv\Scripts\pythonw.exe" -ArgumentList "-m", "stock_analysis", "run-all" -WorkingDirectory $Backend -WindowStyle Hidden
    # 起動完了まで最大30秒待つ
    for ($i = 0; $i -lt 30 -and -not $running; $i++) {
        Start-Sleep -Seconds 1
        try { $running = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/api/health" -TimeoutSec 2).StatusCode -eq 200 } catch {}
    }
    if (-not $running) {
        Write-Host "起動できませんでした。backend\data\console.log と app.log の内容を Claude に送ってください。" -ForegroundColor Red
        Read-Host "Enter で閉じる"; exit 1
    }
}
$token = (Select-String -Path ".env" -Pattern "^STOCK_APP_TOKEN=(.*)$" -Encoding UTF8 | Select-Object -First 1).Matches.Groups[1].Value
Start-Process ("http://127.0.0.1:8000/" + $(if ($token) { "#token=$token" } else { "" }))
