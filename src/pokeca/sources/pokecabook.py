"""ポケカブック (pokecabook.com) から優勝・準優勝デッキを拾う。

サイトが WordPress で動いているため、WP REST API (``/wp-json/wp/v2/``) で
記事本文を JSON として取得する。一覧ページのレイアウト変更に巻き込まれにくい。

実物の記事構造 (2026年5月時点、archives/320777 で確認済み):

    <div class="entry-content">
      <h2><span id="toc1">宝島　岐阜本店（岐阜）</span></h2>
      <p><a href="https://players.pokemon-card.com/event/detail/953108/result">大会結果</a></p>
      <figure class="wp-block-gallery">
        <figure class="wp-block-image">
          <a href="...jpg"><img src="...1_1_8YGKY8-wTd9K2-8Dacc4.jpg"></a>
          <figcaption>
            <a href="https://www.pokemon-card.com/deck/confirm.html/deckID/8YGKY8-wTd9K2-8Dacc4">優勝</a>
          </figcaption>
        </figure>
        ... 準優勝 / TOP4 / TOP8 / TOP16 が続く
      </figure>
      <h2>... 次の店舗 ...</h2>
    </div>

重要な性質:

- **デッキ名はどこにも書かれていない**。デッキの中身は画像で示されており、
  文字情報としては順位ラベルとデッキコードしか無い
- そのかわり **公式のデッキコードが全エントリーに付いている**。
  ここから実物の60枚レシピを開けるので、レシピへの導線はこれで完結する
- 公式イベントページのURLも各店舗に付いている (リーグ区分を補うのに使える)
- ``<h2>`` 内の ``<span id="tocN">`` を使うと元記事の該当店舗へ直接ジャンプできる
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup, Tag

from src.pokeca.models import EVENT_CITY, DeckResult
from src.pokeca.sources import wp

BASE = "https://pokecabook.com"
API = f"{BASE}/wp-json/wp/v2"
CATEGORY_SLUG = "city-league"
CATEGORY_PATH = f"{BASE}/archives/category/tournament/city-league"
FEED_URL = f"{CATEGORY_PATH}/feed/"
INDEX_URL = CATEGORY_PATH

SOURCE_NAME = "pokecabook"

CONTENT_SELECTOR = ".entry-content"

# figcaption のラベル → 順位。TOP4 以降は追いかけない。
RANK_BY_CAPTION = {"優勝": 1, "準優勝": 2}

DECK_CODE_RE = re.compile(r"/deck/confirm\.html/deckID/([A-Za-z0-9\-]+)")
EVENT_ID_RE = re.compile(r"players\.pokemon-card\.com/event/detail/(\d+)/result")

# 「宝島　岐阜本店（岐阜）」→ 店舗名 と 都道府県
STORE_RE = re.compile(r"^(?P<store>.+?)\s*[（(](?P<pref>[^）)]{2,8})[）)]\s*$")

# タイトルから開催日を拾う (「シティリーグ5/6【水】ベスト16デッキまとめ」)
TITLE_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*[/／月]\s*(\d{1,2})")

LEAGUE_KEYWORDS = ("オープン", "マスター", "シニア", "ジュニア")


def _resolve_date(month: int, day: int, today: date) -> str:
    """月日から実際の開催日を決める。

    タイトルには「シティリーグ5/6【水】」のように年が書かれていない。
    大会結果が未来の日付になることはないので、今日を超えない範囲で
    一番新しい年を選ぶ。これで年末年始をまたぐ記事も正しく扱える。
    """
    for year in (today.year, today.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= today:
            return candidate.isoformat()
    return ""


def extract_date_from_title(title: str, today: date) -> str:
    match = TITLE_DATE.search(title)
    if not match:
        return today.isoformat()
    return _resolve_date(int(match.group(1)), int(match.group(2)), today) or today.isoformat()


def _detect_league(text: str) -> str:
    for keyword in LEAGUE_KEYWORDS:
        if keyword in text:
            return keyword
    return ""


def split_store(heading: str) -> tuple[str, str]:
    """「宝島　岐阜本店（岐阜）」→ ("宝島 岐阜本店", "岐阜")。"""
    text = heading.replace("　", " ").strip()
    match = STORE_RE.match(text)
    if match:
        return match.group("store").strip(), match.group("pref").strip()
    return text, ""


def _event_url(node: Tag) -> str:
    match = EVENT_ID_RE.search(str(node))
    if not match:
        return ""
    return f"https://players.pokemon-card.com/event/detail/{match.group(1)}/result"


def _anchor_id(heading: Tag) -> str:
    """見出し内の <span id="tocN"> を拾って元記事への直リンクに使う。"""
    span = heading.find(id=True)
    return span.get("id", "") if span else ""


def parse_post(
    content_html: str, title: str, source_url: str, today: date
) -> list[DeckResult]:
    """1記事ぶんの本文HTMLから優勝・準優勝を抜き出す。

    Args:
        today: 収集日。タイトルに年が無いので、これを超えない範囲で
            一番新しい年を割り当てる。
    """
    soup = BeautifulSoup(content_html, "html.parser")
    content = soup.select_one(CONTENT_SELECTOR) or soup

    held = extract_date_from_title(title, today)
    league_from_title = _detect_league(title)

    records: list[DeckResult] = []
    for heading in content.find_all("h2"):
        store, prefecture = split_store(heading.get_text(" ", strip=True))
        if not store:
            continue

        anchor = _anchor_id(heading)
        store_url = f"{source_url}#{anchor}" if anchor and source_url else source_url

        event_url = ""
        found: list[tuple[int, str]] = []

        # 次の <h2> までがこの店舗のブロック
        for sibling in heading.next_siblings:
            if getattr(sibling, "name", None) == "h2":
                break
            if not isinstance(sibling, Tag):
                continue
            if not event_url:
                event_url = _event_url(sibling)
            for caption in sibling.find_all("figcaption"):
                rank = RANK_BY_CAPTION.get(caption.get_text(" ", strip=True))
                if rank is None:
                    continue
                link = caption.find("a", href=True)
                code_match = DECK_CODE_RE.search(link["href"]) if link else None
                found.append((rank, code_match.group(1) if code_match else ""))

        for rank, deck_code in found:
            records.append(
                DeckResult(
                    date=held,
                    store=store,
                    rank=rank,
                    # この記事にデッキ名は書かれていない。
                    # 名前は deckindex がデッキコード経由で後から埋める
                    deck_name="",
                    event_type=EVENT_CITY,
                    prefecture=prefecture,
                    league=league_from_title,
                    deck_code=deck_code,
                    source=SOURCE_NAME,
                    source_url=store_url,
                    event_url=event_url,
                )
            )

    return records


# ------------------------------------------------------------------
# 取得
# ------------------------------------------------------------------


def fetch_posts(limit: int = 20, log=None) -> list[wp.Post]:
    """シティリーグカテゴリの新着記事を取得する。

    REST API → RSS → 記事一覧HTML の順に試す (src/pokeca/sources/wp.py)。
    """
    return wp.fetch_posts(
        api=API,
        slug=CATEGORY_SLUG,
        feed_url=FEED_URL,
        index_url=INDEX_URL,
        base=BASE,
        limit=limit,
        search="シティリーグ",
        log=log,
    )


def collect(limit: int = 20, log=None) -> list[DeckResult]:
    """新着記事を巡回して優勝・準優勝レコードを返す。"""
    out: list[DeckResult] = []
    for post in fetch_posts(limit=limit, log=log):
        # ジムバトルなど別の記事が混ざらないようにする
        if "シティリーグ" not in post.title:
            continue
        out.extend(parse_post(post.content_html, post.title, post.link, date.today()))
    return out
