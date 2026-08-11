"""WordPress サイトから記事を取る共通処理。

ポケカブックは WP REST API (``/wp-json/``) が 403 で閉じられているため、
1つの経路に頼らず、上から順に試して最初に成功したものを使う。

    1. REST API   … 一番きれいに取れる。使えれば最良
    2. RSS フィード … 記事本文が content:encoded に入っていればそのまま使える
    3. 記事一覧HTML … 一覧から記事URLを拾い、各記事のHTMLを取りに行く

3 は取得回数が増えるぶん相手のサーバーに負担をかけるので、最後の手段。
どの経路で取れたかは呼び出し側にログとして返す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from src.pokeca import http

ARTICLE_LINK_RE = re.compile(r"/archives/(\d+)")


def get_text(url: str) -> str:
    """呼び出し側が http モジュールを直接触らずに済むようにする薄い包み。

    代入ではなく関数にしてあるのは、テストで http.get_text を
    差し替えたときにこちら経由の呼び出しにも効くようにするため。
    """
    return http.get_text(url)

# RSS の名前空間
NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# content:encoded がこれより短ければ抜粋とみなし、記事本文を取りに行く
FULL_CONTENT_MIN = 2000


@dataclass
class Post:
    """収集元ごとの差を吸収した記事1件。"""

    title: str
    content_html: str
    link: str
    published: date


class NoRouteAvailable(RuntimeError):
    """どの経路でも記事を取得できなかった。"""


def _parse_date(value: str) -> date:
    """ISO8601 か RFC822 の日付文字列を date にする。"""
    value = (value or "").strip()
    if not value:
        return date.today()
    try:
        return datetime.fromisoformat(value.replace("Z", "")).date()
    except ValueError:
        pass
    # RSS の pubDate: "Wed, 06 May 2026 17:01:30 +0900"
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return date.today()


def _plain(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


# ------------------------------------------------------------------
# 経路1: REST API
# ------------------------------------------------------------------


def posts_from_rest(api: str, slug: str, limit: int, search: str = "") -> list[Post]:
    category_id = None
    data = http.get_json(f"{api}/categories", params={"slug": slug})
    if isinstance(data, list) and data:
        category_id = data[0].get("id")

    params: dict = {"per_page": min(limit, 100), "orderby": "date", "order": "desc"}
    if category_id:
        params["categories"] = category_id
    elif search:
        params["search"] = search

    raw = http.get_json(f"{api}/posts", params=params)
    if not isinstance(raw, list):
        return []
    return [
        Post(
            title=_plain((p.get("title") or {}).get("rendered", "")),
            content_html=(p.get("content") or {}).get("rendered", ""),
            link=p.get("link") or "",
            published=_parse_date(p.get("modified") or p.get("date") or ""),
        )
        for p in raw
    ]


# ------------------------------------------------------------------
# 経路2: RSS フィード
# ------------------------------------------------------------------


def posts_from_feed(feed_url: str, limit: int) -> list[Post]:
    xml = http.get_text(feed_url)
    root = ElementTree.fromstring(xml)

    out: list[Post] = []
    for item in root.iter("item"):
        if len(out) >= limit:
            break
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        published = _parse_date(item.findtext("pubDate") or "")

        encoded = item.findtext("content:encoded", namespaces=NS) or ""
        if len(encoded) < FULL_CONTENT_MIN and link:
            # 抜粋しか入っていないので記事本文を取りに行く
            encoded = fetch_article_html(link)

        if link and encoded:
            out.append(Post(title=title, content_html=encoded, link=link, published=published))
    return out


# ------------------------------------------------------------------
# 経路3: 記事一覧HTML
# ------------------------------------------------------------------


def fetch_article_html(url: str) -> str:
    """記事ページを取得して本文部分だけ返す。"""
    post = fetch_article(url)
    return post.content_html if post else ""


def fetch_article(url: str) -> Post | None:
    """記事ページを取得して本文・タイトル・公開日を返す。"""
    soup = BeautifulSoup(http.get_text(url), "html.parser")
    content = soup.select_one(".entry-content")
    if not content:
        return None
    title_node = soup.find("h1") or soup.find("title")
    time_node = soup.find("time")
    return Post(
        title=title_node.get_text(" ", strip=True) if title_node else "",
        content_html=str(content),
        link=url,
        published=_parse_date((time_node.get("datetime") if time_node else "") or ""),
    )


def _article_links(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not ARTICLE_LINK_RE.search(href):
            continue
        if href.startswith("/"):
            href = base.rstrip("/") + href
        if href not in links:
            links.append(href)
    return links


def posts_from_index(index_url: str, base: str, limit: int) -> list[Post]:
    """一覧ページから記事URLを拾い、記事ごとにHTMLを取得する。"""
    index_html = http.get_text(index_url)
    out: list[Post] = []
    for link in _article_links(index_html, base)[:limit]:
        try:
            soup = BeautifulSoup(http.get_text(link), "html.parser")
        except Exception:
            continue
        content = soup.select_one(".entry-content")
        if not content:
            continue
        title_node = soup.find("h1") or soup.find("title")
        time_node = soup.find("time")
        published = _parse_date(
            (time_node.get("datetime") if time_node else "") or ""
        )
        out.append(
            Post(
                title=title_node.get_text(" ", strip=True) if title_node else "",
                content_html=str(content),
                link=link,
                published=published,
            )
        )
    return out


# ------------------------------------------------------------------
# 経路の自動選択
# ------------------------------------------------------------------


def fetch_posts(
    *,
    api: str,
    slug: str,
    feed_url: str,
    index_url: str,
    base: str,
    limit: int,
    search: str = "",
    log=None,
) -> list[Post]:
    """使える経路を上から順に試し、最初に記事が取れたものを返す。"""
    routes = (
        ("REST API", lambda: posts_from_rest(api, slug, limit, search)),
        ("RSS", lambda: posts_from_feed(feed_url, limit)),
        ("記事一覧HTML", lambda: posts_from_index(index_url, base, limit)),
    )

    problems: list[str] = []
    for name, fetch in routes:
        try:
            posts = fetch()
        except Exception as exc:
            problems.append(f"{name}: {exc}")
            continue
        if posts:
            if log:
                log(f"  経路: {name} ({len(posts)} 記事)")
            return posts
        problems.append(f"{name}: 0件")

    raise NoRouteAvailable(" / ".join(problems))
