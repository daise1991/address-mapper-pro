# 定性評価（市場拡大・競争優位）入力形式 v2 と検証の定義　2026-10-10

v5 の参考計算で使う定性2項目の、調査ファイルの形式・検査・検証済みの定義を定める。
目的は高得点の銘柄を増やすことではなく、将来性を正確かつ再現可能に評価すること。
v4（正式方式）はこの仕様の影響を受けない。

## 1. 3つの検証フラグ

| フラグ | 誰が付けるか | 意味（検証済みの定義） | 対象の点数 |
|---|---|---|---|
| `company_verified` | 調査・確認担当が取込ファイルで明示 | 競争優位の5要素すべてに、会社の一次情報（決算短信・決算説明資料・中期経営計画・有価証券報告書・会社公式IR・同業各社の一次情報）の出典があり、内容と対象年度を照合済み。シェアのみ、信頼できる外部調査（発行元・発表日・URL・時点が明記）を認める | 競争優位 |
| `market_verified` | 調査・確認担当が取込ファイルで明示 | 市場拡大の全セグメントで、市場成長率の出典（発行元・資料名・URL・発表日・対象市場・予測期間）と、会社の対象事業との対応関係を確認済み。調整（±5）がある場合はその根拠も確認済み | 市場拡大 |
| `calculation_verified` | アプリが自動判定（手で付けない）。項目ごとに判定 | 下記4章の「計算」エラーが0件で、調査に書かれた基礎点・合計点がアプリの再計算と一致 | 両方 |

- 取込ファイルで `company_verified` / `market_verified` が true でも、アプリの検査で「根拠」エラーがあれば検証済みとして扱わない。
- 検査は採点のたびに評価日を基準にやり直す（出典の鮮度・予測期間は日付で変わるため）。
- 旧形式の `primary_verified` は使わない（v1 形式のファイルは正式点数の対象外）。

## 2. 正式点数と参考点数

| 点数 | 市場拡大 | 競争優位 |
|---|---|---|
| 正式点数 | `market_verified` と市場拡大の `calculation_verified` がそろったときだけ。そろわなければ **未評価**（0点にしない） | `company_verified` と競争優位の `calculation_verified` がそろったときだけ。そろわなければ **未評価** |
| 参考点数 | 検証前でも計算する（構成比合計のずれは正規化、規則外の点数は最も近い値に寄せ、警告を付ける） | 同左 |

- 成長期待点の正式点数は、未評価の項目を0点にせず、充足率（80%以上で正式）で扱う。定性2項目が未評価なら充足率は最大70%で、成長期待点は「暫定（参考）」になり、正式ランキング・注文候補には使わない。
- 画面・報告では「正式」「参考」「未評価（理由）」を必ず分けて表示する。

## 3. 入力形式（1銘柄）

```json
{
  "schema_version": 2,
  "code": "6857", "name": "アドバンテスト", "researched_at": "2026-10-10",
  "market_expansion": {
    "segments": [{
      "segment": "テストシステム", "sales_share": 0.903,
      "sales_share_source": {"title": "2026年3月期 決算短信", "url": "...", "published": "2026-04-27", "fiscal_period_end": "2026-03"},
      "market": "半導体テスタ世界市場", "cagr": 0.073, "cagr_period": "2025-2030",
      "source": {"publisher": "調査会社名", "title": "資料名", "url": "...", "published": "2025-11-20"},
      "fit": "会社のテストシステム売上はこの市場に含まれる（SoC・メモリテスタ）。サービス売上は含まない"
    }],
    "adjustment": 0,
    "adjustment_basis": {"reason": "…", "url": "...", "published": "YYYY-MM-DD"},
    "rationale": "…"
  },
  "competitive_advantage": {
    "components": {
      "share": {"points": 30, "evidence": "…", "as_of": "CY2025",
                "sources": [{"type": "primary", "title": "…", "url": "...", "published": "YYYY-MM-DD"}]},
      "margin_vs_peers": {"points": 25, "margin_type": "operating",
        "company": {"margin": 0.442, "fiscal_period_end": "2026-03", "accounting_standard": "IFRS",
                    "source": {"type": "primary", "title": "…", "url": "...", "published": "YYYY-MM-DD"}},
        "peers": [{"name": "Teradyne", "margin": 0.204, "fiscal_period_end": "2025-12", "accounting_standard": "USGAAP",
                   "source": {"type": "primary", "title": "…", "url": "...", "published": "YYYY-MM-DD"}}],
        "standard_note": "会計基準が異なる場合の扱い（差が判定に影響しない理由）"},
      "technology": {"points": 20, "evidence": "…", "sources": [ ... ]},
      "orders_customers": {"points": 9, "evidence": "…", "as_of": "2026-06", "sources": [ ... ]},
      "midterm_plan": {"points": 10, "evidence": "…", "plan_name": "…", "target_fiscal_year": "2027-03",
                       "actual_fiscal_year": "2026-03", "sources": [ ... ]}
    },
    "rationale": "…"
  },
  "verification": {"company_verified": false, "market_verified": false, "verified_at": null, "verified_by": null, "notes": "…"},
  "dividend": {"latest_actual_dps": 0, "forecast_dps": 0, "forecast_period": "…", "payout_policy": "…", "source_url": "..."},
  "unverified": ["…"]
}
```

- `sources[].type`: `primary`（会社・同業の一次情報）または `external`（外部調査・報道）。
- 日付は `YYYY-MM-DD`（日が不明なら `YYYY-MM`）。`unknown` は不可（不明なら検証済みにしない）。
- `margin_type`: `operating`（営業利益率）/ `net`（親会社帰属の純利益率）/ `ebit`。会社と同業で同じ種類にそろえる。

## 4. アプリの検査（v5/qualitative.py）

エラーは2種類あり、どちらも正式点数を未評価にする。「根拠」エラーは検証済み（company_verified / market_verified）を取り消す。「計算」エラーは calculation_verified を取り消す。「警告」は表示のみ。

### 市場拡大
| 検査 | 内容 | 区分 |
|---|---|---|
| 構成比の合計 | セグメントの `sales_share` 合計が 0.95〜1.05 | 計算（参考点数は正規化して計算） |
| 市場成長率の出典 | 各セグメントに `market`・`cagr`・`cagr_period`・`source.url`・`source.published`・`source.publisher`・`fit` | 根拠 |
| 発表日の鮮度 | `source.published` が評価日から3年以内 | 根拠 |
| 予測期間 | `cagr_period` が `YYYY-YYYY`、期間3年以上、終わりが評価年以降、始まりが2029年以前 | 根拠 |
| 市場の重複 | 同じ市場（名称の空白・大小文字を除いて一致）を複数セグメントで使っていない | 計算 |
| 調整 ±5 | 整数で -5〜+5。0 以外なら `adjustment_basis` の理由とURLが必要 | 範囲外は計算（参考点数でのみ ±5 に丸める）、根拠なしは根拠 |
| 基礎点・合計 | 調査の `base_points`・`score` が規則の再計算と一致 | 計算 |

### 競争優位
| 検査 | 内容 | 区分 |
|---|---|---|
| 点数の値 | 各要素の点数が許可値（下表）のどれか | 計算（自動補正しない。参考点数のみ最も近い値に寄せる） |
| 出典 | 各要素に出典URLと発表日 | 根拠 |
| 利益率の種類 | 会社と全同業の `margin_type` が同じ | 種類の記載なしは根拠、不一致は計算 |
| 対象年度 | 同業の決算期末が会社の決算期末から12か月以内 | 記載なしは根拠、ずれは計算 |
| 会計基準 | 全社に `accounting_standard`。会社と違う場合は `standard_note` が必要 | 根拠 |
| 同業の数 | 2社以上 | 根拠 |
| 利益率の判定 | 同業中央値との差 +5pt超→25、±5pt以内→15、-5pt未満→5。記載の点数と一致 | 計算 |
| シェア | `as_of`（時点）と出典が必要 | 根拠 |
| 中計 | `plan_name`・`target_fiscal_year`・`actual_fiscal_year` と一次情報の出典が必要 | 根拠 |
| 一次情報 | `company_verified` が true なら、シェア以外の全要素に `type: primary` の出典が必要 | 根拠 |

許可値: シェア 30/22/12/5、同業比利益率 25/15/5、技術 20/12/5、受注・顧客 15/9/4、中計の実績 10/6/2。

### 市場拡大の基礎点
市場成長率（年平均）: 0%未満→20、0〜3%→40、3〜7%→60、7〜12%→75、12〜20%→88、20%以上→95。売上構成比で加重平均し、調整（±5）を加える。

## 5. 保存先
`qualitative_v5` 表（v5 専用。v4 は読まない）。取込のたびに `assumption_research_history` へ履歴を残す。

## 6. 旧形式（v1）・手入力の扱い
v1 形式（`primary_verified` を使う旧ファイル）と画面の手入力（qualitative_inputs）は、根拠の検証ができないため参考点数にだけ使う。正式点数は未評価。
