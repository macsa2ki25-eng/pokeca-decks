"""デッキの中身とカードの内容のパーサーのテスト。

fixtures は実際に保存した公式サイトのページからの抜粋で、構造は実物のまま。

  official_decklist.html  deck/result.html/deckID/yyMppy-jUiB2j-pyXXRU
  official_card.html      card-search/details.php/card/50452/
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.pokeca.cardstore import card_ids_in
from src.pokeca.sources import official_card, official_deck

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def decklist() -> dict:
    return official_deck.parse_decklist(
        (FIXTURES / "official_decklist.html").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def card() -> dict:
    return official_card.parse_card(
        (FIXTURES / "official_card.html").read_text(encoding="utf-8"), "50452"
    )


# ------------------------------------------------------------------
# デッキの中身
# ------------------------------------------------------------------


def test_decklist_totals_exactly_sixty(decklist):
    """60枚ちょうどでなければ取りこぼしがある。集計の前提。"""
    assert decklist["total"] == 60


def test_decklist_section_headers_match_the_cards(decklist):
    """見出しの「ポケモン (19)」と、実際に拾えた枚数が一致すること。"""
    for section, declared in decklist["sections"].items():
        counted = sum(
            c["count"] for c in decklist["cards"] if c["section"] == section
        )
        assert counted == declared, f"{section}: 見出し{declared} / 実際{counted}"


def test_decklist_extracts_card_identity(decklist):
    first = next(c for c in decklist["cards"] if c["name"] == "メガサーナイトex")
    assert first["id"] == "47838"
    assert first["count"] == 3
    assert first["set"] == "M1S"
    assert first["number"] == "042/063"
    assert first["section"] == "ポケモン"


def test_decklist_covers_every_section(decklist):
    assert set(decklist["sections"]) >= {
        "ポケモン", "グッズ", "サポート", "スタジアム", "エネルギー"
    }


def test_decklist_finds_a_single_copy_card(decklist):
    """1枚積みのカードも取りこぼさない (採用率の分母に効く)。"""
    mashimashira = next(c for c in decklist["cards"] if c["name"] == "マシマシラ")
    assert mashimashira["count"] == 1


def test_card_ids_are_collected_for_fetching(decklist):
    ids = card_ids_in({"yyMppy-jUiB2j-pyXXRU": decklist})
    assert "47838" in ids
    assert len(ids) == len(decklist["cards"])


def test_parse_decklist_survives_an_unrelated_page():
    assert official_deck.parse_decklist("<html><body>なにもない</body></html>")["total"] == 0


def test_deck_url_is_built_from_the_code():
    assert official_deck.deck_url("abc-def-ghi").endswith("/deckID/abc-def-ghi/")


# ------------------------------------------------------------------
# カードの内容
# ------------------------------------------------------------------


def test_card_basic_stats(card):
    assert card["name"] == "シェイミ"
    assert card["stage"] == "たね"
    assert card["hp"] == 80


def test_card_attack_has_cost_damage_and_effect(card):
    """火力の話に必要なのは、必要エネルギー・ダメージ・効果の3つ。"""
    attack = card["attacks"][0]
    assert attack["name"] == "かけぬける"
    assert attack["cost"] == ["草"]
    assert attack["damage"] == "20"
    assert "ベンチポケモン" in attack["effect"]


def test_card_weakness_and_retreat(card):
    """タイプ/エネルギーはアイコンのクラス名から読む。"""
    assert card["weakness"] == "炎×2"
    assert card["retreat"] == 1


def test_card_url_is_built_from_the_id():
    assert official_card.card_url(50452).endswith("/card/50452/")


def test_parse_card_survives_an_unrelated_page():
    card = official_card.parse_card("<html><title>x</title><body>y</body></html>")
    assert card["attacks"] == []
    assert card["abilities"] == []


# ------------------------------------------------------------------
# 失敗理由を握りつぶさない
# ------------------------------------------------------------------


def test_fetch_decklist_reports_why_it_failed(monkeypatch):
    """全滅したときにログへ理由が出ること。

    以前は例外を握りつぶして None を返していたため、本番のログに
    「失敗 200」としか出ず、原因が分からないまま104分走り続けた。
    """
    monkeypatch.setattr(
        official_deck.http, "get_text", lambda url: "<html>中身なし</html>"
    )
    deck, reason = official_deck.fetch_decklist("abc-def-ghi")
    assert deck is None
    assert "0枚しか取れず" in reason


def test_fetch_decklist_reports_network_errors(monkeypatch):
    def boom(url):
        raise RuntimeError("403 Client Error: Forbidden")

    monkeypatch.setattr(official_deck.http, "get_text", boom)
    deck, reason = official_deck.fetch_decklist("abc-def-ghi")
    assert deck is None
    assert "403" in reason


def test_fetch_decklist_succeeds_with_a_full_deck(monkeypatch):
    html = (FIXTURES / "official_decklist.html").read_text(encoding="utf-8")
    monkeypatch.setattr(official_deck.http, "get_text", lambda url: html)
    deck, reason = official_deck.fetch_decklist("yyMppy-jUiB2j-pyXXRU")
    assert reason == ""
    assert deck["total"] == 60
    assert deck["deck_code"] == "yyMppy-jUiB2j-pyXXRU"


def test_fetch_card_reports_why_it_failed(monkeypatch):
    monkeypatch.setattr(
        official_card.http, "get_text", lambda url: "<html><body>x</body></html>"
    )
    card, reason = official_card.fetch_card("50452")
    assert card is None
    assert "カード名が取れず" in reason
