"""勝っているデッキの中身を見る道具のテスト。

数字が少しでもずれると、子どもに嘘を教えることになる。
採用率・分母・平均・確率は、手で確かめられる小さな例で固定しておく。
"""

from __future__ import annotations

import pytest

from src.pokeca import analysis
from src.pokeca.models import DeckResult


def make_result(code: str, name: str, *, rank: int = 1, event: str = "city") -> DeckResult:
    return DeckResult(
        date="2026-08-09",
        store="テスト店",
        rank=rank,
        deck_name=name,
        event_type=event,
        deck_code=code,
    )


def make_deck(cards: dict[str, int]) -> dict:
    """{カード名: 枚数} から、公式のデッキページと同じ形を作る。"""
    return {
        "cards": [
            {
                "id": str(1000 + i),
                "name": name,
                "set": "TST",
                "number": f"{i:03d}/100",
                "count": count,
                "section": "ポケモン",
                "image": "",
            }
            for i, (name, count) in enumerate(cards.items(), start=1)
        ],
        "total": sum(cards.values()),
        "sections": {"ポケモン": sum(cards.values())},
    }


# ドラパルト4件。マシマシラ3件・ナンジャモ2件・ネオラント1件。
# 「確定枠」「選択枠」「型」が手で数えられる大きさにしてある。
DRAPARUTO = {
    "d1": {"ドラパルトex": 3, "ドロンチ": 2, "マシマシラ": 1, "ナンジャモ": 2, "ネオラント": 1},
    "d2": {"ドラパルトex": 3, "ドロンチ": 3, "マシマシラ": 2, "ナンジャモ": 1},
    "d3": {"ドラパルトex": 4, "ドロンチ": 2, "マシマシラ": 1, "ボスの指令": 2},
    "d4": {"ドラパルトex": 3, "ドロンチ": 2, "ボスの指令": 3},
}
LUGIA = {"l1": {"ルギアVSTAR": 4, "アーケオス": 4, "マシマシラ": 1}}


@pytest.fixture(scope="module")
def corpus() -> list[analysis.DeckEntry]:
    results = [make_result(code, "ドラパルトex") for code in DRAPARUTO]
    results.append(make_result("l1", "ルギアVSTAR"))
    decklists = {code: make_deck(cards) for code, cards in {**DRAPARUTO, **LUGIA}.items()}
    return analysis.build_corpus(results, decklists)


@pytest.fixture(scope="module")
def draparuto(corpus) -> list[analysis.DeckEntry]:
    return analysis.select(corpus, deck_key="ドラパルトex")


# ------------------------------------------------------------------
# 突き合わせ
# ------------------------------------------------------------------


def test_corpus_joins_results_with_their_contents(corpus):
    assert len(corpus) == 5
    assert all(deck.counts for deck in corpus)


def test_corpus_skips_decks_without_contents():
    results = [make_result("missing", "ドラパルトex")]
    assert analysis.build_corpus(results, {}) == []


def test_corpus_skips_results_without_a_deck_name():
    """名前が無いと束ねられない。採用率の意味が無くなるので数えない。"""
    results = [make_result("d1", "")]
    assert analysis.build_corpus(results, {"d1": make_deck({"a": 60})}) == []


def test_select_filters_by_deck_and_event(corpus):
    assert len(analysis.select(corpus, deck_key="ドラパルトex")) == 4
    assert len(analysis.select(corpus, event_type="gym")) == 0


# ------------------------------------------------------------------
# 採用率 -- 同じデッキ名でも中身はどう違うのか
# ------------------------------------------------------------------


def test_adoption_counts_decks_not_copies(draparuto):
    """4件中3件に入っていれば 3件 / 75%。枚数の合計と混ぜない。"""
    rows = {row["name"]: row for row in analysis.adoption(draparuto)}
    assert rows["マシマシラ"]["decks"] == 3
    assert rows["マシマシラ"]["total"] == 4
    assert rows["マシマシラ"]["share"] == 0.75


def test_adoption_average_ignores_decks_without_the_card(draparuto):
    """平均枚数は「入れているデッキの中での平均」。1+2+1 の3件で 4/3。"""
    rows = {row["name"]: row for row in analysis.adoption(draparuto)}
    assert rows["マシマシラ"]["average"] == pytest.approx(4 / 3)


def test_adoption_reports_the_spread_of_copies(draparuto):
    """3枚が3件、4枚が1件。何枚積むかは作る人で割れる。"""
    rows = {row["name"]: row for row in analysis.adoption(draparuto)}
    assert rows["ドラパルトex"]["distribution"] == {3: 3, 4: 1}


def test_adoption_orders_by_how_many_decks_use_it(draparuto):
    rows = analysis.adoption(draparuto)
    assert rows[0]["name"] == "ドラパルトex"
    assert [r["decks"] for r in rows] == sorted(
        [r["decks"] for r in rows], reverse=True
    )


def test_adoption_of_nothing_is_empty():
    assert analysis.adoption([]) == []


def test_core_and_flex_splits_by_share(draparuto):
    groups = analysis.core_and_flex(analysis.adoption(draparuto))
    core = [r["name"] for r in groups["確定枠"]]
    flex = [r["name"] for r in groups["選択枠"]]
    assert "ドラパルトex" in core and "ドロンチ" in core
    assert "ネオラント" in flex      # 4件中1件
    assert "マシマシラ" not in flex   # 4件中3件なので「よく入る」
    assert "ナンジャモ" not in flex   # 4件中2件はちょうど境目で「よく入る」


# ------------------------------------------------------------------
# カードからデッキを引く
# ------------------------------------------------------------------


def test_card_index_lists_every_deck_using_a_card(corpus):
    index = analysis.card_index(corpus)
    assert index["マシマシラ"]["decks"] == 4  # ドラパルト3 + ルギア1
    assert index["マシマシラ"]["archetypes"] == {"ドラパルトex": 3, "ルギアVSTAR": 1}


def test_card_index_average_is_per_deck_that_uses_it(corpus):
    index = analysis.card_index(corpus)
    assert index["マシマシラ"]["average"] == pytest.approx(5 / 4)


def test_find_cards_matches_part_of_a_name(corpus):
    index = analysis.card_index(corpus)
    hits = analysis.find_cards(index, "ドラパルト")
    assert [name for name, _ in hits] == ["ドラパルトex"]


def test_find_cards_orders_by_popularity(corpus):
    index = analysis.card_index(corpus)
    # ドロンチ4件 / ナンジャモ2件 / ネオラント1件
    hits = analysis.find_cards(index, "ン")
    assert [name for name, _ in hits] == ["ドロンチ", "ナンジャモ", "ネオラント"]


def test_find_cards_of_empty_keyword_is_empty(corpus):
    assert analysis.find_cards(analysis.card_index(corpus), "  ") == []


# ------------------------------------------------------------------
# 型を見つける
# ------------------------------------------------------------------


def test_divisive_cards_ignore_cards_everyone_plays(draparuto):
    names = [r["name"] for r in analysis.divisive_cards(analysis.adoption(draparuto))]
    assert "ドラパルトex" not in names
    assert "ドロンチ" not in names
    assert "ナンジャモ" in names


def test_variants_group_decks_by_the_dividing_cards(draparuto):
    groups = analysis.variants(draparuto)
    assert groups
    assert all(g["total"] == 4 for g in groups)
    assert all(g["examples"] for g in groups)
    assert all(g["share"] == g["decks"] / 4 for g in groups)


def test_variants_do_not_call_a_single_deck_a_type(draparuto):
    """1件しかない組み合わせを「型」と呼ぶと、みんなそうしていると誤解させる。"""
    groups = analysis.variants(draparuto)
    assert all(g["decks"] >= 2 for g in groups)


def test_variant_coverage_counts_what_did_not_fit(draparuto):
    groups = analysis.variants(draparuto)
    covered = sum(g["decks"] for g in groups)
    assert analysis.variant_coverage(draparuto, groups) == 4 - covered


def test_variants_need_more_than_one_deck():
    corpus = analysis.build_corpus(
        [make_result("d1", "ドラパルトex")], {"d1": make_deck({"a": 60})}
    )
    assert analysis.variants(corpus) == []


def test_variants_of_identical_decks_is_empty():
    """全部同じ中身なら分かれ目が無い。無理に型を作らない。"""
    same = {f"d{i}": {"ドラパルトex": 4, "ドロンチ": 3} for i in range(4)}
    corpus = analysis.build_corpus(
        [make_result(code, "ドラパルトex") for code in same],
        {code: make_deck(cards) for code, cards in same.items()},
    )
    assert analysis.variants(corpus) == []


# ------------------------------------------------------------------
# 安定度の計算 -- 「回らない」に数字で答える
# ------------------------------------------------------------------


def test_draw_probability_of_four_copies():
    """4枚積みが初手7枚に来る確率は約39.9%。よく知られた値。"""
    assert analysis.draw_probability(4) == pytest.approx(0.399, abs=0.001)


def test_draw_probability_grows_with_copies():
    values = [analysis.draw_probability(n) for n in (1, 2, 3, 4)]
    assert values == sorted(values)


def test_draw_probability_of_none_is_zero():
    assert analysis.draw_probability(0) == 0.0


def test_mulligan_rate_of_a_typical_deck():
    """たね10枚なら、最初に1匹も出せない確率は約25.9%。4回に1回は事故る。"""
    assert analysis.mulligan_rate(10) == pytest.approx(0.259, abs=0.001)


def test_mulligan_rate_gets_worse_with_fewer_basics():
    assert analysis.mulligan_rate(6) > analysis.mulligan_rate(12)


def test_mulligan_rate_without_any_basic_is_certain():
    assert analysis.mulligan_rate(0) == 1.0


def test_count_basics_uses_card_text():
    deck = analysis.DeckEntry(
        deck_code="d1", deck_name="x", deck_key="x", date="", event_type="city", rank=1,
        counts={"ホシガリス": 4, "ヨクバリス": 3},
        card_ids={"ホシガリス": "1", "ヨクバリス": "2"},
    )
    cards = {"1": {"stage": "たね"}, "2": {"stage": "1進化"}}
    assert analysis.count_basics(deck, cards) == 4


def test_count_basics_ignores_cards_not_fetched_yet():
    deck = analysis.DeckEntry(
        deck_code="d1", deck_name="x", deck_key="x", date="", event_type="city", rank=1,
        counts={"ホシガリス": 4}, card_ids={"ホシガリス": "1"},
    )
    assert analysis.count_basics(deck, {}) == 0


# ------------------------------------------------------------------
# 自分のデッキと見比べる
# ------------------------------------------------------------------


def test_compare_finds_cards_the_winners_play_and_you_do_not(draparuto):
    mine = {"ドラパルトex": 3, "ドロンチ": 2}
    report = analysis.compare(mine, draparuto)
    assert report["total"] == 4
    assert "マシマシラ" in [row["name"] for row in report["missing"]]


def test_compare_ignores_rare_cards_when_listing_what_is_missing(draparuto):
    """4件中1件しか入れていないカードは「足りない」とは言わない。"""
    mine = {"ドラパルトex": 3, "ドロンチ": 2, "マシマシラ": 1, "ナンジャモ": 2, "ボスの指令": 2}
    missing = [row["name"] for row in analysis.compare(mine, draparuto)["missing"]]
    assert "ネオラント" not in missing


def test_compare_lists_cards_only_you_play(draparuto):
    mine = {"ドラパルトex": 3, "ぼくのオリジナルカード": 1}
    extra = [row["name"] for row in analysis.compare(mine, draparuto)["extra"]]
    assert extra == ["ぼくのオリジナルカード"]


def test_compare_flags_a_clearly_different_number_of_copies(draparuto):
    """ドラパルトexは平均3.25枚。1枚だけなら差として出す。"""
    report = analysis.compare({"ドラパルトex": 1}, draparuto)
    different = {row["name"]: row for row in report["different"]}
    assert different["ドラパルトex"]["yours"] == 1
    assert different["ドラパルトex"]["average"] == pytest.approx(3.25)


def test_compare_stays_quiet_when_the_numbers_match(draparuto):
    report = analysis.compare({"ドラパルトex": 3}, draparuto)
    assert [row["name"] for row in report["different"]] == []
