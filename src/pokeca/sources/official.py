"""公式プレイヤーズクラブ (players.pokemon-card.com) からリーグ区分を補う。

当初はデッキコードを取るために使う想定だったが、実際にはポケカブックの
記事に公式デッキコードがそのまま載っていたため、その役目は不要になった。

現在の役割は **リーグ区分 (オープン / シニア / ジュニア) を補うこと** だけ。
シティリーグはリーグごとに別イベントとして開催されるが、ポケカブックの
まとめ記事にはリーグ表記が無い。8歳の子どもにとってはジュニアリーグの
結果が一番参考になるので、区別できるようにしておきたい。

ポケカブック側が各店舗の「大会結果」リンク (公式イベントURL) を持っているので、
そこを辿ってページ内のリーグ表記を読むだけで済む。

⚠️ 公式サイトのHTML構造は未検証 (開発時に接続できなかった)。
そのためリーグ補完は既定で無効にしてあり、``collect --with-league`` を
付けたときだけ動く。失敗しても本体の収集は止まらない。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from src.pokeca import http

BASE = "https://players.pokemon-card.com"
SOURCE_NAME = "official"

# 「シティリーグ2026 シーズン3 ジュニアリーグ」から区分を取る。
# マスターは順に見て最初に当たったものを採用するので、
# 「オープン」より先に来ないよう並び順に注意する。
LEAGUE_WORDS = ("ジュニア", "シニア", "マスター", "オープン")


def extract_league(html: str) -> str:
    """イベントページのHTMLからリーグ区分を取り出す。見つからなければ空文字。"""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(["h1", "h2", "title"])
    scope = heading.get_text(" ", strip=True) if heading else ""
    if not scope:
        scope = soup.get_text(" ", strip=True)[:600]
    for word in LEAGUE_WORDS:
        if re.search(word + r"\s*リーグ", scope) or word in scope:
            return word
    return ""


def fetch_league(event_url: str) -> str:
    """公式イベントページを1件見に行ってリーグ区分を返す。

    取得に失敗しても例外は投げない。補完は「取れたらうれしい」程度の情報なので、
    ここで収集全体を止めないほうがよい。
    """
    if not event_url:
        return ""
    try:
        return extract_league(http.get_text(event_url))
    except Exception:
        return ""
