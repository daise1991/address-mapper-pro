"""v5 共通: 項目（構成要素付き）の採点と、正式点数・参考点数・充足率の計算。

- 取得できた構成要素だけで項目点を出す（欠損を0点にしない）
- 充足率 = Σ 項目の配点 × 取得できた構成要素の割合 ÷ 配点合計
- 参考点数 = 評価できた項目だけの加重平均（未評価を含むため「参考」）
- 正式点数 = 充足率80%以上かつ必須項目がすべて評価済みのときだけ、参考点数を正式とする
  （欠損が多いまま高得点になることを防ぐ。正式でない点数はランキング・注文候補に使わない）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..scoring.valuation import round_half_up

FORMAL_COVERAGE = 0.8
PROVISIONAL_COVERAGE = 0.5


def scale(x: float | None, points: Sequence[tuple[float, float]]) -> float | None:
    """区分線形の点数化（両端は頭打ち）。points は x 昇順。"""
    if x is None:
        return None
    if x <= points[0][0]:
        return float(points[0][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(points[-1][1])


@dataclass
class Item:
    key: str
    label: str
    weight: float
    required: bool
    components: list[tuple[str, float, float | None]] = field(default_factory=list)  # (名前, 構成比, 点数 or None)

    @property
    def available_share(self) -> float:
        total = sum(w for _, w, _ in self.components) or 1
        return sum(w for _, w, s in self.components if s is not None) / total

    @property
    def score(self) -> float | None:
        avail = [(w, s) for _, w, s in self.components if s is not None]
        if not avail:
            return None
        return sum(w * s for w, s in avail) / sum(w for w, _ in avail)


def summarize(items: list[Item]) -> dict[str, Any]:
    total_w = sum(i.weight for i in items)
    coverage = sum(i.weight * i.available_share for i in items) / total_w
    evaluated = [i for i in items if i.score is not None]
    ref = (sum(i.weight * i.score for i in evaluated) / sum(i.weight for i in evaluated)) if evaluated else None
    missing_required = [i.label for i in items if i.required and i.score is None]
    if ref is None or coverage < PROVISIONAL_COVERAGE:
        status = "unrated"
    elif coverage >= FORMAL_COVERAGE and not missing_required:
        status = "formal"
    else:
        status = "provisional"
    reference = round_half_up(ref) if ref is not None else None
    return {
        "formal": reference if status == "formal" else None,
        "reference": reference,
        "coverage": round(coverage, 3),
        "status": status,  # formal / provisional / unrated
        "missing_required": missing_required,
        "unevaluated": [i.label for i in items if i.score is None],
        "items": {i.key: {"label": i.label, "weight": i.weight, "required": i.required,
                          "score": None if i.score is None else round(i.score, 1),
                          "available_share": round(i.available_share, 2),
                          "components": [{"name": n, "share": w, "score": None if s is None else round(s, 1)}
                                         for n, w, s in i.components]} for i in items},
    }
