"""デッキの中身とカードの内容のパーサーのテスト。

fixtures は実際に保存した公式サイトのページからの抜粋で、構造は実物のまま。

  official_deck_raw.html  deck/result.html/deckID/ngHLgL-pA4GKN-9gniQn
                          サーバーが素で返すHTML。本番はこちらを読む
  official_decklist.html  deck/result.html/deckID/yyMppy-jUiB2j-pyXXRU
                          ブラウザで保存した JavaScript 実行後のDOM
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
def raw_decklist() -> dict:
    return official_deck.parse_decklist(
        (FIXTURES / "official_deck_raw.html").read_text(encoding="utf-8")
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
# 素のHTML (hidden 入力欄) から読む — 本番の経路
#
# 公式のカード表は JavaScript が組み立てているので、サーバーが返すHTMLに
# 表は無い。中身は hidden の入力欄に入っている。
# ------------------------------------------------------------------


def test_raw_decklist_totals_exactly_sixty(raw_decklist):
    """JavaScript を動かさずに60枚そろうこと。ここが崩れると全部崩れる。"""
    assert raw_decklist["total"] == 60


def test_raw_decklist_has_every_distinct_card(raw_decklist):
    """種類数は、同じHTMLに並ぶカード名の件数 (実物で27) と一致する。"""
    assert len(raw_decklist["cards"]) == 27


def test_raw_decklist_reads_counts_per_section(raw_decklist):
    assert raw_decklist["sections"]["ポケモン"] == 16
    assert raw_decklist["sections"]["グッズ"] == 17
    assert raw_decklist["sections"]["サポート"] == 9
    assert raw_decklist["sections"]["スタジアム"] == 2
    assert raw_decklist["sections"]["エネルギー"] == 16


def test_raw_decklist_extracts_card_identity(raw_decklist):
    """47847_2_1 と searchItemName を突き合わせて1枚ぶんに組み立てる。"""
    kangaskhan = next(c for c in raw_decklist["cards"] if c["id"] == "47847")
    assert kangaskhan["name"] == "メガガルーラex"
    assert kangaskhan["count"] == 2
    assert kangaskhan["set"] == "M1S"
    assert kangaskhan["number"] == "051/063"
    assert kangaskhan["section"] == "ポケモン"
    assert kangaskhan["image"].endswith(".jpg")


def test_raw_decklist_keeps_names_with_spaces_intact(raw_decklist):
    """「オーガポン みどりのめんex」のように空白を含む名前を切らない。"""
    ogerpon = next(c for c in raw_decklist["cards"] if c["id"] == "45707")
    assert ogerpon["name"] == "オーガポン みどりのめんex"
    assert ogerpon["number"] == "006/101"


def test_raw_decklist_keeps_cards_without_a_known_name(raw_decklist):
    """名前が引けなくても枚数は正しい。60枚の合計を崩さないため。"""
    unnamed = [c for c in raw_decklist["cards"] if not c["name"]]
    assert unnamed, "名前の無いカードが1枚も無いなら、この試験は意味を持たない"
    assert all(c["count"] > 0 for c in unnamed)


def test_raw_decklist_uses_no_unconfirmed_section(raw_decklist):
    """deck_tech / deck_ajs に中身が入った実物はまだ見ていない。

    入っていたら区分名を決めつけずに気付けるようにしてある。
    """
    assert official_deck.unconfirmed_sections(raw_decklist) == []


def test_unconfirmed_sections_are_reported_when_used():
    html = '<input type="hidden" name="deck_tech" value="12345_1_1">'
    assert official_deck.unconfirmed_sections(
        official_deck.parse_decklist(html)
    ) == ["その他(tech)"]


def test_saved_browser_page_still_parses(decklist, raw_decklist):
    """素のHTMLを優先しつつ、保存したページ (表) も読めること。"""
    assert decklist["total"] == raw_decklist["total"] == 60


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


# ------------------------------------------------------------------
# 保存の形 -- カードの情報はデッキ側に持たない
#
# 同じカードの情報を、そのカードが入っているデッキの数だけ書き写して
# いたため decklists.json が26MBあった。カードの種類は2151しかなく、
# 1枚あたり平均49回書いている計算だった。
# ------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    """保存先を一時ディレクトリに向ける。"""
    from src.pokeca import cardstore

    monkeypatch.setattr(cardstore, "DECKLISTS_FILE", tmp_path / "decklists.json")
    monkeypatch.setattr(cardstore, "CARDS_FILE", tmp_path / "cards.json")
    return cardstore


def test_saved_deck_keeps_only_ids_and_counts(store, raw_decklist):
    store.save_decklists({"abc": raw_decklist})
    saved = store.load_decklists()["abc"]
    assert saved["total"] == 60
    assert saved["cards"][0] == ["47847", 2]
    assert all(len(pair) == 2 for pair in saved["cards"])


def test_saving_a_deck_files_the_card_information_once(store, raw_decklist):
    store.save_decklists({"abc": raw_decklist, "def": raw_decklist})
    cards = store.load_cards()
    assert cards["47847"]["name"] == "メガガルーラex"
    assert cards["47847"]["set"] == "M1S"
    assert cards["47847"]["section"] == "ポケモン"
    # 2デッキ保存しても、カードの情報は1件だけ
    assert len(cards) == 27


def test_saved_deck_is_far_smaller_than_the_parsed_one(store, raw_decklist):
    import json

    store.save_decklists({"abc": raw_decklist})
    fat = len(json.dumps({"abc": raw_decklist}, ensure_ascii=False))
    thin = store.DECKLISTS_FILE.stat().st_size
    assert thin < fat / 2


def test_expand_rebuilds_the_readable_form(store, raw_decklist):
    store.save_decklists({"abc": raw_decklist})
    rebuilt = store.expand(store.load_decklists()["abc"], store.load_cards())
    assert sum(c["count"] for c in rebuilt) == 60
    first = rebuilt[0]
    assert first["name"] == "メガガルーラex"
    assert first["count"] == 2


def test_expand_keeps_counts_for_cards_we_have_no_name_for(store):
    deck = {"cards": [["99999", 4]], "total": 4}
    rebuilt = store.expand(deck, {})
    assert rebuilt == [
        {"id": "99999", "count": 4, "name": "", "set": "", "number": "",
         "section": "", "image": ""}
    ]


def test_old_format_is_read_back_in_the_new_shape(store, raw_decklist):
    """カードの情報を丸ごと持っていた古い保存も、そのまま読めること。"""
    import json

    store.DECKLISTS_FILE.write_text(
        json.dumps({"count": 1, "decklists": {"abc": raw_decklist}}, ensure_ascii=False),
        encoding="utf-8",
    )
    saved = store.load_decklists()["abc"]
    assert saved["cards"][0] == ["47847", 2]
    assert saved["total"] == 60


def test_merge_cards_does_not_overwrite_what_we_already_know(store):
    store.save_cards({"1": {"name": "シェイミ", "hp": 80, "detail": True}})
    store.merge_cards({"1": {"name": "ちがう名前", "set": "SV6"}})
    card = store.load_cards()["1"]
    assert card["name"] == "シェイミ"   # 既にあるものは守る
    assert card["set"] == "SV6"        # 無かったものは足す
    assert card["hp"] == 80


def test_card_ids_are_collected_from_the_saved_shape(store, raw_decklist):
    store.save_decklists({"abc": raw_decklist})
    ids = store.card_ids_in(store.load_decklists())
    assert "47847" in ids
    assert len(ids) == 27


def test_needs_detail_ignores_cards_that_only_have_a_name(store, raw_decklist):
    """名前だけ入っているカードを取得済みと数えると、ワザが永久に集まらない。"""
    store.save_decklists({"abc": raw_decklist})
    decklists = store.load_decklists()
    todo = store.needs_detail(decklists, store.load_cards())
    assert len(todo) == 27   # 名前はあるが、中身はまだ

    store.merge_cards({"47847": {"detail": True}})
    todo = store.needs_detail(decklists, store.load_cards())
    assert "47847" not in todo
    assert len(todo) == 26


def test_japanese_energy_names_for_every_icon():
    """公式(日本)は electric / dark / steel を使う。英語版の呼び方とは別。

    ここが欠けると「弱点 electric×2」のまま残り、子どもには読めず、
    弱点の計算もできない。実データで690箇所やられた。
    """
    for name in ("electric", "dark", "steel", "lightning", "darkness", "metal"):
        assert official_card.ICON_NAMES[name] in "雷悪鋼"

    html = (
        '<div class="RightBox"><div class="TopInfo">'
        '<span class="hp-type"><span class="icon-electric icon"></span></span></div>'
        '<table><tr><th>弱点</th><th>抵抗力</th><th>にげる</th></tr>'
        '<tr><td><span class="icon-dark icon"></span>×2</td>'
        '<td><span class="icon-steel icon"></span>-30</td>'
        '<td class="escape"><span class="icon-none icon"></span></td></tr></table></div>'
    )
    card = official_card.parse_card(html, "1")
    assert card["type"] == "雷"
    assert card["weakness"] == "悪×2"
    assert card["resistance"] == "鋼-30"


def test_effect_text_keeps_energy_symbols():
    """効果文のエネルギーはマークで書かれている。落とすと意味が変わる。

        ○ ついている炎と雷エネルギーの数×50
        × ついている と エネルギーの数×50

    これでメガレックウザexのワザを読み違えかけた。
    """
    html = (
        '<div class="RightBox"><h2>ワザ</h2>'
        '<h4><span class="icon-fire icon"></span><span class="icon-electric icon"></span>'
        'テストワザ<span class="f_right">50×</span></h4>'
        '<p>自分のポケモン全員についている<span class="icon-fire icon"></span>と'
        '<span class="icon-electric icon"></span>エネルギーの数×50ダメージ。</p></div>'
    )
    attack = official_card.parse_card(html, "1")["attacks"][0]
    assert attack["cost"] == ["炎", "雷"]
    assert "炎と雷エネルギーの数" in attack["effect"].replace(" ", "")
