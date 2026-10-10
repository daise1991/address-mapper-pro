# 起動中のWebアプリ・自動分析を停止
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match "stock_analysis" } |
    ForEach-Object {
        # 仮想環境の python は親子2プロセスで動くため、親を止めると子は既に終了していることがある
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "停止: PID $($_.ProcessId)"
    }
