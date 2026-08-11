"""ポケカブックの「デッキ別ページ」から、デッキ名付きの結果を拾う。

シティリーグのまとめ記事にはデッキ名が無い (デッキは画像で示される) が、
デッキ別ページ (例:「ポケカ【ドラパルトex】優勝デッキレシピまとめ」) は
**デッキ名ごとに** 結果が集められているので、そこから名前を取れる。

実物の構造 (archives/122503 で確認済み):

    <div class="entry-content">
      <h2>ストームエメラルダ環境</h2>
      <figure class="wp-block-image">
        <img alt="【ドラパルトex】ジムバトル優勝デッキレシピ" src=".../image-132.png">
        <figcaption>
          <a href="https://www.pokemon-card.com/deck/result.html/deckID/cxxGxa-71MIig-J8cY8c/">
            8/9【日】ジムバトル優勝
          </a>
        </figcaption>
      </figure>
      ... 同じ形が数百件続く
    </div>

つまり1つの figure から次の3つが同時に取れる。

- デッキ名   : img の alt にある【…】
- 開催日と種別: figcaption の「8/9【日】ジムバトル優勝」
- デッキコード: figcaption のリンク先

このモジュールの成果物は2つある。

1. **ジムバトルの優勝レコード** そのもの (店舗名は載っていないので空)
2. **デッキコード → デッキ名の対応表**。シティリーグ側で拾ったコードと
   突き合わせれば、名前の無いレコードに名前を付けられる

なお、ポケカブックがジムバトルについて公開しているのは優勝のみで、
準優勝は掲載されていない (実物ページの324件すべてが「ジムバトル優勝」)。
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from src.pokeca.models import EVENT_CITY, EVENT_GYM, DeckResult
from src.pokeca.sources import wp

BASE = "https://pokecabook.com"
API = f"{BASE}/wp-json/wp/v2"
CATEGORY_SLUG = "deck-recipe"
CATEGORY_PATH = f"{BASE}/archives/category/deck-recipe"
FEED_URL = f"{CATEGORY_PATH}/feed/"
INDEX_URL = CATEGORY_PATH
CATALOG_URL = f"{BASE}/archives/1417"

SOURCE_NAME = "deckindex"

# 「8/9【日】ジムバトル優勝」「12/28【土】シティリーグ準優勝」
CAPTION_RE = re.compile(
    r"^(?P<month>\d{1,2})\s*/\s*(?P<day>\d{1,2})\s*【.】\s*"
    r"(?P<event>ジムバトル|シティリーグ)?\s*(?P<rank>準優勝|優勝)"
)
# デッキコードは confirm.html / result.html どちらの形でも出てくる
DECK_CODE_RE = re.compile(r"/deck/(?:confirm|result)\.html/deckID/([A-Za-z0-9\-]+)")
# 「【ドラパルトex】ジムバトル優勝デッキレシピ」「ポケカ【ドラパルトex】優勝…」
BRACKET_NAME_RE = re.compile(r"【([^】]+)】")

RANK_BY_LABEL = {"優勝": 1, "準優勝": 2}
EVENT_BY_LABEL = {"ジムバトル": EVENT_GYM, "シティリーグ": EVENT_CITY}

# カタログに混ざっている、デッキ別ページではない記事
CATALOG_SKIP = re.compile(r"(まとめ|一覧|ランキング|環境|結果)$")

# デッキ名として明らかにおかしいもの。
# 「【8/10(月)】ジムバトル優勝デッキまとめ」のような日付ページを取り込むと
# 日付がデッキ名になってしまうため、名前を付けずに捨てる。
# 名前が空でも日付とデッキコードは使えるので、誤った名前を付けるより良い。
IMPLAUSIBLE_NAME = re.compile(
    r"^\s*\d{1,2}\s*[/月]"          # 8/10(月) などの日付
    r"|環境\s*$"                     # ストームエメラルダ環境
    r"|(まとめ|一覧|リスト|ランキング|結果)\s*$"
)


def is_plausible_deck_name(name: str) -> bool:
    """デッキ名として使ってよさそうかどうか。"""
    name = (name or "").strip()
    if not name or len(name) > 30:
        return False
    return not IMPLAUSIBLE_NAME.search(name)


def _resolve_date(month: int, day: int, today: date) -> str:
    """月日から実際の開催日を決める。

    キャプションは「8/9【日】」のように年が書かれていない。
    **大会結果が未来の日付になることはない**ので、今日を超えない範囲で
    一番新しい年を選ぶ。

    以前は記事の公開日を基準に「30日以上先なら前年」としていたが、
    デッキ別ページには前年の結果も並ぶため、8月に公開されたページの
    「8/12」を当年と判定して未来の日付を作ってしまっていた
    (実際に本番データへ43件混入し、ランキングの基準日を壊した)。
    """
    for year in (today.year, today.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue  # 閏日など
        if candidate <= today:
            return candidate.isoformat()
    return ""


def extract_deck_name(title: str) -> str:
    """記事タイトルからデッキ名を取り出す。

    「ポケカ【ドラパルトex】優勝デッキレシピまとめ」→「ドラパルトex」

    【…】が複数ある記事もあるので、デッキ名として妥当なものを順に探す。
    「【8/10(月)】ジムバトル優勝デッキまとめ【ストームエメラルダ環境】」のように
    どれも妥当でなければ空文字を返す。
    """
    for candidate in BRACKET_NAME_RE.findall(title or ""):
        if is_plausible_deck_name(candidate):
            return candidate.strip()
    return ""


def parse_catalog(html: str) -> list[tuple[str, str]]:
    """デッキ一覧ページから (デッキ名, 記事URL) を取り出す。"""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one(".entry-content") or soup
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for link in content.find_all("a", href=re.compile(r"/archives/\d+")):
        name = link.get_text(" ", strip=True)
        href = link["href"]
        # リンク文字列が空のもの (サムネイルだけのリンク) や、
        # デッキ名ではないまとめ記事は除く
        if not name or CATALOG_SKIP.search(name) or href in seen:
            continue
        seen.add(href)
        out.append((name, href))
    return out


def parse_deck_page(
    content_html: str,
    title: str,
    source_url: str,
    today: date,
    deck_name: str = "",
) -> list[DeckResult]:
    """デッキ別ページ1枚から結果レコードを取り出す。

    Args:
        today: 収集日。キャプションには年が無いので、これを超えない範囲で
            一番新しい年を割り当てる。
        deck_name: デッキ一覧ページから取れた正式なデッキ名。
            指定されていればこれを最優先で使う (一番確実な出どころ)。
    """
    soup = BeautifulSoup(content_html, "html.parser")
    content = soup.select_one(".entry-content") or soup

    fallback_name = deck_name.strip() or extract_deck_name(title)
    records: list[DeckResult] = []

    for figure in content.find_all("figure", class_="wp-block-image"):
        caption = figure.find("figcaption")
        if not caption:
            continue
        match = CAPTION_RE.match(caption.get_text(" ", strip=True))
        if not match:
            # 「デッキレシピ平均化」「エーススペック採用率」など、
            # 大会結果ではない図は飛ばす
            continue

        link = caption.find("a", href=True)
        code_match = DECK_CODE_RE.search(link["href"]) if link else None
        if not code_match:
            continue

        held = _resolve_date(int(match.group("month")), int(match.group("day")), today)
        if not held:
            continue

        image = figure.find("img")
        from_alt = extract_deck_name(image.get("alt", "")) if image else ""

        records.append(
            DeckResult(
                date=held,
                store="",  # デッキ別ページに店舗名は載っていない
                rank=RANK_BY_LABEL[match.group("rank")],
                deck_name=fallback_name or from_alt,
                event_type=EVENT_BY_LABEL.get(match.group("event") or "", EVENT_GYM),
                deck_code=code_match.group(1),
                source=SOURCE_NAME,
                source_url=source_url,
            )
        )

    return records


# ------------------------------------------------------------------
# 取得
# ------------------------------------------------------------------


def fetch_catalog() -> list[tuple[str, str]]:
    """デッキ一覧ページから (デッキ名, 記事URL) を取る。"""
    return parse_catalog(wp.get_text(CATALOG_URL))


def fetch_deck_posts(limit: int = 80, log=None) -> list[wp.Post]:
    """デッキ別記事を取得する (inspect 用)。"""
    posts: list[wp.Post] = []
    for _, url in fetch_catalog()[:limit]:
        post = wp.fetch_article(url)
        if post:
            posts.append(post)
    return posts


def daily_batch(catalog: list, batch: int, today: date | None = None) -> list:
    """その日に見に行くぶんだけカタログから切り出す。

    デッキ別ページは1枚あたり 500KB 前後あり、73デッキを毎日まとめて取ると
    40MB を超える。個人ブログに対してそれを毎日続けるのは行儀が悪いので、
    日ごとに区切って巡回し、数日かけて一周する。

    データはマージして貯めていくので、一周ぶん遅れても内容は揃う。
    通し番号は日付から決めるため、状態を持たなくても順に進む。
    """
    if batch <= 0 or batch >= len(catalog):
        return catalog
    day = (today or date.today()).toordinal()
    start = (day * batch) % len(catalog)
    doubled = catalog + catalog
    return doubled[start : start + batch]


def collect(limit: int = 80, batch: int = 25, log=None) -> list[DeckResult]:
    """デッキ別ページを巡回して、デッキ名付きの結果レコードを返す。

    RSS のカテゴリフィードは1回に10件しか返さず、しかも
    「【8/10(月)】ジムバトル優勝デッキまとめ」のような日付ページが混ざるため、
    **デッキ一覧ページ (archives/1417) を正**として使う。
    こうすると全デッキを取りこぼさず、デッキ名も一覧の表記で確定できる。
    """
    catalog = fetch_catalog()[:limit]
    if catalog:
        # デッキ名の正解リストとして保存する。弾の名前などが紛れ込んだとき、
        # ここに無い名前は sanitize_results が空に戻す
        from src.pokeca.store import save_deck_catalog

        save_deck_catalog([name for name, _ in catalog])

    todays = daily_batch(catalog, batch)
    if log:
        log(f"  デッキ一覧: {len(catalog)} デッキ → 今日は {len(todays)} デッキぶん")

    out: list[DeckResult] = []
    fetched = 0
    for deck_name, url in todays:
        post = wp.fetch_article(url)
        if not post:
            continue
        fetched += 1
        out.extend(
            parse_deck_page(
                post.content_html, post.title, url, date.today(), deck_name=deck_name
            )
        )
    if log:
        log(f"  取得: {fetched} デッキページ → {len(out)} 件")
    return out


def build_name_index(records: list[DeckResult]) -> dict[str, str]:
    """デッキコード → デッキ名 の対応表を作る。"""
    return {r.deck_code: r.deck_name for r in records if r.deck_code and r.deck_name}
