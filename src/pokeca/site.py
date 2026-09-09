"""子ども向けの静的ページを生成する。

設計方針 (8歳が一人で使えること):

- 文字入力を一切させない。操作は大きなボタンのタップだけ
- UIの文言はひらがな。日付も「5がつ6にち」形式にする
- デッキ名はカタカナが多くそのまま読めるので、文字を大きくして主役にする
- レシピ画像や記事本文は転載しない。タップしたら元サイトを開く
- 1ファイル完結。JSもCSSもデータも全部埋め込むのでオフラインでも開ける
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date

from src.pokeca import aggregate
from src.pokeca.models import EVENT_CITY, EVENT_GYM, DeckResult
from src.pokeca.store import load_deck_notes, load_deck_themes, load_ruby, now_jst

# デッキ名が deck_themes.yaml に無いときに使う色。
# デッキ名のハッシュから決めるので、同じデッキは常に同じ色になる。
FALLBACK_COLORS = [
    "#E63946", "#F77F00", "#FCBF49", "#06D6A0", "#118AB2",
    "#7209B7", "#EF476F", "#2A9D8F", "#E76F51", "#4361EE",
]
FALLBACK_EMOJI = "⚡"


def _theme_for(deck_key: str, deck_name: str, themes: dict) -> dict:
    decks = themes.get("decks") or {}
    entry = decks.get(deck_name) or decks.get(deck_key) or {}
    digest = hashlib.md5(deck_key.encode("utf-8")).hexdigest()
    color = entry.get("color") or FALLBACK_COLORS[int(digest, 16) % len(FALLBACK_COLORS)]
    return {"color": color, "emoji": entry.get("emoji") or FALLBACK_EMOJI}


def _date_label(value: str) -> str:
    """"2026-05-06" → "5がつ6にち"。"""
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        return value or ""
    return f"{parsed.month}がつ{parsed.day}にち"


def _recent_by_event(results: list[DeckResult], max_rows: int) -> list[DeckResult]:
    """大会種別ごとに新しいものから max_rows 件ずつ選ぶ。

    ジムバトルは1日に数百件出るので、単純に全体の新しい順で打ち切ると
    ジムバトルだけで埋まり、シティリーグが一覧から消えてしまう。
    """
    ordered = sorted(results, key=lambda r: (r.date, r.rank, r.store_key), reverse=True)
    buckets: dict[str, list[DeckResult]] = {}
    for record in ordered:
        bucket = buckets.setdefault(record.event_type, [])
        if len(bucket) < max_rows:
            bucket.append(record)
    selected = [r for bucket in buckets.values() for r in bucket]
    selected.sort(key=lambda r: (r.date, r.rank, r.store_key), reverse=True)
    return selected


# カード画像のURLは長く、1枚ぶんの半分以上を占める。
# 共通部分を落として持ち、表示するときに組み立て直す。
CARD_IMAGE_BASE = "https://www.pokemon-card.com/assets/images/card_images/large/"
CARD_IMAGE_PREFIX = "/assets/images/card_images/large/"

# 採用率のグループ名。子どもが読める言い方にする。
GROUP_LABELS = {
    "確定枠": "かならず入っている",
    "よく入る": "よく入っている",
    "選択枠": "人によってちがう",
}
# 1つのデッキで見せるカードの数の上限。
# 1枚積みの端まで全部出すとページが重くなるうえ、子どもが読み切れない。
MAX_CARDS_PER_DECK = 45
# 中身を出すのに必要な最低デッキ数。
# 2〜3件しかないものの「採用率100%」は、ただの偶然でしかない。
MIN_DECKS_FOR_CONTENTS = 5


# YAML の折り返しは行を空白でつなぐので、日本語の文の途中に空白が入る。
#   「320を 削り切るしかない」
# 日本語どうしに挟まれた空白だけを消す。英数字の前後の空白は残す。
_JOIN_SPACE = re.compile(r"(?<=[^\x00-\x7F])[ \t]+(?=[^\x00-\x7F])")


def _tidy(value):
    """ノートの文字列から、折り返しで入った余分な空白を取る。"""
    if isinstance(value, str):
        return _JOIN_SPACE.sub("", value.strip())
    if isinstance(value, list):
        return [_tidy(v) for v in value]
    if isinstance(value, dict):
        # 引用はカードの原文そのままなので、整形しない
        return {k: (v if k == "引用" else _tidy(v)) for k, v in value.items()}
    return value


def _card_row(row: dict, cards: dict) -> dict:
    """採用率1行を、ページに出す形にする。

    平均枚数よりも「何枚入れている人が一番多いか」のほうが、
    デッキを組むときにはそのまま使える。両方持たせる。
    """
    spread = row.get("distribution") or {}
    common = max(spread, key=lambda n: spread[n]) if spread else 0
    image = cards.get(row.get("id", ""), {}).get("image", "")
    return {
        "name": row["name"],
        "n": common,
        "avg": round(row["average"], 1),
        "decks": row["decks"],
        "total": row["total"],
        "img": image[len(CARD_IMAGE_PREFIX):] if image.startswith(CARD_IMAGE_PREFIX) else "",
    }


def build_contents(
    results: list[DeckResult],
    decklists: dict[str, dict],
    cards: dict[str, dict],
    deck_keys: list[str],
) -> tuple[dict, dict]:
    """デッキごとの「なかみ」と、カードから引くための索引を作る。

    Returns:
        (なかみ, カード索引)。中身をまだ持っていなければ両方とも空。
    """
    from src.pokeca import analysis, odds

    if not decklists:
        return {}, {}

    corpus = analysis.build_corpus(results, decklists, cards)
    if not corpus:
        return {}, {}

    notes = (load_deck_notes() or {}).get("decks") or {}
    reach_rules = odds.load_reach()
    by_name = odds.cards_by_name(cards)

    contents: dict[str, dict] = {}
    shown: set[str] = set()

    for key in deck_keys:
        decks = analysis.select(corpus, deck_key=key)
        if len(decks) < MIN_DECKS_FOR_CONTENTS:
            continue

        rows = analysis.adoption(decks)
        groups = []
        budget = MAX_CARDS_PER_DECK
        for label, group_rows in analysis.core_and_flex(rows).items():
            if not group_rows or budget <= 0:
                continue
            picked = group_rows[:budget]
            budget -= len(picked)
            groups.append(
                {
                    "label": GROUP_LABELS.get(label, label),
                    "cards": [_card_row(r, cards) for r in picked],
                }
            )
            shown.update(r["name"] for r in picked)

        variants = analysis.variants(decks)
        contents[key] = {
            "name": decks[0].deck_name,
            "decks": len(decks),
            # 1番めに引きたいカードへ、どれくらいの確率で届くか
            "odds": odds.startup_odds(
                decks[0].deck_name,
                [d.counts for d in decks if d.total == 60],
                by_name,
                reach_rules,
            ),
            # そのデッキの「読み方」。数字からは出てこない部分。
            "note": _tidy(notes.get(decks[0].deck_name) or notes.get(key) or {}),
            "groups": groups,
            "variants": [
                {"cards": v["cards"], "decks": v["decks"], "share": round(v["share"], 2)}
                for v in variants[:6]
            ],
            "other": analysis.variant_coverage(decks, variants),
        }

    # カード → そのカードを使っているデッキ。
    # デッキの中身に出したカードだけでなく、集めた全デッキぶんを載せる。
    # 「このカードが入っているデッキ」を名前で探せるようにするため。
    index = analysis.card_index(corpus)
    facts = _card_facts(cards, _printing_use(decklists))
    deck_totals = Counter(deck.deck_name for deck in corpus)

    card_decks = {}
    for name, data in index.items():
        fact = facts.get(name, {})
        image = fact.get("image") or ""
        card_decks[name] = {
            "decks": data["decks"],
            "avg": round(data["average"], 1),
            # JSON にしたときと同じ形 (配列) で持つ。
            # タプルのままだと、書き出す前後で形が変わって扱いを間違えやすい。
            # 3つめは「そのデッキ全体のうち何件か」。割合を出すのに使う。
            "top": [
                [deck_name, n, deck_totals.get(deck_name, 0)]
                for deck_name, n in list(data["archetypes"].items())[:8]
            ],
            "sec": fact.get("section", ""),
            "img": image[len(CARD_IMAGE_PREFIX):] if image.startswith(CARD_IMAGE_PREFIX) else "",
            "txt": fact.get("text", ""),
        }
    return contents, card_decks


def _printing_use(decklists: dict[str, dict]) -> Counter:
    """カードID → そのIDが使われたデッキ数。どの版が実物かを決めるのに使う。"""
    used: Counter = Counter()
    for entry in decklists.values():
        items = entry if isinstance(entry, list) else entry.get("cards", [])
        for item in items:
            card_id = item.get("id") if isinstance(item, dict) else item[0]
            if card_id:
                used[str(card_id)] += 1
    return used


def _card_facts(
    cards: dict[str, dict], printing_use: Counter | None = None
) -> dict[str, dict]:
    """カード名 → 画面に出したい中身 (区分・画像・カードの文)。

    同じ名前でも、ワザや特性がまるで違うカードがある。
    たとえば「モグリュー」は【ほりまくり】を持つものと
    「なかまをよぶ」を持つものがあり、メガドリュウズexのデッキは
    178件すべてが後者を使っている。
    ここで前者の文を出すと、誰も使っていないカードを見せることになる。

    だから代表は「実際に一番多く使われている版」から選ぶ。
    使われた数が同じなら、ワザや特性まで取れているほうを取る。
    """
    use = printing_use or Counter()
    best: dict[str, dict] = {}
    for card_id, card in cards.items():
        name = card.get("name")
        if not name:
            continue
        rank = (use.get(str(card_id), 0), 1 if card.get("detail") else 0)
        current = best.get(name)
        if current is None or rank > current[0]:
            best[name] = (rank, card)
    best = {name: card for name, (_, card) in best.items()}

    out: dict[str, dict] = {}
    for name, card in best.items():
        lines: list[str] = []
        if card.get("hp"):
            head = ["HP" + str(card["hp"])]
            head += [part for part in (card.get("type"), card.get("stage")) if part]
            head.append("弱点 " + (card.get("weakness") or "なし"))
            lines.append("　".join(head))
        for ability in card.get("abilities") or []:
            lines.append(f"【{ability['name']}】{ability.get('effect') or ''}")
        for attack in card.get("attacks") or []:
            # 空の欄をそのまま並べると、間が不自然に空いて読みにくくなる
            parts = ["".join(attack.get("cost") or []) or "―", attack["name"]]
            parts += [
                part for part in (attack.get("damage"), attack.get("effect")) if part
            ]
            lines.append("　".join(parts))
        if card.get("text"):
            lines.append(card["text"])
        out[name] = {
            "section": card.get("section", ""),
            "image": card.get("image", ""),
            "text": "\n".join(lines),
            "detail": card.get("detail"),
        }
    return out


# 中身を載せるデッキの数。デッキ名ごとに新しいほうからこの数だけ。
# 全部 (4600件) 載せるとページが1MB増えるので、
# 「どの型にも実例がある」ことを保ちつつ、この数で打ち切る。
RECIPES_PER_DECK = 15

# 60枚を並べる順番。実物のレシピと同じ並びにする
SECTION_ORDER = (
    "ポケモン",
    "ポケモンのどうぐ",
    "グッズ",
    "サポート",
    "スタジアム",
    "エネルギー",
)


def build_recipes(
    results: list[DeckResult],
    decklists: dict[str, dict],
    cards: dict[str, dict],
    themes: dict | None = None,
    per_deck: int = RECIPES_PER_DECK,
) -> tuple[list[str], dict, dict]:
    """「このカードが入っていたデッキ」を実物で見せるための材料。

    カード名を番号に置き換えて持つ。同じ名前が何千回も出てくるので、
    そのまま入れるとページが1MB増えてしまう。

    Returns:
        (カード名の一覧, {デッキコード: [[番号, 枚数], ...]}, {デッキコード: 見出し})
    """
    if not decklists or not cards:
        return [], {}, {}

    name_of = {card_id: card.get("name") for card_id, card in cards.items()}

    rows = [
        r
        for r in results
        if r.deck_name and r.deck_code and r.deck_code in decklists
    ]
    rows.sort(key=lambda r: (r.date, r.rank), reverse=True)

    # デッキ名ごとに新しいほうから拾う。
    # 全体の新しい順にすると、流行っている型だけで埋まってしまい、
    # 数の少ない型を調べたときに実例が1つも出てこなくなる。
    taken: Counter = Counter()
    picked: dict[str, DeckResult] = {}
    for record in rows:
        if record.deck_code in picked:
            continue
        if taken[record.deck_name] >= per_deck:
            continue
        taken[record.deck_name] += 1
        picked[record.deck_code] = record

    names: list[str] = []
    number: dict[str, int] = {}

    recipes: dict[str, list] = {}
    meta: dict[str, dict] = {}
    for code, record in picked.items():
        entry = decklists[code]
        items = entry if isinstance(entry, list) else entry.get("cards", [])
        counts: Counter = Counter()
        for item in items:
            card_id, copies = (
                (item.get("id"), item.get("count")) if isinstance(item, dict) else item
            )
            name = name_of.get(str(card_id))
            if name:
                counts[name] += copies
        if not counts:
            continue
        packed = []
        for name, copies in counts.items():
            if name not in number:
                number[name] = len(names)
                names.append(name)
            packed.append([number[name], copies])
        recipes[code] = packed
        # 一覧に出すデッキと同じ形にしておく。
        # そうすればカードを探したときも、いつもと同じ見た目の
        # デッキ (レシピの写真つき) をそのまま並べられる
        theme = _theme_for(record.deck_key, record.deck_name, themes or {})
        meta[code] = {
            "date": record.date,
            "dateLabel": _date_label(record.date),
            "store": record.store,
            "prefecture": record.prefecture,
            "league": record.league,
            "rank": record.rank,
            "eventLabel": record.event_label,
            "deck": record.deck_name,
            "deckKey": record.deck_key,
            "recipeUrl": record.deck_code_url or record.source_url,
            "hasCode": bool(record.deck_code),
            "image": record.deck_image_url or record.image_url,
            "emoji": theme["emoji"],
        }
    return names, recipes, meta


def build_data(
    results: list[DeckResult],
    *,
    is_sample: bool = False,
    max_rows: int = 300,
    decklists: dict[str, dict] | None = None,
    cards: dict[str, dict] | None = None,
) -> dict:
    """ページに埋め込む JSON を組み立てる。

    一覧に載せる行は大会種別ごとに新しいほうから max_rows 件で打ち切る。
    ランキングは打ち切る前の全件から計算するので、集計の正確さは保たれる。

    decklists と cards を渡すと、デッキごとの「なかみ」も一緒に埋め込む。
    渡さなければ従来どおりの一覧とランキングだけになる。
    """
    themes = load_deck_themes()

    rows = []
    for record in _recent_by_event(results, max_rows):
        theme = _theme_for(record.deck_key, record.deck_name, themes)
        rows.append(
            {
                "date": record.date,
                "dateLabel": _date_label(record.date),
                "store": record.store,
                "prefecture": record.prefecture,
                "league": record.league,
                "rank": record.rank,
                "event": record.event_type,
                "eventLabel": record.event_label,
                "deck": record.deck_name,
                "deckKey": record.deck_key,
                # レシピは公式デッキコードを最優先、無ければ元記事へ
                "recipeUrl": record.deck_code_url or record.source_url,
                "hasCode": bool(record.deck_code),
                # 公式が生成するデッキ画像。デッキコードがあれば必ず付く
                "image": record.deck_image_url or record.image_url,
                "color": theme["color"],
                "emoji": theme["emoji"],
            }
        )

    # 大会種別ごとにランキングを持つ。ジムバトルは件数が桁違いに多いので、
    # 一緒くたにするとシティリーグの結果が埋もれてしまう。
    rankings: dict[str, dict] = {}
    for event in ("all", EVENT_CITY, EVENT_GYM):
        scope = results if event == "all" else [r for r in results if r.event_type == event]
        rankings[event] = {}
        for key, (label, days) in aggregate.PERIODS.items():
            ranked = aggregate.deck_ranking(scope, days=days)
            for entry in ranked:
                theme = _theme_for(entry["deck_key"], entry["deck_name"], themes)
                entry["color"] = theme["color"]
                entry["emoji"] = theme["emoji"]
            rankings[event][key] = {"label": label, "items": ranked[:20]}

    decks = aggregate.deck_choices(results)
    for entry in decks:
        theme = _theme_for(entry["deck_key"], entry["deck_name"], themes)
        entry["color"] = theme["color"]
        entry["emoji"] = theme["emoji"]

    # 出すのは上位24デッキ。ただし解説を書いたデッキは、その週たまたま
    # 勝ち数が落ちて24位から外れても残す。
    # せっかく書いた読み方が、静かな1週間があるだけで見られなくなるのを防ぐ。
    written = set(((load_deck_notes() or {}).get("decks") or {}))
    shown_decks = decks[:24]
    seen_names = {d["deck_name"] for d in shown_decks}
    for entry in decks[24:]:
        if entry["deck_name"] in written and entry["deck_name"] not in seen_names:
            shown_decks.append(entry)
            seen_names.add(entry["deck_name"])

    contents, card_decks = build_contents(
        results, decklists or {}, cards or {}, [d["deck_key"] for d in shown_decks]
    )
    recipe_names, recipes, recipe_meta = build_recipes(
        results, decklists or {}, cards or {}, themes
    )

    summary = aggregate.summary(results)
    latest = summary.get("latest_date") or ""
    summary["latest_date_label"] = _date_label(latest)
    summary["latest_first_count"] = sum(
        1 for r in results if r.rank == 1 and r.date == latest
    )

    return {
        "updatedAt": now_jst().strftime("%Y/%m/%d %H:%M"),
        "isSample": is_sample,
        # デッキ名がまだ1件も取れていないときは、ランキングと絞り込みを隠す。
        # 名前が無い状態でそれらを出しても空欄が並ぶだけで混乱させる。
        "hasNames": any(r.deck_name for r in results),
        # 大会種別ごとのデッキ名の有無。シティリーグは名前が取れないので
        # ランキングが常に空になる。その理由を画面で説明するために使う。
        "namedEvents": [
            e
            for e in (EVENT_CITY, EVENT_GYM)
            if any(r.deck_name for r in results if r.event_type == e)
        ],
        # 実際に存在する大会種別だけをボタンに出す
        "events": [e for e in (EVENT_CITY, EVENT_GYM) if any(r.event_type == e for r in results)],
        "summary": summary,
        "results": rows,
        "rankings": rankings,
        "decks": shown_decks,
        # デッキごとの「なかみ」。中身をまだ取っていなければ空になり、
        # 画面側は「なかみを しらべる」ボタンを出さない。
        "contents": contents,
        "cardDecks": card_decks,
        # 「このカードが入っていたデッキ」を実物で見せる材料。
        # カード名は recipeCards の番号で持つ (そのまま入れると重い)
        "recipeCards": recipe_names,
        "recipes": recipes,
        "recipeMeta": recipe_meta,
        "cardBase": CARD_IMAGE_BASE,
        # 読みにくい漢字にだけルビを振る。全部ひらがなにすると中身が薄くなる。
        "ruby": load_ruby(),
    }


STYLE = """
:root{
  --bg:#FFF7EC; --card:#FFFFFF; --ink:#2B2118; --muted:#7A6A5A;
  --gold:#FFB703; --blue:#3A86FF; --line:#EFE1CE; --shadow:0 3px 0 #E8D6BE;
}
*{box-sizing:border-box}
/* 埋め込み表示のときに下の地色が透けないよう html にも背景を敷く */
html{background:var(--bg)}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:"Hiragino Maru Gothic ProN","ヒラギノ丸ゴ ProN","Yu Gothic",
    "Meiryo",system-ui,sans-serif;
  font-size:18px; line-height:1.7; -webkit-text-size-adjust:100%;
}
.wrap{max-width:760px;margin:0 auto;padding:16px 14px 64px}
h1{font-size:28px;margin:8px 0 2px;letter-spacing:.02em}
.updated{color:var(--muted);font-size:14px;margin:0 0 14px}
.sample{
  background:#FFE8E8;border:3px dashed #E63946;border-radius:14px;
  padding:12px 14px;margin:0 0 16px;font-size:16px;color:#B3242F;font-weight:700;
}
.note-box{
  background:#EAF2FF;border:3px solid #BBD4FF;border-radius:14px;
  padding:12px 14px;margin:0 0 16px;font-size:16px;color:#1F4E9C;
}
.hero{
  background:linear-gradient(135deg,#FFD166,#FFB703);
  border-radius:22px;padding:14px 18px;margin-bottom:14px;box-shadow:var(--shadow);
}
.hero .cap{font-size:16px;font-weight:700;margin:0 0 6px}
.hero .name{font-size:30px;font-weight:800;margin:0;line-height:1.3;word-break:break-word}
.hero .note{font-size:15px;margin:6px 0 0}
.section-title{font-size:15px;color:var(--muted);margin:16px 0 6px;font-weight:700}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.span2{grid-column:1 / -1}
button{font-family:inherit;font-size:19px;font-weight:700;cursor:pointer}
.seg{
  min-height:60px;padding:6px 4px;border-radius:16px;
  border:3px solid var(--line);background:var(--card);color:var(--ink);
  box-shadow:var(--shadow);line-height:1.3;
  /* ひらがなが単語の途中で折り返されると読みにくいので、
     文節の空白でだけ改行させる */
  word-break:keep-all;overflow-wrap:normal;
}
.grid3 .seg{font-size:17px}
.seg[aria-pressed="true"]{background:var(--gold);border-color:#E09B00}
.seg.blue[aria-pressed="true"]{background:var(--blue);border-color:#2668D8;color:#fff}
/* 横スクロールにすると、25個あるデッキ名のうち画面に入るのは3つだけで、
   残りは指でこすらないと出てこない。何があるのか分からないので折り返す。
   ただし全部出すと縦に長くなりすぎるので、はじめは少しだけ出す。 */
.chips{display:flex;flex-wrap:wrap;gap:6px;padding:4px 0 10px}
.chip{
  flex:0 0 auto;min-height:44px;padding:0 12px;border-radius:22px;
  border:2px solid var(--line);background:var(--card);color:var(--ink);font-size:15px;
  max-width:100%;overflow-wrap:anywhere;
}
.chip[aria-pressed="true"]{background:var(--ink);color:#fff;border-color:var(--ink)}
.chip.more{border-style:dashed;color:var(--muted);font-weight:800}
.card{
  display:block;background:var(--card);border:3px solid var(--line);
  border-radius:20px;padding:14px 16px;margin-bottom:12px;
  text-decoration:none;color:inherit;box-shadow:var(--shadow);
}
.card:active{transform:translateY(2px);box-shadow:none}
.badge{
  display:inline-block;border-radius:999px;padding:3px 14px;font-size:15px;
  font-weight:800;color:#fff;margin-bottom:6px;
}
.badge.r1{background:var(--gold);color:#4A3200}
.badge.r2{background:var(--blue)}
/* keep-all は「ゆうしょう」が途中で折り返されるのを防ぐためだが、
   それだけだと「ナンジャモのハラバリーex」のような長いデッキ名が
   はみ出して横スクロールが出る。anywhere を足して、収まらないときだけ折る。 */
.deck{
  font-size:25px;font-weight:800;line-height:1.35;margin:2px 0 6px;
  word-break:keep-all;overflow-wrap:anywhere;
}
.meta{font-size:15px;color:var(--muted);word-break:keep-all;overflow-wrap:anywhere}
.go{margin-top:8px;font-size:16px;font-weight:700;color:#1F6FEB}
/* デッキ画像。タップしなくても中身が見えるように一覧へ並べる。
   loading=lazy なので、画面に入ったぶんだけ読み込む。 */
.shot{
  display:block;width:100%;height:auto;margin:10px 0 4px;
  border-radius:12px;border:2px solid var(--line);background:#fff;
}
.rankrow{display:flex;align-items:center;gap:14px}
.num{
  flex:0 0 auto;width:52px;height:52px;border-radius:50%;display:grid;place-items:center;
  font-size:22px;font-weight:800;color:#fff;background:var(--muted);
}
.num.n1{background:var(--gold);color:#4A3200}
.num.n2{background:#B8C0CC;color:#2B2118}
.num.n3{background:#D9A066}
.count{font-size:16px;color:var(--muted);word-break:keep-all;overflow-wrap:anywhere}
.count span{display:inline-block;margin-right:12px}
/* flex の子は既定で内容より縮まないので、明示的に縮められるようにする */
.rankrow > div:last-child{min-width:0}
.empty{text-align:center;color:var(--muted);padding:36px 10px;font-size:17px;line-height:2}
/* ---- デッキの なかみ ---- */
.groupbar{
  display:flex;align-items:baseline;gap:10px;margin:20px 0 8px;
  font-size:20px;font-weight:800;
}
.groupbar .howmany{font-size:14px;color:var(--muted);font-weight:700}
/* ---- 確率のはなし ---- */
.odds{
  background:#EEF4FF;border:3px solid #BFD4FF;border-radius:16px;
  padding:12px 14px;margin-bottom:9px;
}
.odds .head{font-size:18px;font-weight:800;margin-bottom:2px}
.odds .why{font-size:14px;color:var(--muted);margin-bottom:8px}
.odds .pair{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.odds .box{
  flex:1 1 130px;min-width:0;background:#fff;border:2px solid #BFD4FF;
  border-radius:12px;padding:8px 10px;
}
.odds .box.hi{border-color:var(--blue);background:#F4F8FF}
.odds .box .lbl{font-size:13px;color:var(--muted);line-height:1.5}
/* .num は順位バッジ (丸い背景) に使っているので、別の名前にする */
.odds .box .pc{font-size:26px;font-weight:800;line-height:1.2}
.odds .box.hi .pc{color:var(--blue)}
.odds .how{font-size:14px;line-height:1.9;margin:0;padding-left:1.1em}
.odds .how li{word-break:keep-all;overflow-wrap:anywhere}
/* 何枚入れていたか。順位バッジのとなりに並べる */
.badge.howmuch{background:var(--blue);margin-left:6px}
/* ---- カードでさがす ---- */
.search{
  background:var(--card);border:3px solid var(--line);border-radius:16px;
  padding:12px 14px;margin-bottom:10px;box-shadow:var(--shadow);
}
.search input{
  width:100%;box-sizing:border-box;font-size:19px;font-weight:700;
  padding:9px 12px;border:3px solid var(--line);border-radius:12px;
  background:#fff;color:var(--ink);font-family:inherit;
}
.search .hint{font-size:14px;color:var(--muted);line-height:1.8;margin-top:8px}
.hitcount{font-size:15px;color:var(--muted);margin:0 2px 8px}
.sec{
  flex:0 0 auto;font-size:12px;font-weight:800;color:var(--muted);
  border:2px solid var(--line);border-radius:8px;padding:1px 6px;margin-left:6px;
}
.usedby .why{font-size:13px;color:var(--muted);margin-bottom:6px}
/* じぶんのデッキをしらべる計算機 */
.calc{
  background:var(--card);border:3px solid var(--line);border-radius:16px;
  padding:12px 14px;margin-bottom:9px;box-shadow:var(--shadow);
}
.calc label{display:block;font-size:15px;font-weight:700;margin:8px 0 3px}
.calc input{
  width:100%;box-sizing:border-box;font-size:19px;font-weight:800;
  padding:7px 10px;border:3px solid var(--line);border-radius:10px;
  background:#fff;color:var(--ink);font-family:inherit;
}
.calc .out{
  margin-top:12px;padding:10px 12px;border-radius:12px;background:#EEF4FF;
  border:2px solid #BFD4FF;font-size:17px;line-height:1.8;
}
.calc .out b{font-size:28px;color:var(--blue)}
/* カード1枚ぶん。押すと「このカードを つかう デッキ」が下に開く。 */
.cardline{
  display:flex;align-items:center;gap:12px;width:100%;text-align:left;
  background:var(--card);border:3px solid var(--line);border-radius:16px;
  padding:8px 12px;margin-bottom:8px;box-shadow:var(--shadow);color:inherit;
}
.cardline:active{transform:translateY(2px);box-shadow:none}
.thumb{
  flex:0 0 auto;width:52px;aspect-ratio:63/88;object-fit:cover;
  border-radius:6px;background:#EFE1CE;border:1px solid var(--line);
}
/* 中央だけ伸び縮みさせる。枚数の枠まで伸ばすと、画像が読めなかったときに
   枚数がやたら横長になってしまう。 */
.cbody{min-width:0;flex:1}
.cname{font-size:19px;font-weight:800;line-height:1.35;word-break:keep-all;overflow-wrap:anywhere}
.cmeta{font-size:14px;color:var(--muted);word-break:keep-all;overflow-wrap:anywhere}
/* 何枚入れるかが一番大事なので、右端に大きく置く */
.copies{
  flex:0 0 auto;width:62px;text-align:center;font-size:22px;font-weight:800;
  color:#B26A00;background:#FFF1D0;border-radius:12px;padding:4px 6px;line-height:1.2;
}
.copies small{display:block;font-size:12px;color:var(--muted);font-weight:700}
/* 押したときに開く「このカードを つかう デッキ」 */
.usedby{
  background:#F5FAFF;border:3px solid #CFE4FF;border-radius:14px;
  padding:10px 14px;margin:-4px 0 10px;font-size:16px;line-height:1.9;
}
.usedby b{font-size:17px}
.usedby ul{margin:6px 0 0;padding-left:1.2em}
/* 型の一覧 */
.variant{
  display:flex;align-items:center;gap:12px;background:var(--card);
  border:3px solid var(--line);border-radius:16px;padding:10px 14px;
  margin-bottom:8px;box-shadow:var(--shadow);
}
.variant .bar{flex:1;min-width:0}
.variant .vname{font-size:18px;font-weight:800;word-break:keep-all;overflow-wrap:anywhere}
.variant .track{height:10px;border-radius:6px;background:#F0E4D2;margin-top:6px;overflow:hidden}
.variant .fill{height:100%;background:var(--blue);border-radius:6px}
.variant .pct{flex:0 0 auto;font-size:19px;font-weight:800;min-width:56px;text-align:right}
.lead{
  background:#FFF3D6;border:3px solid #FFD98A;border-radius:14px;
  padding:12px 14px;margin:0 0 4px;font-size:16px;line-height:1.8;
}
/* デッキの読み方。数字の並びに埋もれないよう、色で役割を分ける。
   ヒーロー部分が .note を使っているので、別の名前にする。 */
.deckNote{margin:12px 0 4px}
.deckNote .oneline{
  /* ルビが上の行にぶつかるので、本文と同じくらい行間をあける */
  font-size:20px;font-weight:800;line-height:1.9;margin:0 0 10px;
  padding:12px 15px;border-radius:14px;background:var(--ink);color:#fff;
}
.deckNote > div{border-radius:14px;padding:11px 15px;margin-bottom:9px;font-size:17px;line-height:1.95}
.deckNote b{display:block;font-size:18px;margin-bottom:4px}
.deckNote p{margin:0 0 8px}
.deckNote p:last-child{margin-bottom:0}
.deckNote ul{margin:4px 0 0;padding-left:1.15em}
.deckNote li{margin-bottom:5px}
/* ルビ。行間を広めに取っておかないと上の行と重なる */
ruby{ruby-align:center}
rt{font-size:.5em;font-weight:400;opacity:.75;letter-spacing:0}
/* カードの原文。ここはルビを振らず、書いてあるとおりに見せる */
.cardtext{
  margin:6px 0 10px;padding:10px 12px;border-radius:10px;
  background:#FBF6EE;border:2px solid var(--line);
  font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  font-size:13.5px;line-height:1.7;white-space:pre-wrap;overflow-x:auto;
}
.sec{background:var(--card);border:3px solid var(--line)}
.sec b{color:var(--ink)}
.sec-sum{background:#FFF8E6;border:3px solid #FFD98A}
.sec-sum b{color:#8A5A00}
.sec-warn{background:#FFF1EC;border:3px solid #FFC9B5}
.sec-warn b{color:#B3441E}
.sec-vs{background:#EEF2FF;border:3px solid #C6D2FF}
.sec-vs b{color:#3A4FB5}
footer{margin-top:28px;font-size:13px;color:var(--muted);line-height:1.8}
footer a{color:var(--muted)}
"""

SCRIPT = """
var DATA = __DATA__;
var state = { rank: "all", view: "new", deck: "all", period: "7d", event: "all", q: "", allDecks: false };

function el(id){ return document.getElementById(id); }

function setPressed(container, value){
  var nodes = container.querySelectorAll("[data-value]");
  for (var i=0;i<nodes.length;i++){
    nodes[i].setAttribute("aria-pressed", nodes[i].dataset.value === value ? "true" : "false");
  }
}

function filtered(){
  return DATA.results.filter(function(r){
    if (state.rank !== "all" && String(r.rank) !== state.rank) return false;
    if (state.deck !== "all" && r.deckKey !== state.deck) return false;
    if (state.event !== "all" && r.event !== state.event) return false;
    return true;
  });
}

function cardHtml(r, note){
  var cls = r.rank === 1 ? "r1" : "r2";
  var label = r.rank === 1 ? "優勝" : "準優勝";
  var where = [r.eventLabel, r.prefecture, r.store].filter(Boolean).join(" ");
  var league = r.league ? "・" + r.league : "";
  var go = r.hasCode ? "レシピ（60枚）を見る →" : "このデッキを見る →";
  // デッキ名がまだ取れていないときは、順位そのものを見出しにする
  var title = r.deck
    ? r.emoji + " " + esc(r.deck)
    : (r.rank === 1 ? "🏆 優勝デッキ" : "🥈 準優勝デッキ");
  var shot = r.image
    ? '<img class="shot" src="' + esc(r.image) + '" alt="" loading="lazy" decoding="async" ' +
      // 収集元が画像の直リンクを断ることがある。読み込めなければ黙って消して、
      // 壊れたアイコンが並ばないようにする (カード自体はリンクとして機能する)
      'onerror="this.remove()">'
    : '';
  // カードを探して出したときは「このデッキは何枚入れていたか」を並べて出す
  var tag = note ? '<span class="badge howmuch">' + esc(note) + "</span>" : "";
  return '<a class="card" href="' + r.recipeUrl + '" target="_blank" rel="noopener">' +
    '<span class="badge ' + cls + '">' + label + '</span>' + tag +
    '<div class="deck">' + title + '</div>' +
    '<div class="meta">' + esc(r.dateLabel) + "　" + esc(where) + esc(league) + '</div>' +
    shot +
    '<div class="go">' + go + '</div>' +
    '</a>';
}

function rankHtml(item, index){
  var n = index + 1;
  var cls = n <= 3 ? "num n" + n : "num";
  return '<div class="card"><div class="rankrow">' +
    '<div class="' + cls + '">' + n + '</div>' +
    '<div><div class="deck">' + item.emoji + " " + esc(item.deck_name) + '</div>' +
    '<div class="count"><span>優勝' + item.first + '回</span>' +
    '<span>準優勝' + item.second + '回</span></div>' +
    '</div></div></div>';
}

function esc(s){
  return String(s == null ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

/* ---- デッキの なかみ ----
   同じデッキ名でも、どこが共通で どこが人によって違うのかを見せる。
   カードを押すと、そのカードを使っている他のデッキが下に開く。 */

/* 画像が読めなかったときは、同じ大きさの空枠に置き換える。
   消すと行の形が変わり、壊れたアイコンを残すと汚い。 */
function thumbFail(img){
  var box = document.createElement("span");
  box.className = "thumb";
  img.replaceWith(box);
}

function cardLineHtml(c){
  var thumb = c.img
    ? '<img class="thumb" src="' + esc(DATA.cardBase + c.img) + '" alt="" ' +
      'loading="lazy" decoding="async" onerror="thumbFail(this)">'
    : '<span class="thumb"></span>';
  var pct = Math.round((c.decks / c.total) * 100);
  var known = DATA.cardDecks[c.name];
  var meta = pct + "%";
  if (known && known.decks > c.decks){
    meta += "　ほかにも" + (known.decks - c.decks) + "こ";
  }
  return '<button class="cardline" data-card="' + esc(c.name) + '">' +
    thumb +
    '<div class="cbody"><div class="cname">' + esc(c.name) + '</div>' +
    '<div class="cmeta">' + meta + '</div></div>' +
    '<div class="copies">' + c.n + '<small>まい</small></div>' +
    '</button>';
}

function usedByHtml(name){
  var d = DATA.cardDecks[name];
  if (!d) return "";
  var items = d.top.map(function(pair){
    // pair = [デッキ名, このカードを入れていた件数, そのデッキの全件数]
    var pct = pair[2] ? Math.round(pair[1] / pair[2] * 100) + "%" : "";
    return "<li>" + esc(pair[0]) + "　" + pair[1] + "こ" +
      (pct ? '<span style="color:var(--muted)">　' + pct + "</span>" : "") + "</li>";
  }).join("");
  var txt = d.txt ? '<div class="cardtext">' + esc(d.txt) + "</div>" : "";
  return '<div class="usedby" data-used="' + esc(name) + '">' +
    '<b>' + esc(name) + '</b> を つかって かった デッキは ' + d.decks + 'こ<br>' +
    'たいてい ' + d.avg + 'まい いれる' + txt +
    '<div class="why">どのデッキが つかっているか</div>' +
    '<ul>' + items + '</ul>' +
    recipeListHtml(name) + '</div>';
}

/* ---- じっさいのデッキと、その60枚 ----
   「どのデッキで多く使われているか」だけでは、実物が見えない。
   本当に知りたいのは「そのデッキに他に何が入っているか」なので、
   実際に勝った60枚をそのまま開けるようにする。 */
var RECIPE_MAX = 12;

// カード名 → recipeCards の番号
var recipeNo = null;
function recipeNumber(name){
  if (recipeNo === null){
    recipeNo = {};
    var list = DATA.recipeCards || [];
    for (var i=0; i<list.length; i++) recipeNo[list[i]] = i;
  }
  return recipeNo[name];
}

// そのカードが入っている実物のデッキを、新しい順に集める
function decksWithCard(name){
  var no = recipeNumber(name);
  if (no === undefined) return [];
  var recipes = DATA.recipes || {}, meta = DATA.recipeMeta || {};
  var out = [];
  for (var code in recipes){
    var deck = recipes[code], copies = 0;
    for (var i=0; i<deck.length; i++){
      if (deck[i][0] === no){ copies = deck[i][1]; break; }
    }
    if (!copies) continue;
    var m = meta[code] || {};
    out.push({code: code, copies: copies, meta: m});
  }
  out.sort(function(a, b){
    var x = a.meta.date || "", y = b.meta.date || "";
    return x < y ? 1 : (x > y ? -1 : 0);
  });
  return out;
}

function recipeListHtml(name){
  var hits = decksWithCard(name);
  if (!hits.length) return "";
  rubyReset();
  var out = ['<div class="why" style="margin-top:12px">' +
    rt("このカードを つかって かった デッキ") + "</div>", '<div class="recipes">'];
  for (var i=0; i<Math.min(hits.length, RECIPE_MAX); i++){
    var h = hits[i];
    // 一覧に出しているデッキと同じ形で並べる。写真がそのままレシピになっている
    out.push(cardHtml(h.meta, h.copies + "まい"));
  }
  if (hits.length > RECIPE_MAX){
    out.push('<div class="cmeta">ほかにも ' + (hits.length - RECIPE_MAX) + "こ あるよ</div>");
  }
  out.push("</div>");
  return out.join("");
}


/* ---- カードでさがす ----
   名前の一部でも見つかるようにする。子どもは正確な名前を打てない。
   名前で当たらなければ、カードの文の中も探す。
   「ダメカン」と打てば、ダメカンを置くカードが全部出てくる。 */
var SEARCH_MAX = 40;

function searchHits(q){
  q = (q || "").trim();
  var all = DATA.cardDecks || {};
  var names = Object.keys(all);
  if (!q){
    // 何も打っていないときは、よく入っているカードを出す
    names.sort(function(a, b){ return all[b].decks - all[a].decks; });
    return {rows: names.slice(0, 30), byText: {}, empty: true};
  }
  var byName = [], byText = [], flag = {};
  for (var i=0; i<names.length; i++){
    var n = names[i];
    if (n.indexOf(q) >= 0){ byName.push(n); continue; }
    if ((all[n].txt || "").indexOf(q) >= 0){ byText.push(n); flag[n] = true; }
  }
  var by = function(a, b){ return all[b].decks - all[a].decks; };
  byName.sort(by); byText.sort(by);
  return {rows: byName.concat(byText).slice(0, SEARCH_MAX), byText: flag, empty: false};
}

function searchRowHtml(name, viaText){
  var d = DATA.cardDecks[name];
  var thumb = d.img
    ? '<img class="thumb" src="' + esc(DATA.cardBase + d.img) + '" alt="" ' +
      'loading="lazy" decoding="async" onerror="thumbFail(this)">'
    : '<span class="thumb"></span>';
  var meta = d.decks + "この デッキで つかわれている";
  if (viaText) meta = "カードの文に ありました　/　" + meta;
  return '<button class="cardline" data-card="' + esc(name) + '">' + thumb +
    '<div class="cbody"><div class="cname">' + esc(name) +
    (d.sec ? '<span class="sec">' + esc(d.sec) + "</span>" : "") + "</div>" +
    '<div class="cmeta">' + meta + "</div></div>" +
    '<div class="copies">' + d.avg + "<small>まい</small></div></button>";
}

function searchOutHtml(){
  var hit = searchHits(state.q);
  var out = [];
  if (hit.empty){
    out.push('<div class="hitcount">よく入っているカード</div>');
  } else if (!hit.rows.length){
    return '<div class="empty">「' + esc(state.q) + '」は 見つかりませんでした。<br>' +
      'ひらがなや かたかなを かえて みてね</div>';
  } else {
    out.push('<div class="hitcount">' + hit.rows.length +
      (hit.rows.length >= SEARCH_MAX ? "こ以上" : "こ") + " 見つかったよ</div>");
  }
  for (var i=0; i<hit.rows.length; i++){
    out.push(searchRowHtml(hit.rows[i], hit.byText[hit.rows[i]]));
  }
  return out.join("");
}

function searchHtml(){
  rubyReset();
  return '<div class="search">' +
    '<input id="cardQ" type="search" autocomplete="off" ' +
    'placeholder="カードの名前をいれてね" value="' + esc(state.q || "") + '">' +
    '<div class="hint">' +
    rt("名前のいちぶだけでも さがせます。「ボール」「エネルギー」でも出てきます。") +
    "<br>" + rt("カードの文の中もさがすので、「ダメカン」「どく」でも出てきます。") +
    "</div></div>" +
    '<div id="searchOut">' + searchOutHtml() + "</div>";
}

function searchRun(){
  var box = el("searchOut");
  if (box) box.innerHTML = searchOutHtml();
}

function variantHtml(v, total){
  var pct = Math.round(v.share * 100);
  var name = v.cards.length ? v.cards.join(" と ") + " が入っている" : "どれも入れない";
  return '<div class="variant"><div class="bar">' +
    '<div class="vname">' + esc(name) + '</div>' +
    '<div class="track"><div class="fill" style="width:' + pct + '%"></div></div>' +
    '</div><div class="pct">' + pct + '%</div></div>';
}

/* ---- ルビ ----
   全部ひらがなにすると、かえって読みにくいうえに中身も薄くなる。
   漢字のまま書いて、読みにくい語にだけルビを振る。
   長い語から当てる (「事故率」が「事故」に食われないように)。 */
var RUBY_RE = null;
function rubyRegex(){
  if (RUBY_RE !== null) return RUBY_RE;
  var keys = Object.keys(DATA.ruby || {});
  if (!keys.length){ RUBY_RE = false; return false; }
  keys.sort(function(a, b){ return b.length - a.length; });
  RUBY_RE = new RegExp(keys.join("|"), "g");
  return RUBY_RE;
}

/* エスケープしてからルビを足す。戻り値はもうHTMLなので、重ねてescしないこと。
   同じ語に何度もルビが付くと読みにくいので、ひとかたまりの中では最初の1回だけ。 */
var rubySeen = {};
function rubyReset(){ rubySeen = {}; }

function rt(s){
  var text = esc(s);
  var re = rubyRegex();
  if (!re) return text;
  re.lastIndex = 0;
  return text.replace(re, function(word){
    var yomi = DATA.ruby[word];
    if (!yomi || rubySeen[word]) return word;
    rubySeen[word] = true;
    return "<ruby>" + word + "<rt>" + yomi + "</rt></ruby>";
  });
}

/* ---- デッキの読み方 ----
   採用率や打点は数字で出るが、なぜそう組むのかは数字からは出てこない。
   そこだけ人の言葉で持っている。 */
function noteHtml(note){
  if (!note || !note["結論"]) return "";
  var out = ['<div class="deckNote">'];
  rubyReset();
  out.push('<div class="oneline">' + rt(note["結論"]) + '</div>');

  var secs = note["節"] || [];
  for (var i=0; i<secs.length; i++){
    var sec = secs[i];
    rubyReset();
    out.push('<div class="sec"><b>' + rt(sec["見出し"]) + '</b>');
    if (sec["引用"]) out.push('<pre class="cardtext">' + esc(sec["引用"]) + '</pre>');
    var ps = sec["本文"] || [];
    for (var j=0; j<ps.length; j++) out.push('<p>' + rt(ps[j]) + '</p>');
    out.push('</div>');
  }

  var sum = note["まとめ"] || [];
  if (sum.length){
    rubyReset();
    out.push('<div class="sec-sum"><b>まとめ</b><ul>');
    for (var k=0; k<sum.length; k++) out.push('<li>' + rt(sum[k]) + '</li>');
    out.push('</ul>');
    if (note["しめ"]) out.push('<p>' + rt(note["しめ"]) + '</p>');
    out.push('</div>');
  }

  var pairs = [["弱点", "sec-warn", "弱点"], ["対面", "sec-vs", "相手にするとき"]];
  for (var n=0; n<pairs.length; n++){
    var items = note[pairs[n][0]];
    if (!items || !items.length) continue;
    rubyReset();
    out.push('<div class="' + pairs[n][1] + '"><b>' + rt(pairs[n][2]) + '</b><ul>');
    for (var m=0; m<items.length; m++) out.push('<li>' + rt(items[m]) + '</li>');
    out.push('</ul></div>');
  }
  out.push('</div>');
  return out.join("");
}

// 山札 size 枚に hits 枚あるカードが、draws 枚の中に1枚以上ある確率。
// 「引けなかった」のか「そもそも引ける枚数じゃなかった」のかを分ける道具。
function chance(hits, size, draws){
  if (hits <= 0 || size <= 0 || draws <= 0) return 0;
  if (hits >= size || draws >= size) return 1;
  var miss = 1;
  for (var i=0; i<draws; i++) miss *= (size - hits - i) / (size - i);
  return 1 - miss;
}

function oddsHtml(rows){
  if (!rows || !rows.length) return "";
  var out = ['<div class="groupbar">1番めに引けるかな？</div>'];
  for (var i=0; i<rows.length; i++){
    var r = rows[i];
    rubyReset();
    out.push('<div class="odds">');
    out.push('<div class="head">' + esc(r.card) + '</div>');
    if (r.why) out.push('<div class="why">' + rt(r.why) + '</div>');
    out.push('<div class="pair">');
    out.push('<div class="box"><div class="lbl">' + rt("入っている枚数で数えると") +
      '</div><div class="pc">' + r.p_plain + '%</div>' +
      '<div class="lbl">' + r.copies + rt("枚だから") + '</div></div>');
    out.push('<div class="box hi"><div class="lbl">' + rt("たどりつけるカードも数えると") +
      '</div><div class="pc">' + r.p_reach + '%</div>' +
      '<div class="lbl">' + rt("実質") + r.effective + rt("枚") + '</div></div>');
    out.push('</div>');
    if (r.how && r.how.length){
      out.push('<ul class="how">');
      for (var j=0; j<r.how.length; j++) out.push('<li>' + esc(r.how[j]) + '</li>');
      out.push('</ul>');
    }
    out.push('</div>');
  }
  rubyReset();
  out.push('<div class="odds" style="background:#FFF3D6;border-color:#FFD98A">' +
    '<div class="why" style="margin:0">' +
    rt("最初の7枚に、後攻なら1枚引いて8枚。その8枚で見た数字です。" +
       "たどりつけるカードを数えると、こんなに変わります。" +
       "強いデッキは、こうやって当たりを増やしています。") + '</div></div>');
  return out.join("");
}

// じぶんのデッキをしらべる計算機
function calcHtml(){
  rubyReset();
  return '<div class="groupbar">じぶんのデッキをしらべる</div>' +
    '<div class="calc">' +
    '<div class="why" style="font-size:15px;line-height:1.9;color:var(--muted)">' +
    rt("引きたいカードの枚数を入れてね。そのカードを持ってこられるカードがあれば、" +
       "その枚数も足して入れると、本当の確率が分かるよ。") + '</div>' +
    '<label>' + rt("山札の枚数") + '</label>' +
    '<input id="cSize" type="number" inputmode="numeric" value="60" min="1" max="60">' +
    '<label>' + rt("引きたいカードの枚数 (たどりつけるカードも足す)") + '</label>' +
    '<input id="cHits" type="number" inputmode="numeric" value="4" min="0" max="60">' +
    '<label>' + rt("何枚めくる？ (最初は7枚、後攻なら8枚)") + '</label>' +
    '<input id="cDraw" type="number" inputmode="numeric" value="7" min="1" max="60">' +
    '<div class="out" id="cOut"></div></div>';
}

function calcRun(){
  var box = el("cOut");
  if (!box) return;
  var size = Math.max(1, Math.min(60, parseInt(el("cSize").value, 10) || 60));
  var hits = Math.max(0, Math.min(size, parseInt(el("cHits").value, 10) || 0));
  var draw = Math.max(1, Math.min(size, parseInt(el("cDraw").value, 10) || 1));
  var p = Math.round(chance(hits, size, draw) * 100);
  var times = Math.round(p / 10);
  var tail = p >= 50
    ? rt("10回やったら、だいたい") + times + rt("回は引ける、ということ")
    : rt("10回やっても、だいたい") + times + rt("回しか引けない、ということ");
  rubyReset();
  box.innerHTML = rt("引ける確率は") + ' <b>' + p + '%</b><br>' +
    '<span style="font-size:15px;color:var(--muted)">' + tail + '</span>';
}

function insideHtml(){
  if (state.deck === "all"){
    return '<div class="empty">上のボタンでデッキを1つえらんでね</div>';
  }
  var c = DATA.contents[state.deck];
  if (!c){
    return '<div class="empty">このデッキは勝った回数が少ないので、<br>' +
      '中身を見くらべられません。<br>ほかのデッキをえらんでね</div>';
  }

  var out = ['<div class="lead">勝った <b>' + c.decks + 'この' + esc(c.name) + '</b>を見くらべたよ。' +
    '<br>カードをおすと、そのカードを使うほかのデッキが分かるよ。</div>'];

  out.push(noteHtml(c.note));
  out.push(oddsHtml(c.odds));

  if (c.variants.length){
    out.push('<div class="groupbar">どんな形がある？</div>');
    for (var v=0; v<c.variants.length; v++) out.push(variantHtml(c.variants[v], c.decks));
    if (c.other){
      out.push('<div class="cmeta" style="margin:-2px 0 4px">' +
        'のこり' + c.other + 'こは、この分け方にあてはまらなかったよ</div>');
    }
  }

  for (var g=0; g<c.groups.length; g++){
    var grp = c.groups[g];
    out.push('<div class="groupbar">' + esc(grp.label) +
      '<span class="howmany">' + grp.cards.length + '種類</span></div>');
    for (var i=0; i<grp.cards.length; i++) out.push(cardLineHtml(grp.cards[i]));
  }
  out.push(calcHtml());
  return out.join("");
}

// はじめに出すデッキの数。多すぎると、下のリストが画面から押し出される
var DECK_CHIPS_FIRST = 6;

function drawDeckChips(){
  var all = DATA.decks || [];
  var show = state.allDecks ? all.length : Math.min(DECK_CHIPS_FIRST, all.length);
  var chips = ['<button class="chip" data-value="all" aria-pressed="' +
    (state.deck === "all") + '">ぜんぶ</button>'];
  for (var i=0; i<show; i++){
    var d = all[i];
    var key = d.deckKey || d.deck_key;
    chips.push('<button class="chip" data-value="' + esc(key) + '" aria-pressed="' +
      (state.deck === key) + '">' + d.emoji + " " + esc(d.deck_name) + "</button>");
  }
  // 選んでいるデッキが、まだ出していないところにあるときは必ず出す。
  // でないと「今どれを選んでいるか」が画面から消える
  if (!state.allDecks && state.deck !== "all"){
    var shown = false;
    for (var s=0; s<show; s++){
      if ((all[s].deckKey || all[s].deck_key) === state.deck){ shown = true; break; }
    }
    if (!shown){
      for (var j=show; j<all.length; j++){
        var e2 = all[j];
        if ((e2.deckKey || e2.deck_key) === state.deck){
          chips.push('<button class="chip" data-value="' + esc(state.deck) +
            '" aria-pressed="true">' + e2.emoji + " " + esc(e2.deck_name) + "</button>");
          break;
        }
      }
    }
  }
  if (all.length > DECK_CHIPS_FIRST){
    chips.push('<button class="chip more" data-more="1">' +
      (state.allDecks ? "とじる" : "もっと見る (" + (all.length - show) + ")") + "</button>");
  }
  el("deckRow").innerHTML = chips.join("");
}

function render(){
  var listBox = el("list");
  var periodBox = el("periodRow");
  var rankRow = el("rankRow");
  var deckBox = el("deckRow");

  if (state.view === "search"){
    // 探すのはカードなので、しぼりこみのボタンは全部しまう。
    // 大会や期間で結果は変わらないのに、押せると変わりそうに見えるため。
    periodBox.style.display = "none";
    rankRow.style.display = "none";
    deckBox.style.display = "none";
    el("deckTitle").style.display = "none";
    el("eventRow").style.display = "none";
    el("eventTitle").style.display = "none";
    listBox.innerHTML = searchHtml();
    // 入力欄が画面の下に隠れないよう、上まで送る
    listBox.scrollIntoView({block: "start"});
    return;
  }

  if (state.view === "inside"){
    // デッキを選ばないと始まらないので、デッキのボタンだけ残す
    periodBox.style.display = "none";
    rankRow.style.display = "none";
    deckBox.style.display = "flex";
    el("deckTitle").style.display = "block";
    el("deckTitle").textContent = "どのデッキの中身？";
    listBox.innerHTML = insideHtml();
    calcRun();
    return;
  }
  el("deckTitle").textContent = "デッキでさがす";

  if (state.view === "rank"){
    periodBox.style.display = "grid";
    rankRow.style.display = "none";
    deckBox.style.display = "none";
    el("deckTitle").style.display = "none";
    var byEvent = DATA.rankings[state.event] || DATA.rankings["all"] || {};
    var items = (byEvent[state.period] || {items:[]}).items;
    var named = DATA.namedEvents || [];
    var noNamesHere = state.event !== "all" && named.indexOf(state.event) < 0;
    var msg = noNamesHere
      ? 'シティリーグはデッキの名前がまだ分からないので、<br>' +
        'ランキングは出せません。<br>「新しい順」で見てね'
      : 'この期間の結果はまだ無いよ。<br>「ぜんぶ」をおしてみてね';
    listBox.innerHTML = items.length
      ? items.map(rankHtml).join("")
      : '<div class="empty">' + msg + '</div>';
    return;
  }

  periodBox.style.display = "none";
  rankRow.style.display = "grid";
  if (DATA.hasNames){
    deckBox.style.display = "flex";
    el("deckTitle").style.display = "block";
  }
  var rows = filtered();
  listBox.innerHTML = rows.length
    ? rows.slice(0, 300).map(cardHtml).join("")
    : '<div class="empty">それに合う結果はまだ無いよ。<br>' +
      '「ぜんぶ見る」をおしてみてね</div>';
}

function boot(){
  var s = DATA.summary || {};
  var top = s.top_deck;
  if (top){
    el("heroCap").textContent = "いま いちばん強いデッキ";
    el("heroName").textContent = top.deck_name;
    el("heroNote").textContent = "さいきん1週間で" + top.first + "回 優勝";
  } else if (s.latest_first_count){
    // デッキ名がまだ無いとき。日付と件数だけでも「新しい結果が来た」ことは伝わる
    el("heroCap").textContent = "いちばん新しい結果";
    el("heroName").textContent = s.latest_date_label;
    el("heroNote").textContent = "優勝デッキが" + s.latest_first_count + "こあるよ";
  } else {
    el("heroCap").textContent = "";
    el("heroName").textContent = "まだデータが無いよ";
    el("heroNote").textContent = "";
  }
  el("updated").textContent = "こうしん: " + DATA.updatedAt;
  if (DATA.isSample) el("sample").style.display = "block";

  if (!DATA.hasNames){
    // デッキ名が無いあいだは、ランキングと絞り込みを出さない
    el("viewRow").style.display = "none";
    el("viewTitle").style.display = "none";
    el("deckRow").style.display = "none";
    el("deckTitle").style.display = "none";
    el("noNames").style.display = "block";
  }

  drawDeckChips();

  // 大会が2種類そろっているときだけ切り替えボタンを出す
  if ((DATA.events || []).length > 1){
    el("eventRow").style.display = "grid";
    el("eventTitle").style.display = "block";
  }

  el("eventRow").addEventListener("click", function(e){
    var b = e.target.closest("[data-value]"); if(!b) return;
    state.event = b.dataset.value; setPressed(el("eventRow"), state.event); render();
  });
  el("viewRow").addEventListener("click", function(e){
    var b = e.target.closest("[data-value]"); if(!b) return;
    state.view = b.dataset.value; setPressed(el("viewRow"), state.view); render();
  });
  el("rankRow").addEventListener("click", function(e){
    var b = e.target.closest("[data-value]"); if(!b) return;
    state.rank = b.dataset.value; setPressed(el("rankRow"), state.rank); render();
  });
  el("periodRow").addEventListener("click", function(e){
    var b = e.target.closest("[data-value]"); if(!b) return;
    state.period = b.dataset.value; setPressed(el("periodRow"), state.period); render();
  });
  el("deckRow").addEventListener("click", function(e){
    var more = e.target.closest("[data-more]");
    if (more){ state.allDecks = !state.allDecks; drawDeckChips(); return; }
    var b = e.target.closest("[data-value]"); if(!b) return;
    state.deck = b.dataset.value;
    // 選んだデッキが「もっと見る」の中にあることもあるので、組み立て直す
    drawDeckChips();
    render();
  });

  // 入力を拾う。中身は描き直されるので、親でまとめて受ける
  el("list").addEventListener("input", function(e){
    if (!e.target || !e.target.id) return;
    if (e.target.id === "cardQ"){
      state.q = e.target.value;
      searchRun();   // 入力欄は描き直さない。打っている途中で消えてしまうため
      return;
    }
    if (e.target.id.indexOf("c") === 0) calcRun();
  });

  el("list").addEventListener("click", function(e){
    // カードを押したら、そのカードを使う他のデッキを下に開く。もう一度押すと閉じる
    var b = e.target.closest("[data-card]"); if(!b) return;
    var open = b.nextElementSibling;
    var name = b.dataset.card;
    if (open && open.dataset.used === name){ open.remove(); return; }
    var html = usedByHtml(name);
    if (html) b.insertAdjacentHTML("afterend", html);
  });

  // 中身をまだ持っていないときは「なかみ」ボタンを出さない
  if (Object.keys(DATA.contents || {}).length){
    el("insideBtn").style.display = "block";
  }
  // カードの索引が無いと、探しても何も出ないのでボタンを出さない
  if (Object.keys(DATA.cardDecks || {}).length){
    el("searchBtn").style.display = "block";
  }

  render();
}
document.addEventListener("DOMContentLoaded", boot);
"""

BODY = """
<div class="wrap">
  <h1>きょうのポケカ</h1>
  <p class="updated" id="updated"></p>

  <div class="sample" id="sample" style="display:none">
    ⚠️ いまは練習用のサンプルデータです。
    本物の結果になるまで待ってね。
  </div>

  <div class="hero">
    <p class="cap" id="heroCap"></p>
    <p class="name" id="heroName"></p>
    <p class="note" id="heroNote"></p>
  </div>

  <div class="note-box" id="noNames" style="display:none">
    デッキの名前はまだ読みこめていません。
    カードをタップすると、本物のレシピ（60枚）が見られます。
  </div>

  <div class="section-title" id="eventTitle" style="display:none">どの大会？</div>
  <div class="grid3" id="eventRow" style="display:none">
    <button class="seg" data-value="all" aria-pressed="true">ぜんぶ</button>
    <button class="seg" data-value="city" aria-pressed="false">シティリーグ</button>
    <button class="seg" data-value="gym" aria-pressed="false">ジムバトル</button>
  </div>

  <div class="section-title" id="viewTitle">何を見る？</div>
  <div class="grid2" id="viewRow">
    <button class="seg" data-value="new" aria-pressed="true">新しい順</button>
    <button class="seg blue" data-value="rank" aria-pressed="false">強い順</button>
    <button class="seg span2" id="insideBtn" data-value="inside" aria-pressed="false"
            style="display:none">デッキの中身を調べる</button>
    <button class="seg span2" id="searchBtn" data-value="search" aria-pressed="false"
            style="display:none">カードでさがす</button>
  </div>

  <div class="grid2" id="rankRow" style="margin-top:10px">
    <button class="seg" data-value="1" aria-pressed="false">優勝</button>
    <button class="seg blue" data-value="2" aria-pressed="false">準優勝</button>
    <button class="seg span2" data-value="all" aria-pressed="true">ぜんぶ見る</button>
  </div>

  <div class="grid3" id="periodRow" style="margin-top:10px;display:none">
    <button class="seg" data-value="7d" aria-pressed="true">1週間</button>
    <button class="seg" data-value="30d" aria-pressed="false">1か月</button>
    <button class="seg" data-value="all" aria-pressed="false">ぜんぶ</button>
  </div>

  <div class="section-title" id="deckTitle">デッキでさがす</div>
  <div class="chips" id="deckRow"></div>

  <div id="list"></div>

  <footer>
    データの もとは
    <a href="https://pokecabook.com/" target="_blank" rel="noopener">ポケカブック</a> と
    <a href="https://players.pokemon-card.com/" target="_blank" rel="noopener">ポケモンカードゲーム プレイヤーズクラブ</a>
    です。デッキの中身はタップして元のページで見てね。<br>
    このページは家族用の個人的なまとめです。記事や画像の転載はしていません。
  </footer>
</div>
"""


def build_html(data: dict, *, standalone: bool = True) -> str:
    """ページのHTMLを組み立てる。

    Args:
        standalone: True なら <!doctype html> から始まる完全なHTMLを返す
            (GitHub Pages 用)。False なら body の中身だけを返す。
    """
    payload = json.dumps(data, ensure_ascii=False)
    script = SCRIPT.replace("__DATA__", payload)
    inner = (
        f"{BODY}\n<style>{STYLE}</style>\n<script>{script}</script>"
    )
    if not standalone:
        return f"<title>きょうのポケカ</title>\n{inner}\n"
    return (
        "<!doctype html>\n"
        '<html lang="ja">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n'
        '<meta name="theme-color" content="#FFB703">\n'
        "<title>きょうのポケカ</title>\n"
        "</head>\n<body>\n"
        f"{inner}\n"
        "</body>\n</html>\n"
    )
