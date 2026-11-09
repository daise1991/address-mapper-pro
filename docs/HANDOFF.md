# HANDOFF（AIから人・Codexへの引き継ぎ）

AIは1項目1行で追記。人は完了したら `[x]` にする。

## 人がやる
- [ ] Google Maps Platform の API キー発行（フォールバック用・後回し可。`.env` に設定、コミットしない）
- [ ] サンプル住所データの用意（個人情報を含まないもの）

## Codex向け（仕様確定済みの単機能タスク）
先頭の `[low]/[medium]/[high]` は、人が Codex 起動前に設定する推論の強さ。

- [ ] [low] `docs/codex-only/導入手順書.md` の STEP 2 に「作業の種類→推論の強さ」の対応表を追加（low: 名前変更・テスト追加・小修正 / medium: 単機能の実装 / high: 設計・原因不明のバグ）。他の STEP は変更しない。完了後 `docs/codex-only.zip` を作り直す

## 人への質問
