"""勝っているデッキの中身を、いろいろな角度から見るための道具。

results.json (いつ・どこで・何が勝ったか) と decklists.json (60枚の中身) を
デッキコードで突き合わせると、次のことが数字で言えるようになる。

- 同じ「ドラパルトex」でも、どこが共通でどこが違うのか (:func:`adoption`)
- マシマシラを使っているのはどんなデッキか (:func:`card_index`)
- 同じデッキ名の中に、どんな型があるのか (:func:`variants`)
- 自分のデッキは勝っているデッキと何が違うのか (:func:`compare`)

## カードは「名前」で数える

同じカードでも、再録されるたびに公式のカードIDは変わる。
IDで数えると「ネストボール」が別カード扱いになって採用率が壊れるので、
集計の単位は名前にして、IDは代表を1つだけ覚えておく。

## 数字は必ず「何件中の何件か」とセットで出す

3件しかないデッキの採用率100%と、80件あるデッキの採用率100%では
意味がまるで違う。分母を隠すと嘘になるので、常に一緒に返す。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import comb

from src.pokeca.models import DeckResult

DECK_SIZE = 60
OPENING_HAND = 7

# 採用率でカードを3つに分ける境目。
# 「ほぼ全部に入っている＝そのデッキの土台」「半々＝作る人の好みが出る所」
CORE_SHARE = 0.9
COMMON_SHARE = 0.5
# 型を分ける手がかりに使うのは、入っているデッキと入っていないデッキが
# どちらもそれなりにあるカードだけ
FLEX_MIN_SHARE = 0.15
FLEX_MAX_SHARE = 0.85


@dataclass
class DeckEntry:
    """1つの勝ったデッキ = 大会の結果 + 60枚の中身。"""

    deck_code: str
    deck_name: str
    deck_key: str
    date: str
    event_type: str
    rank: int
    counts: dict[str, int] = field(default_factory=dict)  # カード名 → 枚数
    card_ids: dict[str, str] = field(default_factory=dict)  # カード名 → 代表ID
    sections: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def has(self, card_name: str) -> bool:
        return card_name in self.counts


def build_corpus(
    results: list[DeckResult], decklists: dict[str, dict]
) -> list[DeckEntry]:
    """大会結果と60枚の中身を突き合わせて、分析できる形にまとめる。

    中身をまだ取っていないデッキは黙って飛ばす。デッキ名が空のものも
    飛ばす -- 名前で束ねられないと、採用率の意味が無いため。
    """
    corpus: list[DeckEntry] = []
    for result in results:
        deck = decklists.get(result.deck_code)
        if not deck or not result.deck_name:
            continue

        counts: dict[str, int] = defaultdict(int)
        card_ids: dict[str, str] = {}
        for card in deck.get("cards", []):
            name = card.get("name") or ""
            if not name:
                # 名前が引けなかったカードは数えられない。
                # 枚数の合計が60から欠けるので、あとで気付けるようにする。
                continue
            counts[name] += card.get("count", 0)
            card_ids.setdefault(name, card.get("id", ""))

        corpus.append(
            DeckEntry(
                deck_code=result.deck_code,
                deck_name=result.deck_name,
                deck_key=result.deck_key,
                date=result.date,
                event_type=result.event_type,
                rank=result.rank,
                counts=dict(counts),
                card_ids=card_ids,
                sections=deck.get("sections", {}),
            )
        )
    return corpus


def select(
    corpus: list[DeckEntry],
    *,
    deck_key: str = "",
    event_type: str = "",
    rank: int = 0,
) -> list[DeckEntry]:
    """デッキ名・大会の種類・順位で絞る。"""
    out = corpus
    if deck_key:
        out = [d for d in out if d.deck_key == deck_key]
    if event_type:
        out = [d for d in out if d.event_type == event_type]
    if rank:
        out = [d for d in out if d.rank == rank]
    return out


# ------------------------------------------------------------------
# 採用率 -- 「同じデッキ名でも中身はどう違うのか」
# ------------------------------------------------------------------


def adoption(decks: list[DeckEntry]) -> list[dict]:
    """カードごとの採用状況を、採用デッキ数の多い順に返す。

    Returns:
        各要素は
        {"name", "id", "decks", "total", "share",
         "average", "distribution": {枚数: デッキ数}}

        - decks   そのカードを入れているデッキの数
        - total   分母 (対象デッキの数)
        - share   decks / total
        - average 入れているデッキでの平均枚数 (入れていないデッキは数えない)
    """
    total = len(decks)
    if not total:
        return []

    using: Counter[str] = Counter()
    copies: Counter[str] = Counter()
    spread: dict[str, Counter[int]] = defaultdict(Counter)
    ids: dict[str, str] = {}

    for deck in decks:
        for name, count in deck.counts.items():
            if count <= 0:
                continue
            using[name] += 1
            copies[name] += count
            spread[name][count] += 1
            ids.setdefault(name, deck.card_ids.get(name, ""))

    rows = [
        {
            "name": name,
            "id": ids.get(name, ""),
            "decks": count,
            "total": total,
            "share": count / total,
            "average": copies[name] / count,
            "distribution": dict(sorted(spread[name].items())),
        }
        for name, count in using.items()
    ]
    rows.sort(key=lambda r: (-r["decks"], -r["average"], r["name"]))
    return rows


def core_and_flex(rows: list[dict]) -> dict[str, list[dict]]:
    """採用率で「確定枠 / よく入る / 選択枠」に分ける。

    デッキの違いが出るのは選択枠。ここを見比べるのが型の話の入口になる。
    """
    groups: dict[str, list[dict]] = {"確定枠": [], "よく入る": [], "選択枠": []}
    for row in rows:
        if row["share"] >= CORE_SHARE:
            groups["確定枠"].append(row)
        elif row["share"] >= COMMON_SHARE:
            groups["よく入る"].append(row)
        else:
            groups["選択枠"].append(row)
    return groups


# ------------------------------------------------------------------
# カードからデッキを引く -- 「マシマシラを使うデッキは何があるのか」
# ------------------------------------------------------------------


def card_index(corpus: list[DeckEntry]) -> dict[str, dict]:
    """カード名 → そのカードを使っているデッキの一覧。

    Returns:
        {カード名: {"decks": 件数, "archetypes": {デッキ名: 件数},
                    "average": 平均枚数}}
    """
    index: dict[str, dict] = {}
    for deck in corpus:
        for name, count in deck.counts.items():
            if count <= 0:
                continue
            entry = index.setdefault(
                name, {"decks": 0, "copies": 0, "archetypes": Counter()}
            )
            entry["decks"] += 1
            entry["copies"] += count
            entry["archetypes"][deck.deck_name] += 1

    for entry in index.values():
        entry["average"] = entry["copies"] / entry["decks"]
        entry["archetypes"] = dict(entry["archetypes"].most_common())
        del entry["copies"]
    return index


def find_cards(index: dict[str, dict], keyword: str) -> list[tuple[str, dict]]:
    """カード名の一部から探す。子どもが正確な名前を打てなくても引けるように。"""
    keyword = keyword.strip()
    if not keyword:
        return []
    hits = [(name, data) for name, data in index.items() if keyword in name]
    hits.sort(key=lambda pair: -pair[1]["decks"])
    return hits


# ------------------------------------------------------------------
# 型を見つける -- 「同じドラパルトでも、どんな作り方があるのか」
# ------------------------------------------------------------------


def divisive_cards(rows: list[dict], limit: int = 3) -> list[dict]:
    """入っているデッキと入っていないデッキが半々に近いカードを選ぶ。

    ここが分かれ目になっているカード。全部に入っているカードは
    型を分ける手がかりにならないので外す。
    """
    candidates = [r for r in rows if FLEX_MIN_SHARE <= r["share"] <= FLEX_MAX_SHARE]
    candidates.sort(key=lambda r: (abs(r["share"] - 0.5), -r["decks"]))
    return candidates[:limit]


def variants(
    decks: list[DeckEntry], limit: int = 3, min_decks: int = 2
) -> list[dict]:
    """同じデッキ名の中にある「型」を、分かれ目のカードで分けて返す。

    機械的に似たもの同士を寄せる方法もあるが、なぜその分け方なのかを
    子どもに説明できないと意味がない。「このカードが入っているかどうか」
    という、見れば分かる基準だけで分ける。

    1件しかない組み合わせは「型」とは呼ばない。ただの1つのデッキを
    型と呼んでしまうと、みんなそうしていると勘違いさせるため、
    ``min_decks`` 件以上あるものだけ返す。返らなかったぶんは
    :func:`variant_coverage` で数えられる。

    Returns:
        各要素は {"cards": [入っているカード名], "decks": 件数,
                  "total": 分母, "share": 割合, "examples": [デッキコード]}
    """
    if len(decks) < min_decks:
        return []

    keys = [row["name"] for row in divisive_cards(adoption(decks), limit)]
    if not keys:
        return []

    groups: dict[tuple[bool, ...], list[DeckEntry]] = defaultdict(list)
    for deck in decks:
        groups[tuple(deck.has(name) for name in keys)].append(deck)

    out = []
    for pattern, members in groups.items():
        if len(members) < min_decks:
            continue
        included = [name for name, present in zip(keys, pattern) if present]
        out.append(
            {
                "cards": included,
                "decks": len(members),
                "total": len(decks),
                "share": len(members) / len(decks),
                "examples": [d.deck_code for d in members[:3]],
            }
        )
    out.sort(key=lambda g: -g["decks"])
    return out


def variant_coverage(decks: list[DeckEntry], groups: list[dict]) -> int:
    """型にまとまらなかったデッキの数。

    「見つかった型はこれだけ。残りはばらばら」と正直に言うために使う。
    """
    return len(decks) - sum(group["decks"] for group in groups)


# ------------------------------------------------------------------
# デッキの安定度 -- 「回らない」に数字で答えるための計算
# ------------------------------------------------------------------


def draw_probability(copies: int, deck_size: int = DECK_SIZE, hand: int = OPENING_HAND) -> float:
    """デッキに copies 枚あるカードが、最初の hand 枚に1枚以上来る確率。

    山札から順番に引くだけの話なので、確率は計算で正確に出る。
    「たまたま引けなかった」のか「そもそも引ける枚数ではない」のかを
    分けて話せるようにするための土台。
    """
    if copies <= 0 or deck_size <= 0 or hand <= 0:
        return 0.0
    if copies >= deck_size or hand >= deck_size:
        return 1.0
    return 1 - comb(deck_size - copies, hand) / comb(deck_size, hand)


def mulligan_rate(basics: int, deck_size: int = DECK_SIZE, hand: int = OPENING_HAND) -> float:
    """最初の手札に「たね」が1匹も来ない確率 (マリガン率)。

    これが高いデッキは、腕とは関係なく事故る。回らない話の最初に見る数字。
    """
    if basics <= 0:
        return 1.0
    return 1 - draw_probability(basics, deck_size, hand)


def count_basics(deck: DeckEntry, cards: dict[str, dict]) -> int:
    """デッキに入っている「たね」ポケモンの枚数を数える。

    カードの内容 (cards.json) が要る。まだ取っていないカードは数えられない
    ので、数えられた枚数と一緒に扱うこと。
    """
    total = 0
    for name, count in deck.counts.items():
        card = cards.get(deck.card_ids.get(name, ""))
        if card and card.get("stage") == "たね":
            total += count
    return total


# ------------------------------------------------------------------
# 自分のデッキを見る -- 「勝っているデッキと何が違うのか」
# ------------------------------------------------------------------


def compare(counts: dict[str, int], decks: list[DeckEntry]) -> dict:
    """自分のデッキと、勝っているデッキの中身を突き合わせる。

    強いか弱いかを言い切ることはしない。勝っているデッキが何を何枚
    入れているか、自分と何が違うかを並べるところまでにとどめる。
    そこから先は本人が決めること。

    Returns:
        {"total": 比べた相手の数,
         "missing":  よく入っているのに自分には無いカード,
         "extra":    自分だけが入れているカード,
         "different": 枚数が違うカード,
         "sections": 区分ごとの自分の枚数と平均}
    """
    rows = adoption(decks)
    by_name = {row["name"]: row for row in rows}

    missing = [
        {**row, "yours": 0}
        for row in rows
        if row["share"] >= COMMON_SHARE and counts.get(row["name"], 0) == 0
    ]

    extra = [
        {"name": name, "yours": count, "decks": 0, "total": len(decks), "share": 0.0}
        for name, count in sorted(counts.items())
        if name not in by_name
    ]

    different = []
    for name, count in counts.items():
        row = by_name.get(name)
        if not row or count == 0:
            continue
        typical = round(row["average"], 1)
        if abs(count - typical) >= 1:
            different.append({**row, "yours": count, "typical": typical})
    different.sort(key=lambda r: -r["share"])

    return {
        "total": len(decks),
        "missing": missing,
        "extra": extra,
        "different": different,
    }
