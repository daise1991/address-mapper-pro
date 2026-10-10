# 初回セットアップ（何度実行しても安全）
#   1. Python と Node.js の確認
#   2. Python 仮想環境と部品のインストール
#   3. 設定ファイル backend\.env の作成（J-Quants APIキーの入力）
#   4. 画面（frontend）のビルド
#   5. データベース作成
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "`n[エラー] $msg" -ForegroundColor Red; Read-Host "Enter で終了"; exit 1 }

Step "1/5 Python と Node.js を確認"
$py = $null
foreach ($c in @("py -3.12", "py -3.13", "py -3.11", "python")) {
    try {
        $v = & cmd /c "$c --version 2>&1"
        if ($LASTEXITCODE -eq 0 -and $v -match "Python 3\.(1[1-9])") { $py = $c; break }
    } catch {}
}
if (-not $py) { Fail "Python 3.11 以上が見つかりません。PowerShell で次を実行してから、もう一度 setup.bat を実行してください:`n  winget install Python.Python.3.12" }
Write-Host "Python: $py"
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Fail "Node.js が見つかりません。次を実行してから、もう一度 setup.bat を実行してください:`n  winget install OpenJS.NodeJS.LTS" }
Write-Host "Node.js: $(node --version)"

Step "2/5 Python の部品をインストール"
Set-Location $Backend
if (-not (Test-Path ".venv")) { & cmd /c "$py -m venv .venv"; if ($LASTEXITCODE) { Fail "仮想環境の作成に失敗" } }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip -q
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt -q
if ($LASTEXITCODE) { Fail "部品のインストールに失敗しました" }

Step "3/5 設定ファイル（backend\.env）"
if (-not (Test-Path ".env")) {
    $key = Read-Host "J-Quants の APIキーを貼り付けて Enter（まだ無ければ空のまま Enter → デモデータで起動）"
    $edinet = Read-Host "EDINET の APIキー（無ければ空のまま Enter）"
    $bytes = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_").TrimEnd("=")
    $lines = Get-Content ".env.example" -Encoding UTF8
    $mode = if ($key) { "jquants" } else { "demo" }
    $db = if ($key) { "./data/stock.db" } else { "./data/demo.db" }
    $lines = $lines -replace "^STOCK_DATA_MODE=.*", "STOCK_DATA_MODE=$mode" `
                    -replace "^STOCK_DB_PATH=.*", "STOCK_DB_PATH=$db" `
                    -replace "^JQUANTS_API_KEY=.*", "JQUANTS_API_KEY=$key" `
                    -replace "^EDINET_API_KEY=.*", "EDINET_API_KEY=$edinet" `
                    -replace "^STOCK_APP_TOKEN=.*", "STOCK_APP_TOKEN=$token"
    [IO.File]::WriteAllLines((Join-Path $Backend ".env"), $lines, (New-Object Text.UTF8Encoding $false))
    Write-Host ".env を作成しました（モード: $mode）。アクセス用トークン: $token" -ForegroundColor Yellow
    Write-Host "※ スマホから使うときに必要です。backend\.env にも保存されています。"
} else {
    Write-Host "既存の .env を使います（変更はメモ帳で backend\.env を編集）"
}

Step "4/5 画面をビルド"
Set-Location $Frontend
& npm ci --no-audit --no-fund
if ($LASTEXITCODE) { Fail "npm ci に失敗しました" }
& npm run build
if ($LASTEXITCODE) { Fail "画面のビルドに失敗しました" }

Step "5/5 データベースを準備"
Set-Location $Backend
& .\.venv\Scripts\python.exe -m stock_analysis init
& .\.venv\Scripts\python.exe -m stock_analysis gen-vapid
$envText = Get-Content ".env" -Raw -Encoding UTF8
if ($envText -match "STOCK_DATA_MODE=demo") {
    Write-Host "デモデータを作成中…"
    & .\.venv\Scripts\python.exe -m stock_analysis demo
} else {
    Write-Host "J-Quants / EDINET への接続を確認中…"
    & .\.venv\Scripts\python.exe -m stock_analysis check
}

Write-Host "`n完了しました。start.bat をダブルクリックすると画面が開きます。" -ForegroundColor Green
Write-Host "毎日自動で分析させるには register-task.bat を1回実行してください。"
Read-Host "Enter で閉じる"
