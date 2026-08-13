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
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import click
from bs4 import BeautifulSoup
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
    from src.pokeca.cardstore import load_cards, load_decklists

    results = load_results()
    is_sample = bool(results) and all(r.source == "sample" for r in results)
    # デッキの中身をまだ持っていなければ、従来どおり一覧とランキングだけになる
    data = site.build_data(
        results,
        is_sample=is_sample,
        decklists=load_decklists(),
        cards=load_cards(),
    )
    html = site.build_html(data, standalone=not body_only)

    target = out or (SITE_DIR / "index.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")

    console.print(f"[green]生成しました[/green]: {target} ({len(html):,} バイト)")
    console.print(f"  結果 {len(results)} 件 / デッキ {len(data['decks'])} 種類")
    inside = data.get("contents") or {}
    if inside:
        console.print(
            f"  なかみを のせたデッキ {len(inside)} 種類 / "
            f"カード索引 {len(data.get('cardDecks') or {})} 枚"
        )
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
# デッキの中身を見る
# ------------------------------------------------------------------


def _load_corpus():
    """大会結果と60枚の中身を突き合わせたものを返す。"""
    from src.pokeca import analysis
    from src.pokeca.cardstore import load_cards, load_decklists

    decklists = load_decklists()
    if not decklists:
        console.print(
            "[yellow]デッキの中身をまだ持っていません。"
            "先に fetch-decks を実行してください。[/yellow]"
        )
        sys.exit(1)
    return analysis.build_corpus(load_results(), decklists, load_cards())


def _resolve_deck(corpus, name: str):
    """デッキ名の一部から、対象のデッキ群を絞る。

    子どもが「ドラパルト」とだけ打っても引けるように、部分一致で探す。
    """
    from src.pokeca import analysis

    keys = {d.deck_key: d.deck_name for d in corpus}
    hits = [k for k in keys if name in k or name in keys[k]]
    if not hits:
        console.print(f"[yellow]「{name}」に当てはまるデッキがありません。[/yellow]")
        counts = Counter(d.deck_name for d in corpus)
        console.print("[dim]例: " + " / ".join(n for n, _ in counts.most_common(8)) + "[/dim]")
        sys.exit(1)
    # 一番件数の多いものを採る
    counts = Counter(d.deck_key for d in corpus if d.deck_key in hits)
    key = counts.most_common(1)[0][0]
    return key, keys[key], analysis.select(corpus, deck_key=key)


def _share(row: dict) -> str:
    return f"{row['decks']}/{row['total']} ({row['share'] * 100:.0f}%)"


@main.command("cards")
@click.argument("deck_name")
@click.option("--event", type=click.Choice(["city", "gym", "all"]), default="all")
def cmd_cards(deck_name: str, event: str) -> None:
    """あるデッキ名の中身が、勝っているデッキ同士でどう違うかを見る。

    「ほぼ全部に入っているカード」と「入れる人と入れない人がいるカード」を
    分けて出す。同じドラパルトでも中身がどう違うのか、はここに出る。
    """
    from src.pokeca import analysis

    corpus = _load_corpus()
    if event != "all":
        corpus = analysis.select(corpus, event_type=event)
    key, label, decks = _resolve_deck(corpus, deck_name)

    groups = analysis.core_and_flex(analysis.adoption(decks))
    console.print(f"\n[bold]{label}[/bold]  {len(decks)} デッキぶんの中身から\n")
    for title, rows in groups.items():
        if not rows:
            continue
        table = Table(title=title)
        table.add_column("カード", style="bold")
        table.add_column("入れているデッキ", justify="right")
        table.add_column("平均", justify="right", style="cyan")
        table.add_column("枚数の内訳", style="dim")
        for row in rows[:30]:
            spread = " ".join(f"{n}枚:{c}件" for n, c in row["distribution"].items())
            table.add_row(row["name"], _share(row), f"{row['average']:.1f}", spread)
        console.print(table)


@main.command("decks-with")
@click.argument("card_name")
def cmd_decks_with(card_name: str) -> None:
    """あるカードを使っているデッキを探す。

    「マシマシラを使ったデッキにはどんなデッキがあるのか」に答える。
    """
    from src.pokeca import analysis

    corpus = _load_corpus()
    index = analysis.card_index(corpus)
    hits = analysis.find_cards(index, card_name)
    if not hits:
        console.print(f"[yellow]「{card_name}」を使っているデッキはありません。[/yellow]")
        return

    for name, data in hits[:5]:
        table = Table(title=f"{name}  ({data['decks']} デッキ / 平均 {data['average']:.1f}枚)")
        table.add_column("デッキ", style="bold")
        table.add_column("件数", justify="right")
        for deck_name, count in list(data["archetypes"].items())[:15]:
            table.add_row(deck_name, str(count))
        console.print(table)


@main.command("variants")
@click.argument("deck_name")
def cmd_variants(deck_name: str) -> None:
    """同じデッキ名の中にある「型」を、分かれ目のカードで分けて出す。"""
    from src.pokeca import analysis

    corpus = _load_corpus()
    key, label, decks = _resolve_deck(corpus, deck_name)
    groups = analysis.variants(decks)
    if not groups:
        console.print(
            f"[yellow]{label} は {len(decks)} デッキしか無いか、"
            "中身がほとんど同じで、型に分けられません。[/yellow]"
        )
        return

    table = Table(title=f"{label} の型  ({len(decks)} デッキ)")
    table.add_column("入っているカード", style="bold")
    table.add_column("件数", justify="right")
    table.add_column("割合", justify="right", style="cyan")
    table.add_column("デッキコードの例", style="dim")
    for group in groups:
        table.add_row(
            " + ".join(group["cards"]) or "(どれも入れない)",
            str(group["decks"]),
            f"{group['share'] * 100:.0f}%",
            " ".join(group["examples"]),
        )
    console.print(table)

    left = analysis.variant_coverage(decks, groups)
    if left:
        console.print(
            f"[dim]残り {left} デッキは、この分け方ではどれにも当てはまらなかった。"
            "型はこれで全部、という意味ではない。[/dim]"
        )


@main.command("check")
@click.argument("deck_code")
@click.option("--against", default="", help="比べる相手のデッキ名 (省略すると一番近いものを探す)")
def cmd_check(deck_code: str, against: str) -> None:
    """自分のデッキを、勝っているデッキと見比べる。

    公式サイトで作ったデッキのコードを渡すと、そのデッキと勝っている
    デッキの中身の違いを並べる。

    強い・弱いは言わない。勝っているデッキが何を入れているか、自分と
    どこが違うかを出すところまで。そこから先は本人が決めること。
    """
    from src.pokeca import analysis
    from src.pokeca.cardstore import expand, load_cards, load_decklists
    from src.pokeca.sources import official_deck

    cards = load_cards()
    mine = load_decklists().get(deck_code)
    if mine:
        # 保存済みのデッキはIDと枚数だけなので、カード表で名前を補う
        entries = expand(mine, cards)
    else:
        fetched, reason = official_deck.fetch_decklist(deck_code)
        if not fetched:
            console.print(f"[red]デッキを読めませんでした[/red]: {reason}")
            sys.exit(1)
        entries = fetched["cards"]

    counts: dict[str, int] = {}
    for card in entries:
        if card.get("name"):
            counts[card["name"]] = counts.get(card["name"], 0) + card.get("count", 0)

    corpus = _load_corpus()
    if against:
        key, label, decks = _resolve_deck(corpus, against)
    else:
        # 同じカードを一番多く共有しているデッキ名を相手に選ぶ
        overlap: Counter[str] = Counter()
        for deck in corpus:
            shared = sum(min(counts.get(n, 0), c) for n, c in deck.counts.items())
            overlap[deck.deck_key] = max(overlap[deck.deck_key], shared)
        if not overlap:
            console.print("[yellow]比べられるデッキがありません。[/yellow]")
            sys.exit(1)
        key = overlap.most_common(1)[0][0]
        label = next(d.deck_name for d in corpus if d.deck_key == key)
        decks = analysis.select(corpus, deck_key=key)

    report = analysis.compare(counts, decks)
    console.print(
        f"\n[bold]{deck_code}[/bold] を "
        f"[bold]{label}[/bold] の勝ちデッキ {report['total']} 件と見比べます\n"
    )

    if report["missing"]:
        table = Table(title="勝っているデッキによく入っていて、自分には無いカード")
        table.add_column("カード", style="bold")
        table.add_column("入れているデッキ", justify="right")
        table.add_column("平均", justify="right", style="cyan")
        for row in report["missing"][:20]:
            table.add_row(row["name"], _share(row), f"{row['average']:.1f}")
        console.print(table)

    if report["different"]:
        table = Table(title="枚数がちがうカード")
        table.add_column("カード", style="bold")
        table.add_column("自分", justify="right")
        table.add_column("勝ちデッキの平均", justify="right", style="cyan")
        for row in report["different"][:20]:
            table.add_row(row["name"], str(row["yours"]), f"{row['average']:.1f}")
        console.print(table)

    if report["extra"]:
        names = "  ".join(f"{r['name']}×{r['yours']}" for r in report["extra"][:20])
        console.print(f"\n[bold]自分だけが入れているカード[/bold]\n  {names}")
        console.print(
            "[dim]勝っているデッキに無いから弱い、ということではない。"
            "何のために入れたのかを説明できるかどうかが大事。[/dim]"
        )


# ------------------------------------------------------------------
# fetch-decks / fetch-cards
# ------------------------------------------------------------------


@main.command("fetch-decks")
@click.option("--limit", default=200, help="1回に取りに行くデッキ数 (0 で全部)")
def cmd_fetch_decks(limit: int) -> None:
    """優勝・準優勝デッキの中身 (60枚) を取得して保存する。

    デッキの中身は一度確定したら変わらないので、すでに持っているものは
    取りに行かない。初回だけ時間がかかり、以後は新着ぶんだけで済む。
    """
    from src.pokeca.cardstore import load_decklists, save_decklists
    from src.pokeca.sources import official_deck

    known = load_decklists()
    codes = [r.deck_code for r in load_results() if r.deck_code]
    todo = [c for c in dict.fromkeys(codes) if c not in known]
    if limit > 0:
        todo = todo[:limit]

    if not todo:
        console.print(f"[green]すべて取得済みです[/green] ({len(known)} デッキ)")
        return

    console.print(f"取得対象: {len(todo)} デッキ (保有 {len(known)})")
    console.print(f"[dim]1.5秒間隔なので約 {len(todo) * 1.5 / 60:.0f} 分かかります[/dim]")

    ok = failed = 0
    unconfirmed: set[str] = set()
    for index, code in enumerate(todo, start=1):
        deck, reason = official_deck.fetch_decklist(code)
        if deck:
            known[code] = deck
            unconfirmed.update(official_deck.unconfirmed_sections(deck))
            ok += 1
        else:
            failed += 1
            if failed <= 3:
                console.print(f"  [red]失敗[/red] {code}: {reason}")
        # 最初の10件が全滅なら、続けても同じことを繰り返すだけ。
        # 相手のサーバーに無駄なアクセスをかけないうちに止める。
        if index == 10 and ok == 0:
            console.print(
                "[red]10件連続で失敗したため中止します。"
                "上の理由を見て取得方法を直してください。[/red]"
            )
            sys.exit(1)
        if index % 25 == 0 or index == len(todo):
            console.print(f"  {index}/{len(todo)}  成功 {ok} / 失敗 {failed}")
            save_decklists(known)  # 途中で落ちても取得ぶんは残す

    save_decklists(known)
    console.print(f"[green]完了[/green]: {ok} デッキを追加 (合計 {len(known)})")
    if failed:
        console.print(f"[yellow]{failed} デッキは60枚揃わず取得できませんでした[/yellow]")
    if unconfirmed:
        # 公式の入力欄のうち意味を確かめていないものに、実際にカードが入っていた。
        # 区分名が仮のままなので、実物を見て直す手がかりを残す。
        console.print(
            f"[yellow]まだ意味の分からない欄にカードが入っていました: "
            f"{', '.join(sorted(unconfirmed))}[/yellow]"
        )
        console.print(
            "[dim]official_deck.SECTION_FIELDS の区分名を実物に合わせて直してください。[/dim]"
        )


@main.command("fetch-cards")
@click.option("--limit", default=300, help="1回に取りに行くカード数 (0 で全部)")
@click.option(
    "--refresh",
    is_flag=True,
    help="取得済みのカードも取り直す (読み取り方を直したときに使う)",
)
@click.option(
    "--gaps",
    is_flag=True,
    help="効果文にすき間があるカードだけ取り直す (マークが落ちているぶん)",
)
def cmd_fetch_cards(limit: int, refresh: bool, gaps: bool) -> None:
    """デッキに入っているカードの内容 (HP・ワザ・特性) を取得して保存する。

    公式のカードは数千枚あるが、取りに行くのは優勝デッキに実際に
    出てくるカードだけ。カードの内容は変わらないので取得は一度きり。
    """
    from src.pokeca.cardstore import load_cards, load_decklists, needs_detail, save_cards
    from src.pokeca.sources import official_card

    decklists = load_decklists()
    if not decklists:
        console.print("[yellow]先に fetch-decks を実行してください。[/yellow]")
        sys.exit(1)

    known = load_cards()
    # 名前だけ入っているカードは「取得済み」ではない。ワザや特性が
    # 入っているかどうかで判断する。
    if refresh:
        # 読み取り方を直したときは、取得済みでも中身が古い。全部取り直す。
        from src.pokeca.cardstore import card_ids_in

        todo = sorted(card_ids_in(decklists))
    elif gaps:
        # マークが落ちたカードだけ取り直す。全部取り直すより桁違いに速い。
        from src.pokeca.cardstore import gapped_ids

        todo = gapped_ids(decklists, known)
    else:
        todo = needs_detail(decklists, known)
    have = sum(1 for c in known.values() if c.get("detail"))
    if limit > 0:
        todo = todo[:limit]

    if not todo:
        console.print(f"[green]すべて取得済みです[/green] ({have} カード)")
        return

    console.print(f"取得対象: {len(todo)} カード (保有 {have})")
    console.print(f"[dim]1.5秒間隔なので約 {len(todo) * 1.5 / 60:.0f} 分かかります[/dim]")

    ok = failed = 0
    for index, card_id in enumerate(todo, start=1):
        card, reason = official_card.fetch_card(card_id)
        if card:
            # デッキページ経由で入っている情報 (収録セット・区分・画像) は
            # 消さずに残す
            known[card_id] = {**known.get(card_id, {}), **card, "detail": True}
            ok += 1
        else:
            failed += 1
            if failed <= 3:
                console.print(f"  [red]失敗[/red] {card_id}: {reason}")
        if index == 10 and ok == 0:
            console.print(
                "[red]10件連続で失敗したため中止します。"
                "上の理由を見て取得方法を直してください。[/red]"
            )
            sys.exit(1)
        if index % 25 == 0 or index == len(todo):
            console.print(f"  {index}/{len(todo)}  成功 {ok} / 失敗 {failed}")
            save_cards(known)

    save_cards(known)
    detailed = sum(1 for c in known.values() if c.get("detail"))
    console.print(f"[green]完了[/green]: {ok} カードを追加 (合計 {detailed})")
    if failed:
        console.print(
            f"[yellow]{failed} カードは取得できませんでした "
            "(次回の実行で取り直します)[/yellow]"
        )


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


# カードごとに1行ずつ並ぶ索引データ。件数だけ分かればよく、全部出すと
# ログが埋まるので折りたたむ。
INDEX_ASSIGN_RE = re.compile(r"^\s*PCGDECK\.(\w+)\[\s*(\d+)\s*\]\s*=")
WRAP_WIDTH = 165


def _emit(text: str, indent: str = "  ") -> None:
    """長い行を折り返して素の print で出す。

    rich は角括弧をマークアップとして解釈してしまううえ、幅で切り捨てる。
    調査結果は1文字も落としたくないので、ここだけは console を使わない。
    """
    for i in range(0, len(text), WRAP_WIDTH):
        print(f"{indent}{text[i : i + WRAP_WIDTH]}")


def _dump_scripts(html: str) -> None:
    """<script> を順に出す。索引データだけは件数にまとめる。"""
    soup = BeautifulSoup(html, "html.parser")
    for n, tag in enumerate(soup.find_all("script"), 1):
        src = tag.get("src")
        body = tag.string or tag.get_text() or ""
        if src:
            print(f"\n-- script {n}: src={src}")
            continue
        if not body.strip():
            continue
        print(f"\n-- script {n}: インライン {len(body):,} 文字")

        folded: Counter[str] = Counter()
        samples: dict[str, str] = {}
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            match = INDEX_ASSIGN_RE.match(line)
            if match:
                # PCGDECK.searchItemName[47847]='...' のような索引行
                folded[match.group(1)] += 1
                samples.setdefault(match.group(1), stripped)
                continue
            _emit(stripped)
        for name, count in folded.most_common():
            print(f"  [索引 {count}件] PCGDECK.{name}[...] 例:")
            _emit(samples[name][:400], indent="      ")


def _dump_form_fields(html: str) -> None:
    """input / textarea / select をそのまま出す。

    JSが組み立てる画面でも、元になる値はたいてい hidden で埋まっている。
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["input", "textarea", "select"]):
        attrs = " ".join(f'{k}="{v}"' for k, v in tag.attrs.items())
        _emit(f"<{tag.name} {attrs}>")
        inner = tag.get_text(strip=True)
        if inner:
            _emit(f"    中身({len(inner)}文字): {inner[:600]}", indent="  ")


@main.command("dump-official")
@click.option("--deck-code", default="", help="調べるデッキコード (既定は保存済みの先頭)")
def cmd_dump_official(deck_code: str) -> None:
    """公式のデッキページの素のレスポンスを、そのまま調べて表示する。

    ブラウザで保存したHTMLはJavaScript実行後のDOMなので、実際に返ってくる
    HTMLとは別物。カード表がJSで組み立てられている場合、データの在り処は
    素のHTMLの中にある。それを突き止めるための調査用。

    前回の調査で「外部APIは無く、カード名とIDは素のHTMLに直接書かれている」
    ことまでは分かった。足りないのは **枚数** なので、インラインJSと
    フォームの値を余さず出す。
    """
    from src.pokeca import http
    from src.pokeca.sources import official_deck

    if not deck_code:
        codes = [r.deck_code for r in load_results() if r.deck_code]
        deck_code = codes[0] if codes else ""
    if not deck_code:
        console.print("[yellow]デッキコードがありません。[/yellow]")
        sys.exit(1)

    routes = [
        ("result.html", official_deck.deck_url(deck_code)),
        # 「デッキ一覧画像を表示する」ボタンの飛び先。
        # サーバ側で組み立てていれば、こちらの方が素直に読める。
        ("thumbs.html", f"{official_deck.BASE}/deck/thumbs.html/deckID/{deck_code}/"),
    ]

    for label, url in routes:
        print(f"\n{'=' * 70}\n=== {label}  {url}\n{'=' * 70}")
        try:
            html = http.get_text(url, respect_robots=False)
        except Exception as exc:
            print(f"  取得できず: {type(exc).__name__} {exc}")
            continue
        print(f"HTML {len(html):,} 文字 / cardName_ {html.count('cardName_')}箇所 / 「枚」 {html.count('枚')}箇所")

        print("\n■ インラインJS")
        _dump_scripts(html)

        print("\n■ フォームの値 (input / textarea / select)")
        _dump_form_fields(html)

        path = INSPECT_DIR / f"official-{label}-{deck_code}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        print(f"\n全文を保存: {path}")


@main.command("dump-card")
@click.option("--card-id", default="50452", help="調べるカードID")
def cmd_dump_card(card_id: str) -> None:
    """カード詳細ページが素のGETで読めるかを確かめる。

    デッキの中身とカードの内容は別々の経路。片方が駄目でも、もう片方は
    生きているかもしれないので、分けて確認できるようにしておく。
    """
    import json

    from src.pokeca import http
    from src.pokeca.sources import official_card

    url = official_card.card_url(card_id)
    print(f"=== カード詳細 {url}")
    try:
        html = http.get_text(url, respect_robots=False)
    except Exception as exc:
        print(f"  取得できず: {type(exc).__name__} {exc}")
        sys.exit(1)

    print(f"HTML {len(html):,} 文字 / RightBox {html.count('RightBox')}箇所 / h4 {html.count('<h4')}箇所")
    card = official_card.parse_card(html, card_id)
    for line in json.dumps(card, ensure_ascii=False, indent=1).splitlines():
        _emit(line)

    path = INSPECT_DIR / f"official-card-{card_id}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    print(f"\n全文を保存: {path}")


@main.command("check-images")
@click.option("--count", default=5, help="確かめる枚数")
def cmd_check_images(count: int) -> None:
    """カード画像が外から引けるかを確かめる。

    子ども向けページはカード画像を公式から直接読み込んでいる。断られると
    画面は空枠が並ぶだけになり、しかもエラーは出ないので気付けない。
    引けなくなったらここで分かるようにしておく。
    """
    from src.pokeca import http
    from src.pokeca.cardstore import load_cards
    from src.pokeca.site import CARD_IMAGE_BASE, CARD_IMAGE_PREFIX

    paths = [
        card["image"][len(CARD_IMAGE_PREFIX):]
        for card in load_cards().values()
        if (card.get("image") or "").startswith(CARD_IMAGE_PREFIX)
    ]
    if not paths:
        console.print("[yellow]カード画像のURLをまだ持っていません。[/yellow]")
        return

    ok = 0
    for path in paths[:count]:
        url = CARD_IMAGE_BASE + path
        try:
            response = http.get(url, respect_robots=False)
            kind = (response.headers.get("Content-Type") or "").split(";")[0]
            size = len(response.content)
            good = response.status_code == 200 and kind.startswith("image")
            ok += good
            mark = "[green]OK[/green]" if good else "[red]NG[/red]"
            console.print(f"  {mark} {response.status_code} {kind} {size:,}バイト  {path}")
        except Exception as exc:
            console.print(f"  [red]NG[/red] {type(exc).__name__} {exc}  {path}")

    tried = len(paths[:count])
    if ok == tried:
        console.print(f"[green]カード画像は外から読める[/green] ({ok}/{tried})")
    else:
        console.print(
            f"[red]カード画像が読めません ({ok}/{tried})[/red]\n"
            "[dim]直リンクを断られている可能性がある。"
            "ページは空枠が並ぶだけになるので、画像をやめる判断が要る。[/dim]"
        )
        sys.exit(1)


@main.command("probe-official")
@click.option("--deck-code", default="", help="調べるデッキコード (既定は保存済みの先頭)")
def cmd_probe_official(deck_code: str) -> None:
    """公式サイトが素のGETで何を返すか調べる。

    ブラウザで保存したHTMLはJavaScriptが動いた後のDOMなので、
    素のGETでは中身が空、ということが起こりうる。
    実際に何が返っているのかをここで確かめる。
    """
    from src.pokeca import http
    from src.pokeca.sources import official_card, official_deck

    if not deck_code:
        codes = [r.deck_code for r in load_results() if r.deck_code]
        if not codes:
            console.print("[yellow]デッキコードがありません。[/yellow]")
            sys.exit(1)
        deck_code = codes[0]

    # カード画像は子ども向けページに直接並べるので、外から引けるかが重要。
    # 引けないなら、そもそも画像を載せる設計自体が成り立たない。
    from src.pokeca.cardstore import load_cards
    from src.pokeca.site import CARD_IMAGE_BASE, CARD_IMAGE_PREFIX

    image_url = ""
    for card in load_cards().values():
        path = card.get("image") or ""
        if path.startswith(CARD_IMAGE_PREFIX):
            image_url = CARD_IMAGE_BASE + path[len(CARD_IMAGE_PREFIX):]
            break

    targets = [
        ("デッキ result.html", official_deck.deck_url(deck_code)),
        ("デッキ confirm.html", official_deck.deck_url_fallback(deck_code)),
        ("カード詳細", official_card.card_url("50452")),
        ("robots.txt", "https://www.pokemon-card.com/robots.txt"),
    ]
    if image_url:
        targets.append(("カード画像", image_url))

    for label, url in targets:
        console.print(f"\n[cyan]{label}[/cyan]  {url}")
        try:
            allowed = http.can_fetch(url)
            console.print(f"  robots.txt の許可: {allowed}")
            response = http.get(url, respect_robots=False)
            kind = (response.headers.get("Content-Type") or "").split(";")[0]
            if not kind.startswith("text") and "html" not in kind:
                # 画像などはそのまま出せない。届いたかどうかだけ分かればよい。
                console.print(
                    f"  HTTP {response.status_code} / {kind} / "
                    f"{len(response.content):,} バイト"
                )
                continue
            body = response.text
            console.print(f"  HTTP {response.status_code} / {len(body):,} 文字")
            markers = {
                "cardName_ (カード表)": body.count("cardName_"),
                "PCGDECK (JS)": body.count("PCGDECK"),
                "ポケモン (": body.count("ポケモン ("),
                "RightBox (カード詳細)": body.count("RightBox"),
            }
            for name, count in markers.items():
                mark = "[green]✓[/green]" if count else "[red]✗[/red]"
                console.print(f"    {mark} {name}: {count}")
            head = " ".join(body[:220].split())
            console.print(f"  [dim]冒頭: {head}[/dim]")
        except Exception as exc:
            console.print(f"  [red]{type(exc).__name__}[/red]: {exc}")


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


@main.command("odds")
@click.option("--deck", required=True, help="デッキ名 (例: メガドリュウズex)")
@click.option("--card", required=True, help="引きたいカード名")
@click.option("--draws", default=8, help="何枚めくるか (後攻の1番めは8枚)")
@click.option("--first/--second", default=False, help="先攻なら1番めにサポートが使えない")
def cmd_odds(deck: str, card: str, draws: int, first: bool) -> None:
    """引きたいカードにたどりつける確率を出す。

        python -m src.pokeca.cli odds --deck メガドリュウズex --card プレシャスキャリー

    デッキに1枚しか入っていなくても、そのカードを持ってこられるカードが
    たくさん入っていれば、実際にはずっと高い確率で手に入る。
    その「実質枚数」を数えてから確率を出す。
    """
    import statistics
    from collections import Counter

    from src.pokeca import odds as odds_mod
    from src.pokeca.cardstore import load_cards, load_decklists

    cards = load_cards()
    decklists = load_decklists()
    results = load_results()
    by_name = odds_mod.cards_by_name(cards)
    rules = odds_mod.load_reach()

    codes = {
        r.deck_code
        for r in results
        if r.deck_name == deck and r.deck_code and r.deck_code in decklists
    }
    if not codes:
        console.print(f"[yellow]{deck} の中身つきデッキが見つかりません[/yellow]")
        sys.exit(1)

    totals: list[int] = []
    sample: odds_mod.Reach | None = None
    for code in codes:
        entry = decklists[code]
        items = entry if isinstance(entry, list) else entry.get("cards", [])
        counts: Counter = Counter()
        for card_id, copies in items:
            counts[str(card_id)] += copies
        deck_counts = odds_mod.deck_counts_of(dict(counts), cards)
        if sum(deck_counts.values()) != 60:
            continue
        reach = odds_mod.reach_to(
            card, deck_counts, by_name, rules, allow_supporter=not first
        )
        totals.append(reach.total)
        if sample is None or reach.total > sample.total:
            sample = reach

    if not totals:
        console.print("[yellow]60枚そろったデッキがありません[/yellow]")
        sys.exit(1)

    naive = odds_mod.at_least_one(4, 60, draws)
    console.print(f"[bold]{deck}[/bold] で [bold]{card}[/bold] にたどりつく ({len(totals)}デッキ)")
    console.print(f"  実質枚数: 平均 {statistics.mean(totals):.1f} / 中央値 {statistics.median(totals):.0f} / 最大 {max(totals)}")
    best = max(totals)
    console.print(
        f"  最初の{draws}枚で1枚以上: "
        f"平均 {statistics.mean([odds_mod.at_least_one(t, 60, draws) for t in totals]) * 100:.1f}% "
        f"/ 一番よい形 {odds_mod.at_least_one(best, 60, draws) * 100:.1f}%"
    )
    console.print(f"  [dim](参考: ふつうに4枚入れただけなら {naive * 100:.1f}%)[/dim]")
    if sample:
        console.print("  一番よい形のたどり方:")
        for line in sample.lines(8):
            console.print(f"    {line}")


if __name__ == "__main__":
    main()
