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

## 現在の作業（2026-10-10 時点・v5 比較検証）
v5 は比較検証用の参考採点です。**正式な採点方式は v4 のまま**です。v5 の本番採用・切替は、60銘柄の比較結果を利用者が確認し、承認するまで行わないでください。
- v5 は自動通知・売買候補・注文計画・毎日の自動処理に使われません（毎日の参考採点は `STOCK_V5_SHADOW=1` のときだけ。既定は実行しない。設定を変えないこと）。
- v5 の想定PERは「J-Quants の過去5年PER中央値」（利用者承認済みの方針B）。異常の目印が付いた銘柄は正式評価にしない。
- 定性評価（市場拡大・競争優位）10銘柄は一次情報（決算資料・中期経営計画・有価証券報告書）の裏付け前のため、正式点数には使わない（参考点数のみ）。

手順（backend で実行）:
1. `git pull` → `..\scripts\stop.bat` → `cd ..\frontend && npm run build` → `cd ..\backend` → `..\scripts\start.bat`
2. `.venv\Scripts\python -m stock_analysis import-qualitative ..\assumptions\qualitative_batch1_2026-10.json`
3. `.venv\Scripts\python -m stock_analysis compare-v5` → 出力 `data\compare_v4_v5_日付.md` を利用者に報告する。報告内容:
   60銘柄ごとの成長期待点・買い点数・売り点数の v4→v5、信頼度A〜D、充足率、重大リスク、正式評価の銘柄数、
   購入候補・売却候補の追加／除外、「異常な点数変動・欠損データ・想定外の判定」の一覧、PERの異常の目印が付いた銘柄。
4. `.venv\Scripts\python -m stock_analysis explain 7012` で川崎重工の株式分割が希薄化と誤判定されていないか確認する（比較表の希薄化 v5 が 0 であること）。
5. `.venv\Scripts\python -m pytest -q` の結果を報告する。
