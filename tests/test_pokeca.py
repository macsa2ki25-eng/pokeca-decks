"""pokeca モジュールのテスト。

``tests/fixtures/pokecabook_city_league.html`` は実際に保存した
pokecabook.com/archives/320777 から先頭2店舗ぶんを抜き出したもの。
img の srcset など容量だけ食う属性を削っただけで、構造は実物のまま。

収集元の構造が変わってパーサーを直すときは、まず
``python -m src.pokeca.cli inspect --source pokecabook`` で新しい実物を保存し、
fixture を差し替えてからコードを直すこと。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from src.pokeca import aggregate
from src.pokeca.models import (
    EVENT_CITY,
    EVENT_GYM,
    DeckResult,
    normalize_deck_name,
    normalize_store,
)
from src.pokeca.sources import deckindex, official, pokecabook, wp
from src.pokeca.store import merge_results

FIXTURE = Path(__file__).parent / "fixtures" / "pokecabook_city_league.html"
POST_URL = "https://pokecabook.com/archives/320777"
POST_TITLE = "シティリーグ5/6【水】ベスト16デッキまとめ"
PUBLISHED = date(2026, 5, 6)


@pytest.fixture(scope="module")
def records() -> list[DeckResult]:
    html = FIXTURE.read_text(encoding="utf-8")
    return pokecabook.parse_post(html, POST_TITLE, POST_URL, PUBLISHED)


# ------------------------------------------------------------------
# 正規化
# ------------------------------------------------------------------


def test_deck_name_normalization_absorbs_variants():
    assert normalize_deck_name("ドラパルトex") == normalize_deck_name("ドラパルトｅｘ")
    assert normalize_deck_name("ドラパルトex デッキ") == normalize_deck_name("ドラパルトex")
    assert normalize_deck_name("リザードンex") != normalize_deck_name("ドラパルトex")


def test_store_normalization_ignores_spacing():
    assert normalize_store("バトロコ 高田馬場") == normalize_store("バトロコ　高田馬場")


@pytest.mark.parametrize(
    "heading,expected",
    [
        ("宝島　岐阜本店（岐阜）", ("宝島 岐阜本店", "岐阜")),
        ("Super KaBoS + GEO 鯖江店（福井）", ("Super KaBoS + GEO 鯖江店", "福井")),
        ("BOOKOFFPLUS　アミューあつぎ店（神奈川）", ("BOOKOFFPLUS アミューあつぎ店", "神奈川")),
        ("バトロコミニ苫小牧バイパス店（北海道）", ("バトロコミニ苫小牧バイパス店", "北海道")),
        ("店舗名だけで括弧なし", ("店舗名だけで括弧なし", "")),
    ],
)
def test_split_store(heading, expected):
    assert pokecabook.split_store(heading) == expected


# ------------------------------------------------------------------
# ポケカブックのパーサー (実物HTMLに対して)
# ------------------------------------------------------------------


def test_parses_only_first_and_second_place(records):
    """TOP4 / TOP8 / TOP16 は拾わない。2店舗ぶん = 4件。"""
    assert len(records) == 4
    assert sorted(r.rank for r in records) == [1, 1, 2, 2]


def test_extracts_store_and_prefecture(records):
    winners = {r.store: r for r in records if r.rank == 1}
    assert set(winners) == {"宝島 岐阜本店", "ゲームアーク 丸亀店"}
    assert winners["宝島 岐阜本店"].prefecture == "岐阜"
    assert winners["ゲームアーク 丸亀店"].prefecture == "香川"


def test_extracts_official_deck_code(records):
    """デッキコードが取れることが最重要。実物の60枚レシピへの導線になる。"""
    first = next(r for r in records if r.rank == 1 and r.store == "宝島 岐阜本店")
    assert first.deck_code == "8YGKY8-wTd9K2-8Dacc4"
    assert first.deck_code_url == (
        "https://www.pokemon-card.com/deck/confirm.html/deckID/8YGKY8-wTd9K2-8Dacc4/"
    )
    assert all(r.deck_code for r in records)


def test_links_back_to_the_store_section_of_the_article(records):
    """<span id="tocN"> を使って元記事の該当店舗へ直接飛べる。"""
    first = next(r for r in records if r.store == "宝島 岐阜本店")
    second = next(r for r in records if r.store == "ゲームアーク 丸亀店")
    assert first.source_url == f"{POST_URL}#toc1"
    assert second.source_url == f"{POST_URL}#toc2"


def test_captures_official_event_url(records):
    """リーグ区分を後から補うために公式イベントURLを持っておく。"""
    first = next(r for r in records if r.store == "宝島 岐阜本店")
    assert first.event_url == "https://players.pokemon-card.com/event/detail/953108/result"


def test_deck_name_is_absent_in_this_source(records):
    """この記事にデッキ名は書かれていない (デッキは画像で示されている)。

    名前は別の情報源から埋める前提。ここが空であること自体が仕様。
    """
    assert all(r.deck_name == "" for r in records)


def test_date_comes_from_the_title(records):
    assert all(r.date == "2026-05-06" for r in records)


def test_each_record_is_a_distinct_slot(records):
    assert len({r.slot_id for r in records}) == len(records)


def test_extract_date_handles_year_boundary():
    """1月公開の記事に 12/28 とあれば前年の開催とみなす。"""
    published = date(2026, 1, 5)
    assert pokecabook.extract_date_from_title("シティリーグ12/28【日】", published) == "2025-12-28"
    assert pokecabook.extract_date_from_title("シティリーグ1/4【日】", published) == "2026-01-04"


def test_parse_post_survives_empty_content():
    assert pokecabook.parse_post("", POST_TITLE, POST_URL, PUBLISHED) == []


# ------------------------------------------------------------------
# デッキ別ページのパーサー (実物HTMLに対して)
# ------------------------------------------------------------------

DECK_FIXTURE = Path(__file__).parent / "fixtures" / "pokecabook_deck_page.html"
CATALOG_FIXTURE = Path(__file__).parent / "fixtures" / "pokecabook_deck_catalog.html"
DECK_TITLE = "ポケカ【ドラパルトex】優勝デッキレシピまとめ"
DECK_URL = "https://pokecabook.com/archives/122503"


@pytest.fixture(scope="module")
def deck_records() -> list[DeckResult]:
    html = DECK_FIXTURE.read_text(encoding="utf-8")
    return deckindex.parse_deck_page(html, DECK_TITLE, DECK_URL, date(2026, 8, 11))


def test_deck_page_extracts_name_date_and_code(deck_records):
    assert len(deck_records) == 4
    for record in deck_records:
        assert record.deck_name == "ドラパルトex"
        assert record.event_type == EVENT_GYM
        assert record.rank == 1
        assert record.date == "2026-08-09"
        assert record.deck_code
    assert deck_records[0].deck_code == "cxxGxa-71MIig-J8cY8c"


def test_deck_page_skips_non_result_figures(deck_records):
    """「デッキレシピ平均化」「エーススペック採用率」などの図は結果ではない。"""
    codes = {r.deck_code for r in deck_records}
    assert "ccxGG8-z4bxe1-xD8G8K" not in codes


def test_deck_page_never_produces_a_future_date():
    """大会結果が未来の日付になることはない。

    デッキ別ページには前年の結果も並ぶため、収集日より先の月日は前年とみなす。
    本番でこれを誤り、43件が未来日付になってランキングの基準日が壊れた。
    """
    html = """
    <div class="entry-content"><figure class="wp-block-image">
      <img alt="【メガライボルトex】ジムバトル優勝デッキレシピ" src="x.png">
      <figcaption><a href="https://www.pokemon-card.com/deck/result.html/deckID/aa-bb-cc/">
        8/12【火】ジムバトル優勝</a></figcaption>
    </figure></div>
    """
    # 収集日は 8/11。8/12 は明日なので、前年の 8/12 と解釈する
    records = deckindex.parse_deck_page(html, DECK_TITLE, DECK_URL, date(2026, 8, 11))
    assert records[0].date == "2025-08-12"


def test_deck_page_resolves_year_boundary():
    """1月が基準日のとき、12月の結果には前年を付ける。"""
    html = """
    <div class="entry-content"><figure class="wp-block-image">
      <img alt="【リザードンex】ジムバトル優勝デッキレシピ" src="x.png">
      <figcaption><a href="https://www.pokemon-card.com/deck/result.html/deckID/aa-bb-cc/">
        12/28【土】ジムバトル優勝</a></figcaption>
    </figure></div>
    """
    records = deckindex.parse_deck_page(html, DECK_TITLE, DECK_URL, date(2026, 1, 10))
    assert records[0].date == "2025-12-28"


def test_deck_page_falls_back_to_title_for_name():
    """img の alt が欠けていても記事タイトルから名前を取る。"""
    html = """
    <div class="entry-content"><figure class="wp-block-image">
      <img src="x.png">
      <figcaption><a href="https://www.pokemon-card.com/deck/result.html/deckID/aa-bb-cc/">
        8/9【日】ジムバトル優勝</a></figcaption>
    </figure></div>
    """
    records = deckindex.parse_deck_page(html, DECK_TITLE, DECK_URL, date(2026, 8, 11))
    assert records[0].deck_name == "ドラパルトex"


def test_deck_page_reads_city_league_captions_too():
    html = """
    <div class="entry-content"><figure class="wp-block-image">
      <img alt="【リザードンex】デッキレシピ" src="x.png">
      <figcaption><a href="https://www.pokemon-card.com/deck/confirm.html/deckID/aa-bb-cc/">
        5/6【水】シティリーグ準優勝</a></figcaption>
    </figure></div>
    """
    records = deckindex.parse_deck_page(html, DECK_TITLE, DECK_URL, date(2026, 8, 11))
    assert records[0].event_type == EVENT_CITY
    assert records[0].rank == 2


@pytest.mark.parametrize(
    "name,ok",
    [
        ("ドラパルトex", True),
        ("Nのゾロアークex", True),
        ("ドラパルトex＋バシャーモex", True),
        ("おまつりおんど", True),
        # 実際の収集で紛れ込んだもの。日付ページのタイトルから拾ってしまった
        ("8/10(月)", False),
        ("8/9(日)", False),
        ("12月14日", False),
        # 弾や環境の名前であってデッキ名ではない
        ("ストームエメラルダ環境", False),
        ("ジムバトル優勝デッキまとめ", False),
        ("環境デッキ一覧", False),
        ("", False),
    ],
)
def test_is_plausible_deck_name(name, ok):
    assert deckindex.is_plausible_deck_name(name) is ok


def test_extract_deck_name_skips_dates_and_takes_the_deck():
    """【…】が複数あるタイトルから、デッキ名として妥当なものを選ぶ。"""
    assert (
        deckindex.extract_deck_name("ポケカ【ドラパルトex】優勝デッキレシピまとめ")
        == "ドラパルトex"
    )
    # 日付ページ: どの【…】もデッキ名ではないので空を返す
    assert (
        deckindex.extract_deck_name("【8/10(月)】ジムバトル優勝デッキまとめ【ストームエメラルダ環境】")
        == ""
    )


def test_catalog_name_wins_over_page_content():
    """デッキ一覧ページの表記を正とする (一番確実な出どころ)。"""
    html = """
    <div class="entry-content"><figure class="wp-block-image">
      <img alt="【まちがった名前】ジムバトル優勝デッキレシピ" src="x.png">
      <figcaption><a href="https://www.pokemon-card.com/deck/result.html/deckID/aa-bb-cc/">
        8/9【日】ジムバトル優勝</a></figcaption>
    </figure></div>
    """
    records = deckindex.parse_deck_page(
        html, "【8/10(月)】ジムバトル優勝デッキまとめ", DECK_URL,
        date(2026, 8, 11), deck_name="メガレックウザex",
    )
    assert records[0].deck_name == "メガレックウザex"


def test_sanitize_clears_implausible_names_already_saved():
    """過去に保存された変なデッキ名は、実行のたびに直る。"""
    from src.pokeca.store import sanitize_results

    records = [
        _record(deck_name="8/10(月)", deck_code="a-1"),
        _record(deck_name="ストームエメラルダ環境", deck_code="a-2"),
        _record(deck_name="ドラパルトex", deck_code="a-3"),
    ]
    cleaned, fixed = sanitize_results(records, known_decks=set())
    assert fixed == 2
    assert [r.deck_name for r in cleaned] == ["", "", "ドラパルトex"]
    # レコード自体は消さない (日付とデッキコードは使える)
    assert all(r.deck_code for r in cleaned)


def test_sanitize_uses_the_catalog_to_reject_set_names():
    """「アビスアイ」「ストームエメラルダ」は形だけでは弾けない。

    デッキ名らしい見た目をしているが実際は弾の名前なので、
    デッキ一覧に載っているかどうかで判定する。
    """
    from src.pokeca.store import sanitize_results

    known = {normalize_deck_name("ドラパルトex"), normalize_deck_name("メガレックウザex")}
    records = [
        _record(deck_name="アビスアイ", deck_code="a-1"),
        _record(deck_name="ストームエメラルダ", deck_code="a-2"),
        _record(deck_name="ドラパルトex", deck_code="a-3"),
        _record(deck_name="メガレックウザex", deck_code="a-4"),
    ]
    cleaned, fixed = sanitize_results(records, known_decks=known)
    assert fixed == 2
    assert [r.deck_name for r in cleaned] == ["", "", "ドラパルトex", "メガレックウザex"]


def test_sanitize_pulls_future_dates_back_a_year():
    """保存済みの未来日付は1年戻して直す。

    マージは空欄しか埋めないので、日付が間違ったレコードは
    再収集しても直らない。掃除のときに直しておく必要がある。
    """
    from src.pokeca.store import now_jst, sanitize_results

    tomorrow = (now_jst().date() + timedelta(days=1)).isoformat()
    records = [_record(date=tomorrow, deck_name="ドラパルトex", deck_code="a-1")]
    cleaned, fixed = sanitize_results(records, known_decks=set())
    assert fixed == 1
    assert cleaned[0].date == tomorrow.replace(tomorrow[:4], str(int(tomorrow[:4]) - 1), 1)


def test_sanitize_leaves_past_dates_alone():
    from src.pokeca.store import sanitize_results

    records = [_record(date="2026-05-06", deck_name="ドラパルトex", deck_code="a-1")]
    cleaned, fixed = sanitize_results(records, known_decks=set())
    assert (fixed, cleaned[0].date) == (0, "2026-05-06")


def test_sanitize_keeps_everything_when_catalog_is_missing():
    """正解リストがまだ無いときに、全部消してしまわないこと。"""
    from src.pokeca.store import sanitize_results

    records = [_record(deck_name="まだ知らないデッキex", deck_code="a-1")]
    cleaned, fixed = sanitize_results(records, known_decks=set())
    assert fixed == 0
    assert cleaned[0].deck_name == "まだ知らないデッキex"


def test_ranking_is_not_polluted_by_cleared_names():
    records = [
        _record(store="A", deck_name="8/10(月)", deck_code="a-1"),
        _record(store="B", deck_name="8/10(月)", deck_code="a-2"),
        _record(store="C", deck_name="ドラパルトex", deck_code="a-3"),
    ]
    from src.pokeca.store import sanitize_results

    cleaned, _ = sanitize_results(records)
    ranked = aggregate.deck_ranking(cleaned, days=0)
    assert [e["deck_name"] for e in ranked] == ["ドラパルトex"]


def test_daily_batch_covers_everything_over_time():
    """毎日ちがう区間を見に行き、数日で一周する。"""
    catalog = [(f"deck{i}", f"u{i}") for i in range(73)]
    seen: set[str] = set()
    for offset in range(3):
        day = date(2026, 8, 11 + offset)
        seen.update(name for name, _ in deckindex.daily_batch(catalog, 25, today=day))
    assert len(seen) == 73  # 3日で全デッキを踏む


def test_daily_batch_returns_a_fixed_size_slice():
    catalog = [(f"deck{i}", f"u{i}") for i in range(73)]
    todays = deckindex.daily_batch(catalog, 25, today=date(2026, 8, 11))
    assert len(todays) == 25
    assert len({n for n, _ in todays}) == 25  # 同じデッキを二度取らない


def test_daily_batch_returns_all_when_batch_is_large_enough():
    catalog = [(f"deck{i}", f"u{i}") for i in range(10)]
    assert deckindex.daily_batch(catalog, 25) == catalog
    assert deckindex.daily_batch(catalog, 0) == catalog


def test_build_name_index(deck_records):
    index = deckindex.build_name_index(deck_records)
    assert index["cxxGxa-71MIig-J8cY8c"] == "ドラパルトex"


def test_parse_catalog_returns_deck_names_and_urls():
    pairs = deckindex.parse_catalog(CATALOG_FIXTURE.read_text(encoding="utf-8"))
    names = [n for n, _ in pairs]
    assert "ドラパルトex" in names
    assert "メガレックウザex" in names
    # 「まとめ」記事 (ジムバトル優勝デッキまとめ等) はデッキではないので除く
    assert not any(n.endswith("まとめ") for n in names)
    assert all(url.startswith("https://pokecabook.com/archives/") for _, url in pairs)


# ------------------------------------------------------------------
# 取得経路の切り替え (REST が 403 で閉じられている場合の回り込み)
# ------------------------------------------------------------------

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <item>
      <title>シティリーグ5/6【水】ベスト16デッキまとめ</title>
      <link>https://pokecabook.com/archives/320777</link>
      <pubDate>Wed, 06 May 2026 17:01:30 +0900</pubDate>
      <content:encoded><![CDATA[__CONTENT__]]></content:encoded>
    </item>
  </channel>
</rss>
"""


def _feed_with(content: str) -> str:
    return FEED_XML.replace("__CONTENT__", content)


def test_feed_uses_embedded_content_when_full(monkeypatch):
    """RSS に本文が丸ごと入っていれば、記事を個別に取りに行かない。"""
    body = FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setattr(wp.http, "get_text", lambda url, **kw: _feed_with(body))
    monkeypatch.setattr(
        wp, "fetch_article_html", lambda url: pytest.fail("記事を取りに行くべきではない")
    )
    posts = wp.posts_from_feed("https://example.test/feed/", limit=5)
    assert len(posts) == 1
    assert posts[0].published == date(2026, 5, 6)
    assert posts[0].link == "https://pokecabook.com/archives/320777"
    assert "宝島" in posts[0].content_html


def test_feed_falls_back_to_article_when_only_excerpt(monkeypatch):
    """RSS が抜粋しか返さない設定なら、記事本文を取りに行く。"""
    monkeypatch.setattr(wp.http, "get_text", lambda url, **kw: _feed_with("短い抜粋"))
    monkeypatch.setattr(wp, "fetch_article_html", lambda url: "<div>本文</div>")
    posts = wp.posts_from_feed("https://example.test/feed/", limit=5)
    assert posts[0].content_html == "<div>本文</div>"


def test_fetch_posts_falls_back_from_rest_to_feed(monkeypatch):
    """REST が 403 でも RSS が生きていれば収集は続く。"""
    calls: list[str] = []

    def boom(*a, **kw):
        calls.append("rest")
        raise RuntimeError("403 Client Error: Forbidden")

    monkeypatch.setattr(wp, "posts_from_rest", boom)
    monkeypatch.setattr(
        wp,
        "posts_from_feed",
        lambda url, limit: [
            wp.Post(title="t", content_html="<p>x</p>", link="u", published=date(2026, 5, 6))
        ],
    )
    posts = wp.fetch_posts(
        api="a", slug="s", feed_url="f", index_url="i", base="b", limit=5
    )
    assert calls == ["rest"]
    assert len(posts) == 1


def test_fetch_posts_raises_when_every_route_fails(monkeypatch):
    for name in ("posts_from_rest", "posts_from_feed", "posts_from_index"):
        monkeypatch.setattr(
            wp, name, lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("403"))
        )
    with pytest.raises(wp.NoRouteAvailable) as excinfo:
        wp.fetch_posts(api="a", slug="s", feed_url="f", index_url="i", base="b", limit=5)
    # どの経路がなぜ駄目だったのかがメッセージに残る
    assert "REST API" in str(excinfo.value)
    assert "RSS" in str(excinfo.value)


def test_article_links_are_absolutised():
    html = '<a href="/archives/123">a</a><a href="https://pokecabook.com/archives/456">b</a>'
    links = wp._article_links(html, "https://pokecabook.com")
    assert links == [
        "https://pokecabook.com/archives/123",
        "https://pokecabook.com/archives/456",
    ]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-05-06T17:01:30+09:00", date(2026, 5, 6)),
        ("Wed, 06 May 2026 17:01:30 +0900", date(2026, 5, 6)),
    ],
)
def test_parse_date_accepts_both_formats(raw, expected):
    assert wp._parse_date(raw) == expected


def test_collect_parses_posts_from_whichever_route_worked(monkeypatch):
    """経路が変わっても、後段の解析は同じように動く。"""
    body = FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setattr(
        pokecabook.wp,
        "fetch_posts",
        lambda **kw: [
            wp.Post(
                title=POST_TITLE,
                content_html=body,
                link=POST_URL,
                published=PUBLISHED,
            )
        ],
    )
    results = pokecabook.collect(limit=5)
    assert len(results) == 4
    assert all(r.deck_code for r in results)


# ------------------------------------------------------------------
# 公式サイトのパーサー (リーグ区分の補完用・構造は未検証)
# ------------------------------------------------------------------


def test_official_league_extraction():
    html = "<h1>シティリーグ2026 シーズン3 ジュニアリーグ</h1><p>2026年5月6日</p>"
    assert official.extract_league(html) == "ジュニア"


def test_official_league_extraction_returns_empty_when_unknown():
    assert official.extract_league("<h1>なにかのイベント</h1>") == ""


# ------------------------------------------------------------------
# マージ
# ------------------------------------------------------------------


def _record(**kwargs) -> DeckResult:
    base = dict(
        date="2026-05-06",
        store="バトロコ 高田馬場",
        rank=1,
        deck_name="ドラパルトex",
        league="",
    )
    base.update(kwargs)
    return DeckResult(**base)


def test_merge_fills_in_deck_name_via_deck_code():
    """デッキコードだけ先に入り、あとからデッキ名が付くのが実際の流れ。

    大会ページ (順位・店舗はあるがデッキ名が無い) と
    デッキ別ページ (デッキ名はあるが店舗が無い) を、
    同じデッキコードを持つ同一の結果として結び付ける。
    """
    existing = [
        _record(deck_name="", deck_code="abc-123", store="宝島 岐阜本店", source="pokecabook")
    ]
    incoming = [
        _record(deck_name="ドラパルトex", deck_code="abc-123", store="", source="deckindex")
    ]
    merged, added, updated = merge_results(existing, incoming)
    assert (added, updated) == (0, 1)
    assert len(merged) == 1
    assert merged[0].deck_name == "ドラパルトex"
    assert merged[0].store == "宝島 岐阜本店"  # 店舗は消えない


def test_merge_keeps_different_deck_codes_apart():
    merged, added, _ = merge_results(
        [_record(deck_code="aaa-111")], [_record(deck_code="bbb-222")]
    )
    assert (added, len(merged)) == (1, 2)


def test_merge_accepts_records_that_only_have_a_deck_code():
    """ポケカブック由来のレコードは名前が無くコードだけ。これが通常の状態。"""
    merged, added, _ = merge_results([], [_record(deck_name="", deck_code="abc-123")])
    assert added == 1
    assert merged[0].deck_code == "abc-123"


def test_merge_rejects_records_with_neither_name_nor_code():
    merged, added, _ = merge_results([], [_record(deck_name="", deck_code="")])
    assert (added, merged) == (0, [])


def test_merge_absorbs_store_spacing_differences():
    existing = [_record(store="バトロコ　高田馬場", source="pokecabook")]
    merged, added, _ = merge_results(existing, [_record(store="バトロコ 高田馬場")])
    assert added == 0
    assert len(merged) == 1


def test_merge_keeps_different_leagues_apart():
    merged, added, _ = merge_results(
        [_record(league="オープン")], [_record(league="ジュニア")]
    )
    assert added == 1
    assert len(merged) == 2


def test_merge_is_idempotent():
    records = [_record(source="pokecabook")]
    once, added1, _ = merge_results([], records)
    twice, added2, _ = merge_results(once, records)
    assert (added1, added2, len(twice)) == (1, 0, 1)


def test_merge_does_not_overwrite_existing_values():
    existing = [_record(deck_name="ドラパルトex", deck_code="keep-me")]
    merged, _, _ = merge_results(existing, [_record(deck_code="new-code")])
    assert merged[0].deck_code == "keep-me"


# ------------------------------------------------------------------
# 集計
# ------------------------------------------------------------------


def test_deck_ranking_orders_by_wins_then_runner_ups():
    results = [
        _record(store="A", rank=1, deck_name="ドラパルトex"),
        _record(store="B", rank=1, deck_name="ドラパルトex"),
        _record(store="C", rank=1, deck_name="リザードンex"),
        _record(store="D", rank=2, deck_name="リザードンex"),
        _record(store="E", rank=2, deck_name="サーナイトex"),
    ]
    ranked = aggregate.deck_ranking(results, days=0)
    assert [e["deck_name"] for e in ranked] == ["ドラパルトex", "リザードンex", "サーナイトex"]
    assert ranked[0]["first"] == 2


def test_deck_ranking_ignores_unnamed_decks():
    """名前が付いていないデッキはランキングに出さない (「」が1位になるのを防ぐ)。"""
    results = [
        _record(store="A", rank=1, deck_name="ドラパルトex"),
        _record(store="B", rank=1, deck_name=""),
        _record(store="C", rank=1, deck_name=""),
    ]
    ranked = aggregate.deck_ranking(results, days=0)
    assert [e["deck_name"] for e in ranked] == ["ドラパルトex"]


def test_deck_ranking_respects_period():
    results = [
        _record(date="2026-05-06", store="A", deck_name="ドラパルトex"),
        _record(date="2026-04-01", store="B", deck_name="リザードンex"),
    ]
    recent = aggregate.deck_ranking(results, days=7, today=date(2026, 5, 6))
    assert [e["deck_name"] for e in recent] == ["ドラパルトex"]
    assert len(aggregate.deck_ranking(results, days=0)) == 2
