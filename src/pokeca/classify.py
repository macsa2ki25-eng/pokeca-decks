"""デッキの中身 (60枚) から、デッキ名を当てる。

シティリーグの結果には、デッキ名が付いていない。デッキ名はデッキ別の
一覧ページから拾っていて、その一覧はジムバトルのものしか無いため。
前のシーズンも507件すべて名前が無く、シティリーグだけランキングが
出せなかった。

ただしデッキコードは付いているので、60枚の中身は取れる。
同じシーズンのジムバトルには名前と中身の両方がそろっているので、
それを手本にして「中身が一番似ている手本の名前」を付ける。

## やり方

1. デッキを「カード名 → 枚数」にする。基本エネルギーは数えない
   (どのデッキにも入っていて、デッキの見分けに役立たないため)。
   枚数は4で頭打ちにする。
2. どのデッキにも入っているカード (ハイパーボールなど) は軽く、
   一部のデッキにしか入っていないカード (ドロンチなど) は重く数える。
3. デッキ名にカード名が入っている手本は、そのカードがこのデッキに
   入っているときだけ候補にする。「ドラパルトex＋ノココッチ」の手本と
   中身がそっくりでも、ノココッチが入っていなければ「ドラパルトex」になる。
4. 残った手本の中から、中身が近い順に5つ選ぶ。
5. 一番近い1つと、5つの多数決 (3票以上) の名前が一致したときだけ採用する。
   どちらかが割れたら、名前は付けずに空のままにしておく。

名前が付いたジムバトル302件で、1件ずつ抜いて当てさせたところ、
名前を付けた258件のうち255件が正解だった (99%)。付けなかったのは44件。
3 を入れる前は 256件中248件 (97%) で、外れの半分は
「ドラパルトex」と「ドラパルトex＋ノココッチ」の取り違えだった。

## 当てた名前は印を付けて持つ

``DeckResult.deck_name_guessed`` が True の名前は、ここで当てたもの。
収集元が名前を出してきたら、そちらで上書きする (``merge_results``)。
手本は毎日増えるので、当てた名前は毎回いったん外して当て直す。
"""

from __future__ import annotations

import math
import unicodedata
from collections import Counter
from dataclasses import dataclass

from src.pokeca.models import DeckResult, normalize_deck_name

# 手本の中から近い順にいくつ見るか
NEIGHBORS = 5
# 何票そろえば、その名前にしてよいか
MIN_VOTES = 3
# 一番近い手本との近さ (0〜1)。これより遠ければ、似たデッキが無いとみなす
MIN_SIMILARITY = 0.6
# 同じカードを何枚まで数えるか
COUNT_CAP = 4


def features(counts: dict[str, int]) -> dict[str, int]:
    """「カード名 → 枚数」を、見比べに使う形にする。"""
    return {
        name: min(copies, COUNT_CAP)
        for name, copies in counts.items()
        if copies > 0 and not name.startswith("基本")
    }


def _components(deck_name: str) -> list[str]:
    """「ドラパルトex＋ノココッチ」→ ["ドラパルトex", "ノココッチ"]"""
    text = unicodedata.normalize("NFKC", deck_name)
    return [part.strip() for part in text.split("+") if part.strip()]


@dataclass
class Guess:
    deck_name: str
    votes: int  # 近い5つのうち、この名前だったもの
    similarity: float  # 一番近い手本との近さ


class DeckNamer:
    """名前の付いたデッキを手本にして、名前の無いデッキに名前を付ける。

    Args:
        examples: (デッキ名, カード名 → 枚数) の並び
        card_names: カードとして存在する名前。デッキ名の中にカード名が
            入っているのに、そのカードがデッキに無ければ、その名前は付けない
            (「ドラパルトex＋ノココッチ」なのにノココッチが入っていない、など)
    """

    def __init__(
        self,
        examples: list[tuple[str, dict[str, int]]],
        card_names: set[str] | None = None,
    ) -> None:
        self.card_names = card_names or set()
        examples = [(name, counts) for name, counts in examples if name]
        # 手本の元の中身。精度を確かめるとき、手本自身を当てさせるのに使う
        self.counts = [counts for _, counts in examples]
        rows = [(name, features(counts)) for name, counts in examples]
        total = len(rows)
        seen_in = Counter(card for _, feats in rows for card in feats)
        # どのデッキにも入っているカードほど軽くする (1 に近づく)
        self.weight = {
            card: math.log((total + 1) / (n + 1)) + 1 for card, n in seen_in.items()
        }
        self.examples = [(name, self._vector(feats)) for name, feats in rows]

    def _vector(self, feats: dict[str, int]) -> tuple[dict[str, float], float]:
        # 手本に1度も出てこないカードは、見比べに使えないので落とす
        vec = {
            card: copies * self.weight[card]
            for card, copies in feats.items()
            if card in self.weight
        }
        norm = math.sqrt(sum(v * v for v in vec.values()))
        return vec, norm

    def _similarity(self, a, b) -> float:
        (va, na), (vb, nb) = a, b
        if not na or not nb:
            return 0.0
        if len(va) > len(vb):
            va, vb = vb, va
        return sum(v * vb.get(card, 0.0) for card, v in va.items()) / (na * nb)

    def _fits(self, deck_name: str, counts: dict[str, int]) -> bool:
        """デッキ名に出てくるカードが、そのデッキに入っているか。"""
        return all(
            part in counts
            for part in _components(deck_name)
            if part in self.card_names
        )

    def guess(self, counts: dict[str, int], *, skip: int | None = None) -> Guess | None:
        """名前を当てる。自信が無ければ None。

        ``skip`` は手本の何番目を除いて考えるか。手本自身を当てさせて
        精度を確かめるとき (1件ずつ抜いて試すとき) に使う。
        """
        query = self._vector(features(counts))
        if not query[1]:
            return None
        fits = {name: self._fits(name, counts) for name, _ in self.examples}
        scored = sorted(
            (
                (self._similarity(query, vec), name)
                for i, (name, vec) in enumerate(self.examples)
                if i != skip and fits[name]
            ),
            key=lambda pair: -pair[0],
        )[:NEIGHBORS]
        if not scored:
            return None

        top_similarity, top_name = scored[0]
        if top_similarity < MIN_SIMILARITY:
            return None
        votes = Counter(name for _, name in scored)
        leader, count = votes.most_common(1)[0]
        # 1番近いものと多数決が食いちがうときは、どちらとも決めない
        if leader != top_name or count < MIN_VOTES:
            return None
        return Guess(deck_name=leader, votes=count, similarity=round(top_similarity, 3))


def deck_counts(deck: dict, cards: dict[str, dict]) -> dict[str, int]:
    """保存形のデッキ (カードID と 枚数) を「カード名 → 枚数」にする。"""
    counts: dict[str, int] = {}
    for card_id, copies in deck.get("cards", []):
        name = cards.get(str(card_id), {}).get("name")
        if name:
            counts[name] = counts.get(name, 0) + copies
    return counts


def build_namer(
    results: list[DeckResult], decklists: dict[str, dict], cards: dict[str, dict]
) -> DeckNamer:
    """収集元が名前を出している結果だけを手本にする。

    当てた名前を手本に混ぜると、1度の当てちがいが次の当てちがいを呼ぶ。
    """
    examples = [
        (r.deck_name, deck_counts(decklists[r.deck_code], cards))
        for r in results
        if r.deck_name and not r.deck_name_guessed and r.deck_code in decklists
    ]
    card_names = {c.get("name") for c in cards.values() if c.get("name")}
    return DeckNamer(examples, card_names)


def name_decks(
    results: list[DeckResult], decklists: dict[str, dict], cards: dict[str, dict]
) -> tuple[int, int]:
    """名前の無い結果に、中身から当てた名前を付ける。

    前に当てた名前はいったん外して、今の手本で当て直す。
    収集元が付けた名前には触らない。

    Returns:
        (名前を付けた件数, 中身はあるのに名前を決められなかった件数)
    """
    for record in results:
        if record.deck_name_guessed:
            record.deck_name = ""
            record.deck_key = ""
            record.deck_name_guessed = False

    namer = build_namer(results, decklists, cards)
    named = undecided = 0
    for record in results:
        if record.deck_name or record.deck_code not in decklists:
            continue
        guess = namer.guess(deck_counts(decklists[record.deck_code], cards))
        if guess is None:
            undecided += 1
            continue
        record.deck_name = guess.deck_name
        record.deck_key = normalize_deck_name(guess.deck_name)
        record.deck_name_guessed = True
        named += 1
    return named, undecided


def leave_one_out(namer: DeckNamer) -> tuple[int, int, int, Counter]:
    """手本を1件ずつ抜いて当てさせ、どれだけ当たるかを数える。

    Returns:
        (当たり, はずれ, 決めなかった, はずれの内訳 Counter[(正解, 当てた名前)])
    """
    right = wrong = skipped = 0
    mistakes: Counter = Counter()
    for i, (name, _) in enumerate(namer.examples):
        guess = namer.guess(namer.counts[i], skip=i)
        if guess is None:
            skipped += 1
        elif guess.deck_name == name:
            right += 1
        else:
            wrong += 1
            mistakes[(name, guess.deck_name)] += 1
    return right, wrong, skipped, mistakes
