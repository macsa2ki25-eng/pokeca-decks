"""デッキの中身とカードの内容を貯めておく。

results.json (どのデッキがいつどこで勝ったか) とは別に、2つ持つ。

    decklists.json  デッキコード → 60枚の中身
    cards.json      カードID     → HP・ワザ・特性・効果テキスト

どちらも **一度取得したら内容が変わらない** 種類のデータなので、
貯めておけば取りに行くのは新しいぶんだけで済む。
そのため results.json のようなマージや期限切れ削除は要らない。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
POKECA_DIR = ROOT / "data" / "pokeca"
DECKLISTS_FILE = POKECA_DIR / "decklists.json"
CARDS_FILE = POKECA_DIR / "cards.json"


def _load(path: Path, key: str) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f).get(key, {})


def _save(path: Path, key: str, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(
            {"count": len(data), key: dict(sorted(data.items()))},
            f,
            ensure_ascii=False,
            indent=1,
        )
        f.write("\n")


def load_decklists() -> dict[str, dict]:
    """デッキコード → 中身。"""
    return _load(DECKLISTS_FILE, "decklists")


def save_decklists(decklists: dict[str, dict]) -> None:
    _save(DECKLISTS_FILE, "decklists", decklists)


def load_cards() -> dict[str, dict]:
    """カードID → カードの内容。"""
    return _load(CARDS_FILE, "cards")


def save_cards(cards: dict[str, dict]) -> None:
    _save(CARDS_FILE, "cards", cards)


def card_ids_in(decklists: dict[str, dict]) -> set[str]:
    """デッキに実際に入っているカードIDを集める。

    公式のカードは数千枚あるが、優勝デッキに出てくるのはその一部。
    ここで絞ることで、取りに行く枚数を必要最小限にする。
    """
    ids: set[str] = set()
    for deck in decklists.values():
        for card in deck.get("cards", []):
            if card.get("id"):
                ids.add(card["id"])
    return ids
