"""行儀のよい HTTP 取得。

他人のサイトからデータを頂く以上、最低限のマナーは機械側で強制しておく。

- robots.txt を必ず確認し、禁止されているパスは取りに行かない
- 同一ホストへの連続アクセスは既定 1.5 秒あける
- User-Agent に用途と連絡先を明示する
- 失敗しても諦める (リトライは控えめに)
"""

from __future__ import annotations

import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = (
    "SpecialAigent-PokecaBot/1.0 "
    "(personal, non-commercial; 1 request/1.5s; "
    "+https://github.com/macsa2ki25-eng/SpecialAigent)"
)

# ホストごとの最終アクセス時刻
_last_access: dict[str, float] = {}
_robots_cache: dict[str, RobotFileParser | None] = {}

DEFAULT_DELAY = 1.5
DEFAULT_TIMEOUT = 20


class FetchBlocked(RuntimeError):
    """robots.txt により取得が禁止されている。"""


def _robots_for(url: str) -> RobotFileParser | None:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin in _robots_cache:
        return _robots_cache[origin]

    parser = RobotFileParser()
    parser.set_url(f"{origin}/robots.txt")
    try:
        parser.read()
    except Exception:
        # robots.txt が読めないときは「許可されている」と決めつけない。
        # ただし取得自体は止めない (多くのサイトは robots.txt を置いていない)。
        parser = None
    _robots_cache[origin] = parser
    return parser


def can_fetch(url: str) -> bool:
    parser = _robots_for(url)
    if parser is None:
        return True
    return parser.can_fetch(USER_AGENT, url)


def _throttle(url: str, delay: float) -> None:
    host = urlparse(url).netloc
    previous = _last_access.get(host)
    if previous is not None:
        elapsed = time.monotonic() - previous
        if elapsed < delay:
            time.sleep(delay - elapsed)
    _last_access[host] = time.monotonic()


def get(
    url: str,
    *,
    params: dict | None = None,
    delay: float = DEFAULT_DELAY,
    timeout: int = DEFAULT_TIMEOUT,
    respect_robots: bool = True,
) -> requests.Response:
    """GET する。robots.txt で禁止なら FetchBlocked を投げる。"""
    if respect_robots and not can_fetch(url):
        raise FetchBlocked(f"robots.txt により取得が許可されていません: {url}")
    _throttle(url, delay)
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def get_json(url: str, **kwargs) -> object:
    return get(url, **kwargs).json()


def get_text(url: str, **kwargs) -> str:
    response = get(url, **kwargs)
    response.encoding = response.apparent_encoding or response.encoding
    return response.text
