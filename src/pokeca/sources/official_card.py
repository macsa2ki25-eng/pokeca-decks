"""公式サイトからカード1枚ぶんの中身 (HP・ワザ・特性・効果テキスト) を取る。

デッキの中身から取れるのはカード名と枚数までで、そのカードが何をするのかは
分からない。「山札が回らない」「火力が足りない」といった相談に答えるには、
カードに書かれていることそのものが要る。

実物の構造 (card-search/details.php/card/50452/ で確認済み):

    <div class="RightBox">
      <div class="TopInfo">
        <span class="type">たね</span>
        <span class="hp">HP</span><span class="hp-num">80</span>
        <span class="hp-type"><span class="icon-grass icon"></span></span>
      </div>
      <h2>特性</h2>  <h4>とくせい名</h4> <p>効果テキスト</p>
      <h2>ワザ</h2>
      <h4><span class="icon-grass icon"></span>かけぬける<span class="f_right">20</span></h4>
      <p>相手のベンチポケモン1匹にも、20ダメージ。</p>
      <table>
        <tr><th>弱点</th><th>抵抗力</th><th>にげる</th></tr>
        <tr><td><span class="icon-fire icon"></span>×2</td><td>--</td>
            <td class="escape"><span class="icon-none icon"></span></td></tr>
      </table>
    </div>

タイプとエネルギーは画像ではなく ``<span class="icon-fire icon">`` の
**クラス名**で表されているので、そのまま機械可読。

**カードの内容は刷られた時点で確定していて変わらない**ので、
一度取得したら保存して二度と取りに行かない。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from src.pokeca import http

BASE = "https://www.pokemon-card.com"

# icon-<英名> → 日本語のエネルギー/タイプ表記
#
# 公式(日本)は electric / dark / steel を使う。英語版の呼び方
# (lightning / darkness / metal) とは別なので、両方受ける。
# ここが欠けると「弱点 electric×2」のように英語のまま残り、
# 子どもには読めないうえ、弱点の計算もできない。
ICON_NAMES = {
    "grass": "草",
    "fire": "炎",
    "water": "水",
    "electric": "雷",
    "lightning": "雷",
    "psychic": "超",
    "fighting": "闘",
    "dark": "悪",
    "darkness": "悪",
    "steel": "鋼",
    "metal": "鋼",
    "dragon": "竜",
    "colorless": "無",
    "none": "無",
    "fairy": "妖",
    "void": "",
}


def card_url(card_id: str | int) -> str:
    return f"{BASE}/card-search/details.php/card/{card_id}/"


def _icons(node) -> list[str]:
    """配下の <span class="icon-xxx icon"> をエネルギー表記の並びにする。"""
    if node is None:
        return []
    out = []
    for span in node.find_all("span", class_="icon"):
        for cls in span.get("class", []):
            if cls.startswith("icon-") and cls != "icon":
                name = ICON_NAMES.get(cls[5:], cls[5:])
                if name:
                    out.append(name)
    return out


def _prose(node) -> str:
    """効果テキストを、エネルギーのマークを残したまま文字列にする。

    公式はエネルギーを ``<span class="icon-fire icon">`` で書くので、
    素朴に文字だけ取ると **マークが消えて意味が変わる**。

        ○ 「ついている[炎]と[雷]エネルギーの数×50」
        × 「ついている と エネルギーの数×50」

    実際これで、メガレックウザexのワザを「炎と雷だけ数える」と
    読み違えかけた。文章の意味に関わるので、マークは必ず残す。
    """
    if node is None:
        return ""
    copy = BeautifulSoup(str(node), "html.parser")
    for span in copy.find_all("span", class_="icon"):
        names = [
            ICON_NAMES.get(cls[5:], cls[5:])
            for cls in span.get("class", [])
            if cls.startswith("icon-") and cls != "icon"
        ]
        span.replace_with("".join(n for n in names if n))
    return copy.get_text(" ", strip=True)


def _sections(main, heading: str) -> list[dict]:
    """<h2>ワザ</h2> や <h2>特性</h2> の下に並ぶ h4 + p を拾う。"""
    out: list[dict] = []
    for h2 in main.find_all("h2"):
        if h2.get_text(strip=True) != heading:
            continue
        for h4 in h2.find_all_next("h4"):
            # 次の h2 に入ったら、その見出しの範囲は終わり
            if h4.find_previous("h2") is not h2:
                break
            block = BeautifulSoup(str(h4), "html.parser").h4
            damage_node = block.select_one(".f_right")
            damage = damage_node.get_text(strip=True) if damage_node else ""
            cost = _icons(block)
            for span in block.find_all("span"):
                span.extract()
            paragraph = h4.find_next_sibling("p")
            out.append(
                {
                    "name": block.get_text(" ", strip=True),
                    "cost": cost,
                    "damage": damage,
                    "effect": _prose(paragraph),
                }
            )
    return out


def parse_card(html: str, card_id: str = "") -> dict:
    """カード詳細ページのHTMLを構造化する。"""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one(".RightBox") or soup

    name = ""
    if soup.title:
        name = soup.title.get_text(strip=True).split("|")[0].strip()

    card: dict = {"id": str(card_id), "name": name}

    top = main.select_one(".TopInfo")
    if top:
        stage = top.select_one(".type")
        if stage:
            card["stage"] = stage.get_text(strip=True)
        hp = top.select_one(".hp-num")
        if hp and hp.get_text(strip=True).isdigit():
            card["hp"] = int(hp.get_text(strip=True))
        # タイプは HP の並びにあるアイコン。無い場合 (トレーナーズ) は空
        types = _icons(top.select_one(".hp-type")) or _icons(top)
        card["type"] = types[0] if types else ""

    card["abilities"] = _sections(main, "特性")
    card["attacks"] = _sections(main, "ワザ")

    table = main.find("table")
    if table:
        rows = table.find_all("tr")
        if len(rows) >= 2:
            cells = rows[1].find_all("td")
            if len(cells) >= 3:
                weak = _icons(cells[0])
                card["weakness"] = (
                    f"{weak[0]}{cells[0].get_text(strip=True)}" if weak else ""
                )
                resist = _icons(cells[1])
                card["resistance"] = (
                    f"{resist[0]}{cells[1].get_text(strip=True)}" if resist else ""
                )
                card["retreat"] = len(_icons(cells[2]))

    # トレーナーズやエネルギーは h2/h4 が無く、説明文だけのことがある
    if not card["abilities"] and not card["attacks"]:
        paragraph = main.find("p")
        if paragraph:
            card["text"] = _prose(paragraph)

    return card


def fetch_card(card_id: str | int) -> tuple[dict | None, str]:
    """カードIDから内容を取得する。

    Returns:
        (カード, 失敗理由)。成功時の理由は空文字。
    """
    try:
        html = http.get_text(card_url(card_id))
    except Exception as exc:
        return None, f"{type(exc).__name__} {exc}"[:110]
    card = parse_card(html, str(card_id))
    if not card.get("name"):
        return None, f"カード名が取れず (HTML {len(html):,}文字)"
    return card, ""
