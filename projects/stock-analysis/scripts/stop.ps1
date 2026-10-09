# 起動中のWebアプリ・自動分析を停止
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match "stock_analysis" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "停止: PID $($_.ProcessId)" }
