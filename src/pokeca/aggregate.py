"""集計ロジック。

「いま いちばん つよいデッキ」を出すのがここの役目。
日々のデッキ作りの参考にするなら、新着一覧より集計のほうが効く。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from src.pokeca.models import DeckResult

# ランキングの集計期間 (日数)。子ども向けページのタブに対応する。
PERIODS = {
    "7d": ("さいきん 1しゅうかん", 7),
    "30d": ("さいきん 1かげつ", 30),
    "all": ("ぜんぶ", 0),
}


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def filter_by_period(
    results: list[DeckResult], days: int, today: date | None = None
) -> list[DeckResult]:
    """直近 days 日ぶんに絞る。days=0 なら全期間。"""
    if days <= 0:
        return list(results)
    base = today or max(
        (d for d in (_parse_date(r.date) for r in results) if d), default=date.today()
    )
    cutoff = base - timedelta(days=days - 1)
    out = []
    for record in results:
        parsed = _parse_date(record.date)
        if parsed and cutoff <= parsed <= base:
            out.append(record)
    return out


def deck_ranking(
    results: list[DeckResult], days: int = 7, today: date | None = None
) -> list[dict]:
    """デッキごとの優勝/準優勝回数ランキングを返す。

    並び順は「優勝回数が多い順 → 準優勝回数が多い順 → デッキ名」。
    優勝を2点、準優勝を1点として score も付ける。
    """
    # デッキ名が未取得のレコードは集計しない。
    # 名無しをひとまとめにすると、それが常に1位になってランキングが嘘になる。
    scoped = [r for r in filter_by_period(results, days, today) if r.deck_name]

    buckets: dict[str, dict] = {}
    for record in scoped:
        entry = buckets.setdefault(
            record.deck_key,
            {
                "deck_key": record.deck_key,
                "deck_name": record.deck_name,
                "first": 0,
                "second": 0,
                "latest_date": "",
            },
        )
        if record.rank == 1:
            entry["first"] += 1
        elif record.rank == 2:
            entry["second"] += 1
        if record.date > entry["latest_date"]:
            entry["latest_date"] = record.date
            # 表示名は最新の表記に合わせる
            entry["deck_name"] = record.deck_name

    ranked = []
    for entry in buckets.values():
        entry["total"] = entry["first"] + entry["second"]
        entry["score"] = entry["first"] * 2 + entry["second"]
        ranked.append(entry)

    ranked.sort(key=lambda e: (-e["first"], -e["second"], e["deck_name"]))
    for position, entry in enumerate(ranked, start=1):
        entry["position"] = position
    return ranked


def deck_choices(results: list[DeckResult]) -> list[dict]:
    """絞り込みチップ用のデッキ一覧 (登場回数の多い順)。"""
    counts: dict[str, dict] = defaultdict(lambda: {"count": 0, "deck_name": ""})
    for record in results:
        if not record.deck_name:
            continue
        entry = counts[record.deck_key]
        entry["count"] += 1
        if not entry["deck_name"]:
            entry["deck_name"] = record.deck_name
    out = [
        {"deck_key": key, "deck_name": value["deck_name"], "count": value["count"]}
        for key, value in counts.items()
    ]
    out.sort(key=lambda e: (-e["count"], e["deck_name"]))
    return out


def summary(results: list[DeckResult], today: date | None = None) -> dict:
    """ページ上部に出すサマリー。"""
    dates = sorted({r.date for r in results if r.date})
    ranking_7d = deck_ranking(results, days=7, today=today)
    return {
        "total": len(results),
        "first_count": sum(1 for r in results if r.rank == 1),
        "second_count": sum(1 for r in results if r.rank == 2),
        "latest_date": dates[-1] if dates else "",
        "oldest_date": dates[0] if dates else "",
        "store_count": len({r.store_key for r in results if r.store_key}),
        "top_deck": ranking_7d[0] if ranking_7d else None,
    }
