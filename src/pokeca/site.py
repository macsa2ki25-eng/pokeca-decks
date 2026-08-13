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
    from src.pokeca import analysis

    if not decklists:
        return {}, {}

    corpus = analysis.build_corpus(results, decklists, cards)
    if not corpus:
        return {}, {}

    notes = (load_deck_notes() or {}).get("decks") or {}

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
            # そのデッキの「読み方」。数字からは出てこない部分。
            "note": _tidy(notes.get(decks[0].deck_name) or notes.get(key) or {}),
            "groups": groups,
            "variants": [
                {"cards": v["cards"], "decks": v["decks"], "share": round(v["share"], 2)}
                for v in variants[:6]
            ],
            "other": analysis.variant_coverage(decks, variants),
        }

    # カード → そのカードを使っているデッキ。画面に出したカードだけに絞る。
    index = analysis.card_index(corpus)
    card_decks = {
        name: {
            "decks": data["decks"],
            "avg": round(data["average"], 1),
            # JSON にしたときと同じ形 (配列) で持つ。
            # タプルのままだと、書き出す前後で形が変わって扱いを間違えやすい。
            "top": [[deck_name, n] for deck_name, n in list(data["archetypes"].items())[:8]],
        }
        for name, data in index.items()
        if name in shown
    }
    return contents, card_decks


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

    contents, card_decks = build_contents(
        results, decklists or {}, cards or {}, [d["deck_key"] for d in decks[:24]]
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
        "decks": decks[:24],
        # デッキごとの「なかみ」。中身をまだ取っていなければ空になり、
        # 画面側は「なかみを しらべる」ボタンを出さない。
        "contents": contents,
        "cardDecks": card_decks,
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
.chips{display:flex;gap:8px;overflow-x:auto;padding:4px 0 10px;-webkit-overflow-scrolling:touch}
.chip{
  flex:0 0 auto;min-height:52px;padding:0 18px;border-radius:26px;
  border:3px solid var(--line);background:var(--card);color:var(--ink);font-size:17px;
}
.chip[aria-pressed="true"]{background:var(--ink);color:#fff;border-color:var(--ink)}
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
  font-size:20px;font-weight:800;line-height:1.65;margin:0 0 10px;
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
var state = { rank: "all", view: "new", deck: "all", period: "7d", event: "all" };

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

function cardHtml(r){
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
  return '<a class="card" href="' + r.recipeUrl + '" target="_blank" rel="noopener">' +
    '<span class="badge ' + cls + '">' + label + '</span>' +
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
    return "<li>" + esc(pair[0]) + "　" + pair[1] + "こ</li>";
  }).join("");
  return '<div class="usedby" data-used="' + esc(name) + '">' +
    '<b>' + esc(name) + '</b> を つかって かった デッキは ' + d.decks + 'こ<br>' +
    'たいてい ' + d.avg + 'まい いれる' +
    '<ul>' + items + '</ul></div>';
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
  return out.join("");
}

function render(){
  var listBox = el("list");
  var periodBox = el("periodRow");
  var rankRow = el("rankRow");
  var deckBox = el("deckRow");

  if (state.view === "inside"){
    // デッキを選ばないと始まらないので、デッキのボタンだけ残す
    periodBox.style.display = "none";
    rankRow.style.display = "none";
    deckBox.style.display = "flex";
    el("deckTitle").style.display = "block";
    el("deckTitle").textContent = "どのデッキの中身？";
    listBox.innerHTML = insideHtml();
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

  var chips = ['<button class="chip" data-value="all" aria-pressed="true">ぜんぶ</button>'];
  for (var i=0;i<DATA.decks.length;i++){
    var d = DATA.decks[i];
    chips.push('<button class="chip" data-value="' + esc(d.deckKey || d.deck_key) + '">' +
      d.emoji + " " + esc(d.deck_name) + '</button>');
  }
  el("deckRow").innerHTML = chips.join("");

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
    var b = e.target.closest("[data-value]"); if(!b) return;
    state.deck = b.dataset.value; setPressed(el("deckRow"), state.deck); render();
  });

  // カードを押したら、そのカードを使う他のデッキを下に開く。もう一度押すと閉じる。
  el("list").addEventListener("click", function(e){
    var b = e.target.closest("[data-card]"); if(!b) return;
    var name = b.dataset.card;
    var open = b.nextElementSibling;
    if (open && open.dataset.used === name){ open.remove(); return; }
    var html = usedByHtml(name);
    if (html) b.insertAdjacentHTML("afterend", html);
  });

  // 中身をまだ持っていないときは「なかみ」ボタンを出さない
  if (Object.keys(DATA.contents || {}).length){
    el("insideBtn").style.display = "block";
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
