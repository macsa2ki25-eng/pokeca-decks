"""デッキの中身とカードの内容を貯めておく。

results.json (どのデッキがいつどこで勝ったか) とは別に、2つ持つ。

    decklists.json  デッキコード → [カードID, 枚数] の並び
    cards.json      カードID     → 名前・収録セット・区分・画像
                                   + HP・ワザ・特性・効果テキスト

どちらも **一度取得したら内容が変わらない** 種類のデータなので、
貯めておけば取りに行くのは新しいぶんだけで済む。
そのため results.json のようなマージや期限切れ削除は要らない。

## カードの情報はデッキ側に持たない

公式のデッキページからは、カード名・収録セット・番号・区分・画像が
一緒に取れる。だがそれらは **カードIDが決まれば決まる** 情報なので、
そのカードが入っているデッキの数だけ書き写すと、同じ内容が何度も並ぶ。

実際、4150デッキ・カード行10万件に対して、カードの種類は2151だった。
1枚のカードの情報を平均49回書いていた計算で、ファイルは26MBあった。
IDと枚数だけにすれば 1.6MB で足りる。

そこで **カードの情報は cards.json に1つだけ持ち、デッキ側は
「どのIDが何枚か」だけを持つ**。表示するときは :func:`expand` で
組み立て直す。

## cards.json の "detail"

cards.json には2つの経路から情報が入る。

    デッキページ経由  名前・収録セット・番号・区分・画像
    カード詳細ページ  HP・ワザ・特性・効果テキスト        → "detail": true

``detail`` が付いていないカードは、名前は分かっているが中身をまだ
取っていない。:func:`needs_detail` がそれを拾う。この印が無いと
「名前だけ入っているカード」を取得済みと誤認して、ワザのテキストが
永久に集まらなくなる。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
POKECA_DIR = ROOT / "data" / "pokeca"
DECKLISTS_FILE = POKECA_DIR / "decklists.json"
CARDS_FILE = POKECA_DIR / "cards.json"

# デッキページから分かる、カードそのものの情報
CARD_FACTS = ("name", "set", "number", "section", "image")


def _load(path: Path, key: str) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f).get(key, {})


def _save(path: Path, key: str, data: dict, *, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(
            {"count": len(data), key: dict(sorted(data.items()))},
            f,
            ensure_ascii=False,
            indent=indent,
        )
        f.write("\n")


# ------------------------------------------------------------------
# デッキの中身
# ------------------------------------------------------------------


def slim(deck: dict) -> dict:
    """パーサーが返した中身を、保存する形 (IDと枚数だけ) にする。

    すでに保存形になっているものはそのまま通すので、二度通しても安全。
    """
    cards = deck.get("cards", [])
    if cards and isinstance(cards[0], dict):
        pairs = [[c.get("id", ""), c.get("count", 0)] for c in cards]
    else:
        pairs = [[str(c[0]), int(c[1])] for c in cards]
    out: dict = {"cards": pairs, "total": deck.get("total", sum(p[1] for p in pairs))}
    if deck.get("sections"):
        out["sections"] = deck["sections"]
    return out


def card_facts(deck: dict) -> dict[str, dict]:
    """デッキの中身から、カードそのものの情報だけを抜き出す。"""
    facts: dict[str, dict] = {}
    for card in deck.get("cards", []):
        if not isinstance(card, dict) or not card.get("id"):
            continue
        facts[card["id"]] = {k: card[k] for k in CARD_FACTS if card.get(k)}
    return facts


def expand(deck: dict, cards: dict[str, dict]) -> list[dict]:
    """保存形のデッキを、表示できる形に組み立て直す。

    カード表に無いIDでも枚数は返す。60枚の合計を崩さないため。
    """
    out = []
    for card_id, count in deck.get("cards", []):
        info = cards.get(str(card_id), {})
        out.append(
            {
                "id": str(card_id),
                "count": count,
                **{k: info.get(k, "") for k in CARD_FACTS},
            }
        )
    return out


def load_decklists() -> dict[str, dict]:
    """デッキコード → 中身 (IDと枚数)。

    古い形式 (カードの情報を丸ごと持っていたもの) で保存されていても、
    読んだ時点で保存形に直して返す。
    """
    return {
        code: slim(deck) for code, deck in _load(DECKLISTS_FILE, "decklists").items()
    }


def save_decklists(decklists: dict[str, dict]) -> None:
    """デッキを保存する。カードの情報は cards.json に寄せる。"""
    facts: dict[str, dict] = {}
    for deck in decklists.values():
        facts.update(card_facts(deck))
    if facts:
        merge_cards(facts)
    # 1デッキ1行に収める。10万行の縦長ファイルにしても人が読めないので。
    _save(
        DECKLISTS_FILE,
        "decklists",
        {code: slim(deck) for code, deck in decklists.items()},
        indent=None,
    )


# ------------------------------------------------------------------
# カードの内容
# ------------------------------------------------------------------


def load_cards() -> dict[str, dict]:
    """カードID → カードの内容。"""
    return _load(CARDS_FILE, "cards")


def save_cards(cards: dict[str, dict]) -> None:
    _save(CARDS_FILE, "cards", cards)


def merge_cards(new: dict[str, dict]) -> dict[str, dict]:
    """カード表に情報を足す。すでに入っている値は上書きしない。

    デッキページ経由の情報とカード詳細ページ経由の情報が、
    同じカードの1件にまとまるようにする。
    """
    cards = load_cards()
    for card_id, info in new.items():
        entry = cards.setdefault(str(card_id), {})
        for key, value in info.items():
            if value not in ("", None, [], {}) and not entry.get(key):
                entry[key] = value
    save_cards(cards)
    return cards


def card_ids_in(decklists: dict[str, dict]) -> set[str]:
    """デッキに実際に入っているカードIDを集める。

    公式のカードは数千枚あるが、優勝デッキに出てくるのはその一部。
    ここで絞ることで、取りに行く枚数を必要最小限にする。
    """
    ids: set[str] = set()
    for deck in decklists.values():
        for card in deck.get("cards", []):
            card_id = card.get("id") if isinstance(card, dict) else card[0]
            if card_id:
                ids.add(str(card_id))
    return ids


def needs_detail(decklists: dict[str, dict], cards: dict[str, dict]) -> list[str]:
    """ワザや特性をまだ取っていないカードIDを、若い順に返す。

    名前だけ入っているカードを「取得済み」と数えてしまうと、
    ワザのテキストが永久に集まらない。``detail`` の印で見分ける。
    """
    return sorted(
        card_id
        for card_id in card_ids_in(decklists)
        if not cards.get(card_id, {}).get("detail")
    )
