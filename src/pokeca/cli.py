"""シティリーグの優勝・準優勝デッキを集めて子ども向けページを作る CLI。

    python -m src.pokeca.cli collect          # 収集して results.json を更新
    python -m src.pokeca.cli build            # site/index.html を生成
    python -m src.pokeca.cli list --rank 1    # 親がターミナルで確認する用
    python -m src.pokeca.cli rank --days 7    # ランキングを表示
    python -m src.pokeca.cli sample           # サンプルデータを入れて動作確認
    python -m src.pokeca.cli inspect --source pokecabook   # 生データを保存
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from src.pokeca import aggregate, site
from src.pokeca.models import DeckResult
from src.pokeca.store import (
    POKECA_DIR,
    RESULTS_FILE,
    SITE_DIR,
    apply_aliases,
    load_results,
    merge_results,
    now_jst,
    prune_results,
    sanitize_results,
    save_results,
)

console = Console()

INSPECT_DIR = POKECA_DIR / "_inspect"

# デッキ別ページは1記事あたり数百件の結果を含むので、記事数はこれくらいでよい
DECK_PAGE_LIMIT = 80


def _load_source(name: str):
    if name == "pokecabook":
        from src.pokeca.sources import pokecabook

        return pokecabook
    if name == "deckindex":
        from src.pokeca.sources import deckindex

        return deckindex
    raise click.BadParameter(f"不明な収集元: {name}")


# 結果そのものを持ってくる収集元。
#   pokecabook = シティリーグのまとめ記事 (順位・店舗・デッキコード)
#   deckindex  = デッキ別ページ (デッキ名付きのジムバトル優勝 + 名前の索引)
# 公式サイト (official) はリーグ区分を補うだけなのでここには含めない。
SOURCE_NAMES = ("pokecabook", "deckindex")


@click.group()
def main() -> None:
    """ポケカ シティリーグ 優勝デッキまとめ"""


# ------------------------------------------------------------------
# collect
# ------------------------------------------------------------------


@main.command("collect")
@click.option(
    "--source",
    type=click.Choice([*SOURCE_NAMES, "all"]),
    default="all",
    help="収集元。既定はすべて。",
)
@click.option("--limit", default=20, help="1回に見に行く記事・イベント数")
@click.option("--dry-run", is_flag=True, help="保存せず結果だけ表示")
@click.option(
    "--with-league",
    is_flag=True,
    help="公式イベントページを辿ってリーグ区分(オープン/シニア/ジュニア)を補う",
)
@click.option("--keep-days", default=180, help="何日ぶん残すか (0 で無制限)")
@click.option(
    "--deck-batch",
    default=25,
    help="1回に見に行くデッキ別ページの数 (0 で全部)。数日かけて一周する",
)
def cmd_collect(
    source: str,
    limit: int,
    dry_run: bool,
    with_league: bool,
    keep_days: int,
    deck_batch: int,
) -> None:
    """新着の優勝・準優勝デッキを集めて results.json に追記する。"""
    targets = SOURCE_NAMES if source == "all" else (source,)
    collected: list[DeckResult] = []
    failures: list[str] = []

    for name in targets:
        module = _load_source(name)
        console.print(f"[cyan]{name}[/cyan] から収集中...")
        kwargs: dict = {"log": console.print}
        if name == "deckindex":
            # デッキ別ページは1枚が大きいので、日ごとに区切って巡回する
            kwargs["limit"] = DECK_PAGE_LIMIT
            kwargs["batch"] = deck_batch
        else:
            kwargs["limit"] = limit
        try:
            found = module.collect(**kwargs)
        except Exception as exc:  # 片方が落ちても、もう片方は生かす
            failures.append(f"{name}: {exc}")
            console.print(f"  [red]失敗[/red]: {exc}")
            continue
        stamp = now_jst().isoformat(timespec="seconds")
        for record in found:
            record.collected_at = stamp
        console.print(f"  {len(found)} 件")
        collected.extend(found)

    if not collected:
        console.print("[yellow]新しく取れたデータがありません。[/yellow]")
        if failures:
            console.print("[red]エラー:[/red] " + " / ".join(failures))
            sys.exit(1)
        return

    if with_league:
        from src.pokeca.sources import official

        # 同じイベントURLを何度も叩かないようキャッシュする
        cache: dict[str, str] = {}
        for record in collected:
            if record.league or not record.event_url:
                continue
            if record.event_url not in cache:
                cache[record.event_url] = official.fetch_league(record.event_url)
            record.league = cache[record.event_url]
        found = sum(1 for r in collected if r.league)
        console.print(f"リーグ区分を補完: {found}/{len(collected)} 件")

    collected = apply_aliases(collected)
    existing = load_results()

    # 本物のデータが取れたら、動作確認用のサンプルは役目を終える。
    # 混ざったまま集計するとランキングが嘘になるので捨てる。
    samples = [r for r in existing if r.source == "sample"]
    if samples:
        existing = [r for r in existing if r.source != "sample"]
        console.print(f"[dim]サンプルデータ {len(samples)} 件を破棄しました。[/dim]")

    merged, added, updated = merge_results(existing, collected)

    console.print(
        f"[green]新規 {added} 件 / 補強 {updated} 件[/green] (合計 {len(merged)} 件)"
    )
    merged, fixed = sanitize_results(merged)
    if fixed:
        console.print(f"[dim]おかしいデッキ名 {fixed} 件を空にしました。[/dim]")

    named = sum(1 for r in merged if r.deck_name)
    console.print(f"デッキ名あり: {named}/{len(merged)} 件")
    events = Counter(r.event_type for r in merged)
    console.print(
        f"内訳: シティリーグ {events.get('city', 0)} 件 / "
        f"ジムバトル {events.get('gym', 0)} 件 / "
        f"デッキ {len({r.deck_key for r in merged if r.deck_name})} 種類"
    )

    before = len(merged)
    merged = prune_results(merged, keep_days=keep_days)
    if len(merged) < before:
        console.print(f"[dim]{before - len(merged)} 件の古いデータを削除しました。[/dim]")

    if dry_run:
        console.print("[yellow]--dry-run のため保存しませんでした。[/yellow]")
        return

    save_results(merged)
    console.print(f"保存しました: {RESULTS_FILE}")

    if failures:
        console.print("[yellow]一部の収集元で失敗しています:[/yellow] " + " / ".join(failures))


# ------------------------------------------------------------------
# build
# ------------------------------------------------------------------


@main.command("build")
@click.option("--out", type=click.Path(path_type=Path), default=None, help="出力先HTML")
@click.option("--body-only", is_flag=True, help="body の中身だけ出力する")
def cmd_build(out: Path | None, body_only: bool) -> None:
    """results.json から子ども向けページを生成する。"""
    results = load_results()
    is_sample = bool(results) and all(r.source == "sample" for r in results)
    data = site.build_data(results, is_sample=is_sample)
    html = site.build_html(data, standalone=not body_only)

    target = out or (SITE_DIR / "index.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")

    console.print(f"[green]生成しました[/green]: {target} ({len(html):,} バイト)")
    console.print(f"  結果 {len(results)} 件 / デッキ {len(data['decks'])} 種類")
    if is_sample:
        console.print("[yellow]※ サンプルデータで生成しています。[/yellow]")


# ------------------------------------------------------------------
# list / rank
# ------------------------------------------------------------------


@main.command("list")
@click.option("--rank", type=click.Choice(["1", "2", "all"]), default="all")
@click.option("--event", type=click.Choice(["city", "gym", "all"]), default="all")
@click.option("--deck", default="", help="デッキ名で絞り込み (部分一致)")
@click.option("--limit", default=30)
def cmd_list(rank: str, event: str, deck: str, limit: int) -> None:
    """保存済みの結果を一覧表示する (親の確認用)。"""
    results = load_results()
    if rank != "all":
        results = [r for r in results if r.rank == int(rank)]
    if event != "all":
        results = [r for r in results if r.event_type == event]
    if deck:
        results = [r for r in results if deck.lower() in r.deck_name.lower()]

    if not results:
        console.print("[yellow]該当するデータがありません。[/yellow]")
        return

    scope = {"city": "シティリーグ", "gym": "ジムバトル"}.get(event, "すべての大会")
    table = Table(title=f"{scope} 結果 ({len(results)} 件)")
    table.add_column("開催日", style="cyan")
    table.add_column("大会", style="dim")
    table.add_column("順位")
    table.add_column("デッキ", style="bold")
    table.add_column("店舗", style="dim")
    table.add_column("コード", style="dim")

    for record in results[:limit]:
        table.add_row(
            record.date,
            record.event_label,
            record.rank_label,
            record.deck_name or "[dim](名前なし)[/dim]",
            f"{record.prefecture} {record.store}".strip() or "-",
            record.deck_code or "-",
        )
    console.print(table)


@main.command("rank")
@click.option("--days", default=7, help="集計期間 (0 で全期間)")
@click.option("--event", type=click.Choice(["city", "gym", "all"]), default="all")
def cmd_rank(days: int, event: str) -> None:
    """デッキ別の優勝回数ランキングを表示する。"""
    results = load_results()
    if event != "all":
        results = [r for r in results if r.event_type == event]
    ranked = aggregate.deck_ranking(results, days=days)
    if not ranked:
        console.print("[yellow]集計できるデータがありません。[/yellow]")
        return

    label = "全期間" if days <= 0 else f"直近 {days} 日"
    scope = {"city": "シティリーグ", "gym": "ジムバトル"}.get(event, "すべての大会")
    table = Table(title=f"デッキランキング ({scope} / {label})")
    table.add_column("#", justify="right")
    table.add_column("デッキ", style="bold")
    table.add_column("優勝", justify="right", style="yellow")
    table.add_column("準優勝", justify="right", style="cyan")

    for entry in ranked[:20]:
        table.add_row(
            str(entry["position"]),
            entry["deck_name"],
            str(entry["first"]),
            str(entry["second"]),
        )
    console.print(table)


# ------------------------------------------------------------------
# probe
# ------------------------------------------------------------------

PROBE_TARGETS = [
    ("robots.txt", "https://pokecabook.com/robots.txt"),
    ("REST: カテゴリ", "https://pokecabook.com/wp-json/wp/v2/categories?slug=city-league"),
    ("REST: 記事", "https://pokecabook.com/wp-json/wp/v2/posts?per_page=1"),
    ("RSS: サイト全体", "https://pokecabook.com/feed/"),
    ("RSS: シティリーグ", "https://pokecabook.com/archives/category/tournament/city-league/feed/"),
    ("RSS: デッキ別", "https://pokecabook.com/archives/category/deck-recipe/feed/"),
    ("HTML: 一覧", "https://pokecabook.com/archives/category/tournament/city-league"),
    ("HTML: 記事", "https://pokecabook.com/archives/320777"),
    ("HTML: デッキ一覧", "https://pokecabook.com/archives/1417"),
]


@main.command("probe")
def cmd_probe() -> None:
    """どの取得経路が使えるかを調べる。

    REST API が閉じられている等で収集できないとき、これを実行すると
    どこまで届いているのかが1回で分かる。
    """
    import requests

    from src.pokeca import http

    table = Table(title="取得経路の疎通確認")
    table.add_column("経路")
    table.add_column("状態", justify="right")
    table.add_column("種類", style="dim")
    table.add_column("サイズ", justify="right", style="dim")

    for label, url in PROBE_TARGETS:
        try:
            response = http.get(url, respect_robots=False)
            status = f"[green]{response.status_code}[/green]"
            kind = (response.headers.get("Content-Type") or "").split(";")[0]
            size = f"{len(response.content):,}"
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            status, kind, size = f"[red]{code}[/red]", "-", "-"
        except Exception as exc:
            status, kind, size = "[red]失敗[/red]", type(exc).__name__, "-"
        table.add_row(label, status, kind, size)

    console.print(table)
    console.print(
        "\n[dim]200 が並んでいる経路が使えます。"
        "collect は REST → RSS → HTML の順に自動で切り替えます。[/dim]"
    )


# ------------------------------------------------------------------
# healthcheck
# ------------------------------------------------------------------


@main.command("healthcheck")
@click.option("--max-age-days", default=10, help="最新データがこの日数より古ければ異常")
def cmd_healthcheck(max_age_days: int) -> None:
    """収集が止まっていないか確認する (異常なら終了コード 1)。

    収集元のHTML構造が変わるとパーサーが黙って空振りする。
    「エラーは出ないのにデータだけ増えない」状態に気づくための番人。
    """
    results = [r for r in load_results() if r.source != "sample"]
    if not results:
        console.print("[red]本物のデータが1件もありません。[/red]")
        sys.exit(1)

    latest = max(r.date for r in results if r.date)
    try:
        age = (date.today() - date.fromisoformat(latest)).days
    except ValueError:
        console.print(f"[red]最新日付を解釈できません: {latest}[/red]")
        sys.exit(1)

    console.print(f"最新の開催日: {latest} ({age} 日前) / 全 {len(results)} 件")
    if age > max_age_days:
        console.print(
            f"[red]{max_age_days} 日以上更新されていません。"
            "収集元の構造が変わった可能性があります。[/red]"
        )
        sys.exit(1)
    console.print("[green]正常です。[/green]")


# ------------------------------------------------------------------
# inspect
# ------------------------------------------------------------------


@main.command("inspect")
@click.option("--source", type=click.Choice([*SOURCE_NAMES, "official"]), default="pokecabook")
@click.option("--url", default="", help="個別ページを直接指定したいとき")
def cmd_inspect(source: str, url: str) -> None:
    """収集元の生データを保存して構造を確認する。

    パーサーが空振りしたときは、まずこれを実行して
    data/pokeca/_inspect/ に落ちたファイルを開いて構造を確かめる。
    """
    from src.pokeca import http

    INSPECT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_jst().strftime("%Y%m%d-%H%M%S")

    if url:
        text = http.get_text(url)
        path = INSPECT_DIR / f"{source}-{stamp}.html"
        path.write_text(text, encoding="utf-8")
        console.print(f"保存: {path} ({len(text):,} 文字)")
        return

    if source in SOURCE_NAMES:
        module = _load_source(source)
        fetch = getattr(module, "fetch_posts", None) or module.fetch_deck_posts
        posts = fetch(limit=3, log=console.print)
        for index, post in enumerate(posts, start=1):
            path = INSPECT_DIR / f"{source}-{stamp}-{index}.html"
            path.write_text(post.content_html, encoding="utf-8")
            console.print(
                f"保存: {path.name} ({len(post.content_html):,} 文字) "
                f"[dim]{post.published} {post.title[:40]}[/dim]"
            )
        if not posts:
            console.print("[yellow]記事を取得できませんでした。probe を試してください。[/yellow]")
        return

    # 公式サイトは収集済みレコードが持つイベントURLを1件だけ見に行く
    events = [r.event_url for r in load_results() if r.event_url]
    if not events:
        console.print(
            "[yellow]イベントURLを持つレコードがありません。"
            "先に collect を実行してください。[/yellow]"
        )
        return
    target = events[0]
    text = http.get_text(target)
    path = INSPECT_DIR / f"official-event-{stamp}.html"
    path.write_text(text, encoding="utf-8")
    console.print(f"保存: {path} ({len(text):,} 文字) <- {target}")


# ------------------------------------------------------------------
# sample
# ------------------------------------------------------------------

SAMPLE_STORES = [
    ("東京", "バトロコ高田馬場"),
    ("岐阜", "宝島 岐阜本店"),
    ("香川", "ゲームアーク 丸亀店"),
    ("広島", "カードボックス 福山店"),
    ("福井", "Super KaBoS + GEO 鯖江店"),
    ("新潟", "トイニティ長岡E・PLAZA店"),
    ("大阪", "カードキングダム 難波店"),
    ("北海道", "ホビーステーション 札幌店"),
]

SAMPLE_DECKS = [
    "ドラパルトex",
    "リザードンex",
    "サーナイトex",
    "パオジアンex",
    "ミライドンex",
    "ロストバレット",
    "ピジョットex",
    "タケルライコex",
]

SAMPLE_LEAGUES = ["オープン", "オープン", "オープン", "シニア", "ジュニア"]


@main.command("sample")
@click.option("--days", default=21, help="何日ぶんのサンプルを作るか")
@click.option("--force", is_flag=True, help="既存の results.json を上書きする")
def cmd_sample(days: int, force: bool) -> None:
    """動作確認用のサンプルデータを作る。

    本物の収集が動くまでのあいだ、子どもにページを見せて
    使い勝手を確かめるためのダミーデータ。source は "sample" にしてあるので
    本物のデータと混ざっても後から見分けられる。
    """
    existing = load_results()
    if existing and not force:
        real = [r for r in existing if r.source != "sample"]
        if real:
            console.print(
                "[red]本物のデータが入っています。"
                "上書きするなら --force を付けてください。[/red]"
            )
            sys.exit(1)

    rng = random.Random(20260811)
    today = date.today()
    records: list[DeckResult] = []
    stamp = now_jst().isoformat(timespec="seconds")

    for offset in range(days):
        held = today - timedelta(days=offset)
        # 平日は開催が少なく、土日に集中する実態にざっくり寄せる
        count = rng.choice([0, 1, 1, 2]) if held.weekday() < 5 else rng.choice([3, 4, 5])
        for _ in range(count):
            pref, store = rng.choice(SAMPLE_STORES)
            league = rng.choice(SAMPLE_LEAGUES)
            picked = rng.sample(SAMPLE_DECKS, 2)
            for rank, deck in enumerate(picked, start=1):
                records.append(
                    DeckResult(
                        date=held.isoformat(),
                        store=store,
                        rank=rank,
                        deck_name=deck,
                        prefecture=pref,
                        league=league,
                        source="sample",
                        source_url="https://pokecabook.com/",
                        collected_at=stamp,
                    )
                )

    # 同じ店・同じ日・同じリーグが重複したぶんは落とす
    deduped: dict[str, DeckResult] = {r.slot_id: r for r in records}
    save_results(list(deduped.values()))
    console.print(f"[green]サンプル {len(deduped)} 件を作成しました[/green]: {RESULTS_FILE}")
    console.print("次は: python -m src.pokeca.cli build")


if __name__ == "__main__":
    main()
