"""公式サイトからデッキの中身 (60枚) を取る。

デッキコードさえあれば、そのデッキに何のカードが何枚入っているかが分かる。
採用率・型の判別・デッキ診断など、カード単位で見る機能はすべてこれが土台。

実物の構造 (deck/result.html/deckID/yyMppy-jUiB2j-pyXXRU で確認済み):

    <table>
      <tr><th colspan="2">ポケモン (19)</th></tr>
      <tr>
        <td><a onclick="PCGDECK.cardDetailViewCall('47838')" id="cardName_47838">
              メガサーナイトex<br>M1S<br>042/063</a></td>
        <td><span>3枚</span></td>
      </tr>
      ...
    </table>

1行から カードID・カード名・収録セット・番号・枚数 が同時に取れる。
カードIDは公式の通し番号で、カード詳細ページ
``card-search/details.php/card/<ID>/`` を引くときにも使う。

**デッキの中身は一度確定したら変わらない**ので、取得したら保存して二度と取りに行かない。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from src.pokeca import http

BASE = "https://www.pokemon-card.com"

# 「ポケモン (19)」「ポケモンのどうぐ (0)」のような区分の見出し
SECTION_RE = re.compile(
    r"^(ポケモン|ポケモンのどうぐ|グッズ|サポート|スタジアム|エネルギー)\s*[（(](\d+)[）)]"
)
CARD_ID_RE = re.compile(r"^cardName_(\d+)$")
COUNT_RE = re.compile(r"(\d+)\s*枚")

DECK_SIZE = 60


def deck_url(deck_code: str) -> str:
    return f"{BASE}/deck/result.html/deckID/{deck_code}/"


def deck_url_fallback(deck_code: str) -> str:
    """result.html が使えないデッキ向けの別経路。"""
    return f"{BASE}/deck/confirm.html/deckID/{deck_code}/"


def parse_decklist(html: str) -> dict:
    """デッキページのHTMLから、区分ごとのカード一覧を取り出す。

    Returns:
        {"cards": [...], "total": 60, "sections": {"ポケモン": 19, ...}}
        cards の各要素は
        {"id", "name", "set", "number", "count", "section"}
    """
    soup = BeautifulSoup(html, "html.parser")

    cards: list[dict] = []
    sections: dict[str, int] = {}

    for table in soup.find_all("table"):
        head = table.find("th")
        if not head:
            continue
        match = SECTION_RE.match(head.get_text(" ", strip=True))
        if not match:
            continue
        section = match.group(1)
        sections[section] = int(match.group(2))

        for row in table.find_all("tr"):
            link = row.find("a", id=CARD_ID_RE)
            if not link:
                continue
            card_id = CARD_ID_RE.match(link["id"]).group(1)
            # <a> の中は 名前<br>収録セット<br>番号 の順に並ぶ
            parts = [t.strip() for t in link.stripped_strings if t.strip()]
            cells = row.find_all("td")
            count_match = COUNT_RE.search(cells[-1].get_text(" ", strip=True)) if cells else None
            cards.append(
                {
                    "id": card_id,
                    "name": parts[0] if parts else "",
                    "set": parts[1] if len(parts) > 1 else "",
                    "number": parts[2] if len(parts) > 2 else "",
                    "count": int(count_match.group(1)) if count_match else 0,
                    "section": section,
                }
            )

    return {
        "cards": cards,
        "total": sum(c["count"] for c in cards),
        "sections": sections,
    }


def fetch_decklist(deck_code: str) -> tuple[dict | None, str]:
    """デッキコードから中身を取得する。

    60枚に満たない結果は不完全とみなして捨てる。中途半端なデッキを
    保存してしまうと、採用率の集計がそのぶんだけ狂うため。

    Returns:
        (中身, 失敗理由)。成功時の理由は空文字。
        以前は例外を握りつぶして None を返していたため、ログに
        「失敗 200」としか出ず原因が分からなかった。理由を必ず返す。
    """
    reasons = []
    for url in (deck_url(deck_code), deck_url_fallback(deck_code)):
        try:
            html = http.get_text(url)
        except Exception as exc:
            reasons.append(f"{url.rsplit('/deck/', 1)[-1][:14]}: {type(exc).__name__} {exc}"[:110])
            continue
        parsed = parse_decklist(html)
        if parsed["total"] == DECK_SIZE:
            parsed["deck_code"] = deck_code
            parsed["source_url"] = url
            return parsed, ""
        reasons.append(
            f"{url.rsplit('/deck/', 1)[-1][:14]}: {parsed['total']}枚しか取れず "
            f"(HTML {len(html):,}文字)"
        )
    return None, " / ".join(reasons)
