"""子ども向けページに埋め込むデータのテスト。

ページの見た目そのものは Chromium で実際に開いて確かめている。
ここで固定するのは「何を出して、何を出さないか」の判断のほう。
"""

from __future__ import annotations

from src.pokeca import site
from src.pokeca.models import DeckResult

CARDS = {
    "1": {"name": "ドラメシヤ", "image": site.CARD_IMAGE_PREFIX + "SV6/001_A.jpg"},
    "2": {"name": "ドロンチ", "image": site.CARD_IMAGE_PREFIX + "SV6/002_B.jpg"},
    "3": {"name": "マシマシラ", "image": ""},
    "4": {"name": "ナンジャモ"},
    "9": {"name": "ルギアVSTAR"},
}


def result(code: str, name: str) -> DeckResult:
    return DeckResult(
        date="2026-08-09", store="てすと", rank=1, deck_name=name,
        event_type="city", deck_code=code,
    )


def deck(pairs: list[tuple[str, int]]) -> dict:
    return {"cards": [[i, n] for i, n in pairs], "total": sum(n for _, n in pairs)}


# ドラパルト6件。マシマシラは4件、ナンジャモは2件にだけ入っている。
DRAPARUTO = {
    "d1": [("1", 4), ("2", 4), ("3", 1), ("4", 2)],
    "d2": [("1", 4), ("2", 4), ("3", 2), ("4", 1)],
    "d3": [("1", 4), ("2", 3), ("3", 1)],
    "d4": [("1", 4), ("2", 4), ("3", 1)],
    "d5": [("1", 3), ("2", 4)],
    "d6": [("1", 4), ("2", 4)],
}


def build(extra_results=(), extra_decks=None):
    results = [result(code, "ドラパルトex") for code in DRAPARUTO]
    results.extend(extra_results)
    decklists = {code: deck(pairs) for code, pairs in DRAPARUTO.items()}
    decklists.update(extra_decks or {})
    return site.build_data(results, decklists=decklists, cards=CARDS)


def test_contents_are_built_for_a_deck_with_enough_wins():
    data = build()
    inside = data["contents"]["ドラパルトex"]
    assert inside["decks"] == 6
    assert inside["name"] == "ドラパルトex"


def test_contents_split_cards_by_how_often_they_are_used():
    labels = {g["label"]: g for g in build()["contents"]["ドラパルトex"]["groups"]}
    assert "かならず入っている" in labels
    core = [c["name"] for c in labels["かならず入っている"]["cards"]]
    assert "ドラメシヤ" in core and "ドロンチ" in core

    rest = [c["name"] for g in labels.values() for c in g["cards"]]
    assert "ナンジャモ" in rest   # 6件中2件でも消さない


def test_each_card_says_how_many_copies_people_actually_play():
    """平均より「一番多い枚数」。デッキを組むときはそのまま使えるほう。"""
    groups = build()["contents"]["ドラパルトex"]["groups"]
    dorameshiya = next(
        c for g in groups for c in g["cards"] if c["name"] == "ドラメシヤ"
    )
    assert dorameshiya["n"] == 4        # 4枚が5件、3枚が1件
    assert dorameshiya["decks"] == 6
    assert dorameshiya["total"] == 6


def test_card_images_drop_the_shared_prefix():
    """URLの共通部分は1回だけ持つ。1枚ごとに持つとページが重くなる。"""
    groups = build()["contents"]["ドラパルトex"]["groups"]
    row = next(c for g in groups for c in g["cards"] if c["name"] == "ドラメシヤ")
    assert row["img"] == "SV6/001_A.jpg"
    assert data_base_joins(row["img"]) == (
        "https://www.pokemon-card.com/assets/images/card_images/large/SV6/001_A.jpg"
    )


def data_base_joins(path: str) -> str:
    return site.CARD_IMAGE_BASE + path


def test_cards_without_a_picture_are_still_listed():
    groups = build()["contents"]["ドラパルトex"]["groups"]
    row = next(c for g in groups for c in g["cards"] if c["name"] == "マシマシラ")
    assert row["img"] == ""
    assert row["decks"] == 4


def test_a_deck_with_too_few_wins_gets_no_contents():
    """2件しかないデッキの「採用率100%」は、ただの偶然でしかない。"""
    data = build(
        extra_results=[result("x1", "ルギアVSTAR")],
        extra_decks={"x1": deck([("9", 4)])},
    )
    assert "ルギアVSTAR" not in data["contents"]


def test_card_index_only_covers_cards_shown_on_the_page():
    """出していないカードの索引を埋め込むと、読み込むだけ無駄になる。"""
    data = build()
    shown = {
        c["name"]
        for inside in data["contents"].values()
        for g in inside["groups"]
        for c in g["cards"]
    }
    assert set(data["cardDecks"]) == shown


def test_card_index_says_which_decks_use_the_card():
    entry = build()["cardDecks"]["マシマシラ"]
    assert entry["decks"] == 4
    assert entry["top"][0] == ["ドラパルトex", 4]


def test_no_contents_without_deck_data():
    """中身をまだ取っていなくても、これまでどおりページは作れること。"""
    data = site.build_data([result("d1", "ドラパルトex")])
    assert data["contents"] == {}
    assert data["cardDecks"] == {}
    assert data["results"]


def test_page_hides_the_inside_button_when_there_is_nothing_to_show():
    html = site.build_html(site.build_data([result("d1", "ドラパルトex")]))
    assert '"contents": {}' in html or '"contents":{}' in html


def test_page_embeds_the_contents_when_they_exist():
    html = site.build_html(build())
    assert "かならず入っている" in html
    assert "ドラメシヤ" in html
