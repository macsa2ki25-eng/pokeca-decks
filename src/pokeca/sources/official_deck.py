"""公式サイトからデッキの中身 (60枚) を取る。

デッキコードさえあれば、そのデッキに何のカードが何枚入っているかが分かる。
採用率・型の判別・デッキ診断など、カード単位で見る機能はすべてこれが土台。

## 素のHTMLから読む (こちらが本番)

ブラウザで見えるカード表は JavaScript が組み立てている。実際にサーバーが
返すHTMLには ``<table>`` は存在せず、代わりに **hidden な入力欄**に
デッキの中身がそのまま入っている (deckID/ngHLgL-pA4GKN-9gniQn で確認済み):

    <input type="hidden" name="deck_pke" value="47847_2_1-49270_4_1-45707_3_1-...">
    <input type="hidden" name="deck_gds" value="46219_4_1-48679_1_1-...">
    <input type="hidden" name="deck_sup" value="46824_4_1-46227_2_1-...">
    <input type="hidden" name="deck_sta" value="46041_2_1">
    <input type="hidden" name="deck_ene" value="46116_8_1-46119_4_1-46121_4_1">

``カードID_枚数_?`` を ``-`` で連ねた形。欄の名前が区分に対応する。
カード名と画像は同じHTML内のインラインJSにある:

    PCGDECK.searchItemName[47847]='メガガルーラex(M1S 051/063)';
    PCGDECK.searchItemNameAlt[47847]='メガガルーラex';
    PCGDECK.searchItemCardPict[47847]='/assets/images/card_images/large/M1S/047847_P_....jpg';

上の実物で枚数の合計は 60、種類は 27 で、``searchItemName`` の件数と一致した。
つまり **1回のGETだけで60枚すべてが揃う**。JavaScriptを動かす必要はない。

## 表から読む (控え)

ブラウザで保存したページ (.webarchive) は JS 実行後のDOMなので、そちらには
``<table>`` がある。手元の資料から読むときのために表の解釈も残してある。

カードIDは公式の通し番号で、カード詳細ページ
``card-search/details.php/card/<ID>/`` を引くときにも使う。

**デッキの中身は一度確定したら変わらない**ので、取得したら保存して二度と取りに行かない。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from src.pokeca import http

BASE = "https://www.pokemon-card.com"

# hidden な入力欄の名前 → 区分
SECTION_FIELDS = {
    "deck_pke": "ポケモン",
    "deck_tool": "ポケモンのどうぐ",
    "deck_gds": "グッズ",
    "deck_sup": "サポート",
    "deck_sta": "スタジアム",
    "deck_ene": "エネルギー",
    # 中身が入っている実物をまだ見ていない欄。名前を決めつけると
    # 間違ったまま集計に乗ってしまうので、分かるように印を付けておく。
    "deck_tech": "その他(tech)",
    "deck_ajs": "その他(ajs)",
}
UNCONFIRMED_SECTIONS = {"その他(tech)", "その他(ajs)"}

# 47847_2_1 → カードID 47847 が 2枚。3つ目の数字の意味は不明で、使っていない。
ENTRY_RE = re.compile(r"(\d+)_(\d+)_(\d+)")
# メガガルーラex(M1S 051/063) → 名前 / 収録セット / 番号
FULL_NAME_RE = re.compile(r"^(.*)\(([^()\s]+)\s+([^()\s]+)\)$")


def _js_index(html: str, variable: str) -> dict[str, str]:
    """PCGDECK.<変数>[ID]='値'; をまとめて拾う。"""
    pattern = re.compile(
        r"PCGDECK\.%s\[\s*(\d+)\s*\]\s*=\s*'((?:[^'\\]|\\.)*)'" % re.escape(variable)
    )
    return {
        m.group(1): m.group(2).replace("\\'", "'").replace("\\\\", "\\")
        for m in pattern.finditer(html)
    }


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

    素のレスポンス (hidden 入力欄) を先に見て、無ければ表を読む。

    Returns:
        {"cards": [...], "total": 60, "sections": {"ポケモン": 19, ...}}
        cards の各要素は
        {"id", "name", "set", "number", "count", "section", "image"}
    """
    parsed = parse_hidden_fields(html)
    if parsed["cards"]:
        return parsed
    return parse_card_table(html)


def parse_hidden_fields(html: str) -> dict:
    """素のHTMLの hidden 入力欄からデッキの中身を組み立てる。"""
    soup = BeautifulSoup(html, "html.parser")
    names = _js_index(html, "searchItemName")
    plain_names = _js_index(html, "searchItemNameAlt")
    pictures = _js_index(html, "searchItemCardPict")

    cards: list[dict] = []
    sections: dict[str, int] = {}

    for field, section in SECTION_FIELDS.items():
        node = soup.find("input", attrs={"name": field})
        if node is None:
            continue
        value = node.get("value") or ""
        total = 0
        for match in ENTRY_RE.finditer(value):
            card_id, count = match.group(1), int(match.group(2))
            total += count

            # 収録セットと番号は「名前(セット 番号)」の形で入っている
            full = names.get(card_id, "")
            name = plain_names.get(card_id, "")
            card_set = number = ""
            detail = FULL_NAME_RE.match(full)
            if detail:
                name = name or detail.group(1).strip()
                card_set, number = detail.group(2), detail.group(3)
            elif not name:
                name = full

            cards.append(
                {
                    "id": card_id,
                    "name": name,
                    "set": card_set,
                    "number": number,
                    "count": count,
                    "section": section,
                    "image": pictures.get(card_id, ""),
                }
            )
        if total or value:
            sections[section] = total

    return {
        "cards": cards,
        "total": sum(c["count"] for c in cards),
        "sections": sections,
    }


def unconfirmed_sections(deck: dict) -> list[str]:
    """まだ意味を確かめていない欄にカードが入っていたら、その区分名を返す。

    公式の入力欄のうち deck_tech / deck_ajs は、中身の入った実物を
    まだ見ていない。実際に入っていたら知りたいので、気付けるようにしておく。
    """
    return sorted(
        {
            c["section"]
            for c in deck.get("cards", [])
            if c.get("section") in UNCONFIRMED_SECTIONS
        }
    )


def parse_card_table(html: str) -> dict:
    """JS実行後のDOM (ブラウザで保存したページ) にあるカード表を読む。"""
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
                    "image": "",
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
