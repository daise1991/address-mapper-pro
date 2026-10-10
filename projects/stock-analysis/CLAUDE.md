# CLAUDE.md（利用者のPCで動く Claude Code 向けの引き継ぎ）

このフォルダは、日本株60銘柄のAI投資分析Webアプリです。詳細は README.md、はじめに.md、docs/ を参照してください。

## 環境（Windows）
- バックエンド: `backend\.venv\Scripts\python -m stock_analysis <コマンド>`。作業フォルダは `backend` です。
- 設定: `backend\.env`（APIキー入り）。**中身を表示・送信・commit しないでください。**
- DB: `backend\data\stock.db`、ログ: `backend\data\app.log` と `console.log`
- 自動起動: タスクスケジューラの「StockAnalysis」（`scripts\register-task.bat`）

## よく使う操作
| 目的 | コマンド（backend で実行） |
|---|---|
| 最新版の取得 | `git pull` → `..\scripts\stop.bat` → `cd ..\frontend && npm run build` → `cd ..\backend` → `..\scripts\start.bat` |
| 接続チェック | `.venv\Scripts\python -m stock_analysis check` |
| 採点のやり直し | `.venv\Scripts\python -m stock_analysis rescore` |
| 点数の根拠 | `.venv\Scripts\python -m stock_analysis explain 7012` |
| 調査前提の取込 | `.venv\Scripts\python -m stock_analysis import-research ..\assumptions\batch1_2026-10.json` |
| 前提の承認 | `.venv\Scripts\python -m stock_analysis approve 1944 5032 --note "確認内容"`（利用者の承認を得てから実行） |
| テスト | `.venv\Scripts\python -m pytest -q` |

## ルール（利用者の指示）
- 自動発注、証券会社への自動ログイン、注文画面の操作はしない。
- 外部サービスの契約・課金、Webアプリの外部公開は、事前に承認を得る。
- 評価前提の「検証済み」への変更（approve）と、計算方式の変更は、利用者の承認を得てから行う。
- APIキーなどの秘密情報を画面やチャットに出さない。
- 結果は、専門用語を避けて短く報告する。

## 現在の作業（2026-10-10 時点）
1. `git pull` → `rescore` を実行し、`explain 7012` と `explain 5803` で売り点数の内訳を確認する。株式分割を増資と誤判定していた不具合の修正確認で、「増資・希薄化」が0付近になっていれば修正できている。
2. `import-research ..\assumptions\batch1_2026-10.json` の出力にある `= 一致` / `= 要確認` を利用者に報告する（調査値と J-Quants 公式データの照合）。
3. 想定PERの方針（A: 調査値のまま／B: 過去5年PERの中央値）を利用者に確認し、残り50銘柄の調査に進む。
