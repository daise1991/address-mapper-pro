"""重大リスク警告（売り点数とは別）。

警告が出た銘柄は、売り点数に関係なく保有株は「売却・撤退の再評価対象」、購入候補からは除外する。
警告だけで売却を確定しない（原因と影響を利用者が確認する）。株式分割は希薄化として扱わない。
"""
from __future__ import annotations

from typing import Any

from .features import V5Features

FORECAST_CUT = -0.15        # 営業利益予想の15%以上の減額
EQUITY_RATIO_LOW = 0.20
DILUTION = 0.10             # 1年で10%以上の株数増加（分割補正後）


def detect(f: V5Features) -> list[dict[str, Any]]:
    g = f.get
    out: list[dict[str, Any]] = []
    cut = g("max_op_forecast_cut_120d")
    if cut is not None and cut <= FORECAST_CUT:
        out.append({"type": "業績悪化", "detail": f"120日以内に営業利益予想を{cut:.0%}減額", "unresolved": True})
    if g("operating_cf") is not None and g("operating_cf") < 0 and g("equity_ratio") is not None \
            and g("equity_ratio") < EQUITY_RATIO_LOW:
        out.append({"type": "財務問題", "detail": f"営業CFが赤字かつ自己資本比率{g('equity_ratio'):.0%}", "unresolved": True})
    if g("equity") is not None and g("equity") < 0:
        out.append({"type": "財務問題", "detail": "債務超過", "unresolved": True})
    if g("share_change") is not None and g("share_change") >= DILUTION:
        out.append({"type": "大規模希薄化", "detail": f"株数が1年で{g('share_change'):+.0%}（分割補正後）", "unresolved": False})
    if g("disc_offering_120d"):
        out.append({"type": "大規模希薄化", "detail": f"120日以内に増資等の開示 {g('disc_offering_120d')}件", "unresolved": False})
    return out


def blocks_purchase(risks: list[dict[str, Any]]) -> bool:
    """重大な財務問題・業績悪化が未解決、または希薄化の警告があれば購入候補から除外する。"""
    return bool(risks)
