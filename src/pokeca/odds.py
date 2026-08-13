"""確率と「実質枚数」。

ポケモンカードは確率のゲームです。
「4枚入れたカードを最初の7枚で引ける確率は?」は計算で正確に出ますが、
その計算をそのまま信じると読み違えます。ロケット団のラムダ4枚のデッキでも、
ロケット団のレシーバーやニャースexやハイパーボールからラムダにたどりつけるなら、
実際には16枚入っているのと同じだからです。

このモジュールは2つのことをします。

1. 山札からカードを引く確率 (超幾何分布)
2. どのカードからどのカードにたどりつけるかをたどって、「実質枚数」を数える

2 の材料は ``data/pokeca/reach.yaml`` に書いてあります。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import comb
from pathlib import Path

import yaml

DECK_SIZE = 60
OPENING_HAND = 7

ROOT = Path(__file__).resolve().parent.parent.parent
REACH_FILE = ROOT / "data" / "pokeca" / "reach.yaml"

# ルールを持つポケモン (倒されるとサイドを2枚以上わたす) の見分け方。
# カード名の終わりが ex かどうかで判定する。今の環境ではこれで足りる。
RULE_SUFFIXES = ("ex", "V", "VSTAR", "VMAX", "GX")


def at_least_one(hits: int, deck_size: int = DECK_SIZE, draws: int = OPENING_HAND) -> float:
    """山札 deck_size 枚に hits 枚あるカードが、draws 枚の中に1枚以上ある確率。"""
    if hits <= 0 or deck_size <= 0 or draws <= 0:
        return 0.0
    if hits >= deck_size or draws >= deck_size:
        return 1.0
    return 1 - comb(deck_size - hits, draws) / comb(deck_size, draws)


def at_least(k: int, hits: int, deck_size: int = DECK_SIZE, draws: int = OPENING_HAND) -> float:
    """k枚以上ある確率。2枚そろえたいカードを見るときに使う。"""
    if k <= 0:
        return 1.0
    if hits < k or draws < k or deck_size <= 0:
        return 0.0
    total = comb(deck_size, draws)
    below = sum(
        comb(hits, i) * comb(deck_size - hits, draws - i)
        for i in range(0, min(k, hits + 1))
        if draws - i >= 0
    )
    return 1 - below / total


def is_rule_pokemon(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in RULE_SUFFIXES)


def is_ace_spec(card: dict) -> bool:
    """ACE SPEC かどうか。

    同じ名前のカードは、ふつうデッキに4枚まで入れられる (エネルギーは別)。
    ACE SPEC はデッキに1枚までしか入れられないかわりに、
    1枚で試合が動くくらい強く作られている。

    公式のデッキページはカード名のうしろに「(ACE SPEC)」と付けるので、
    収録セットのところに ACE / SPEC と分かれて入っている。
    """
    return card.get("set") == "ACE" and card.get("number") == "SPEC"


def load_reach() -> dict:
    if not REACH_FILE.exists():
        return {"探す": {}, "始動": {}}
    with REACH_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {"探す": {}, "始動": {}}


def _matches(rule: dict, card: dict) -> bool:
    """探すカードの条件に、そのカードが当てはまるか。"""
    section = card.get("section") or ""
    name = card.get("name") or ""

    sections = rule.get("区分") or []
    if sections and section not in sections:
        return False
    if rule.get("たね") and card.get("stage") != "たね":
        return False
    if rule.get("進化") and card.get("stage") in ("たね", None, ""):
        return False
    limit = rule.get("HP上限")
    if limit is not None and (not card.get("hp") or card["hp"] > limit):
        return False
    if rule.get("非ルール") and is_rule_pokemon(name):
        return False
    if rule.get("ex") and not name.endswith("ex"):
        return False
    keyword = rule.get("名前に")
    if keyword and keyword not in name:
        return False
    kind = rule.get("タイプ")
    if kind and card.get("type") != kind:
        return False
    return True


@dataclass
class Reach:
    """ある1枚に、山札のどのカードからたどりつけるか。"""

    target: str
    counts: dict[str, int] = field(default_factory=dict)  # カード名 → 枚数
    paths: dict[str, list[str]] = field(default_factory=dict)  # カード名 → 通り道

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def lines(self, limit: int = 6) -> list[str]:
        """「ハイパーボール 4枚 (→ニャースex→…)」の形で説明を作る。"""
        rows = sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))
        out = []
        for name, copies in rows[:limit]:
            path = self.paths.get(name) or []
            via = "".join(f"→{step}" for step in path)
            out.append(f"{name} {copies}枚{via}")
        return out


def reach_to(
    target: str,
    deck_counts: dict[str, int],
    cards_by_name: dict[str, dict],
    reach_rules: dict | None = None,
    allow_supporter: bool = True,
) -> Reach:
    """target にたどりつけるカードを、デッキの中から全部集める。

    target から逆向きに、幅優先でたどる。守る決まりは2つ。

    1. サポートは1つの番に1枚しか使えない。
       だから1本のつながりの中でサポートを通れるのは1回まで。
       ``allow_supporter=False`` は「先攻の1番めでサポートが使えない」場合。
    2. 山札から直接ベンチに出すカードは、そこから先につなげられない。
       ニャースexの特性は「手札からベンチに出したとき」なので、
       なかよしポフィンで山札から直接出しても特性が使えないため。
    """
    rules = (reach_rules or load_reach()).get("探す") or {}
    result = Reach(target=target)

    if target not in deck_counts:
        return result

    # want: いま手に入れたいカード
    # need_hand: それを「手札に」持ってこないと先に進めないか
    #            (途中のカードは必ず手札から使うので True)
    # used_supporter: サポートをもう1枚使ったか
    seen = {(target, False, False)}
    queue = [(target, False, False, [])]

    while queue:
        want, need_hand, used_supporter, path = queue.pop(0)
        card = cards_by_name.get(want)
        if not card:
            continue

        for name, copies in deck_counts.items():
            if name == want:
                continue
            rule = rules.get(name)
            if not rule or not _matches(rule, card):
                continue
            if need_hand and rule.get("出す先") == "ベンチ":
                continue  # ベンチに出されると、そのカードの特性が使えない
            needs_supporter = rule.get("使い方") == "サポート"
            if needs_supporter and (used_supporter or not allow_supporter):
                continue

            state = (name, True, used_supporter or needs_supporter)
            if state in seen:
                continue
            seen.add(state)
            new_path = [want, *path]
            # 同じカードを2通りで見つけたら、先に見つかった短いほうを残す
            if name not in result.counts:
                result.counts[name] = copies
                result.paths[name] = new_path
            # ここから先、name は手札から使うことになる
            queue.append((name, True, used_supporter or needs_supporter, new_path))

    # target 自身も当然たどりつける
    result.counts[target] = deck_counts[target]
    result.paths[target] = []
    return result


def deck_counts_of(deck, cards: dict[str, dict]) -> dict[str, int]:
    """デッキ(カードID→枚数)を、カード名→枚数 に直す。

    再録でIDが変わるので、数えるときは名前でまとめる。
    """
    counts: dict[str, int] = {}
    for card_id, copies in deck.items():
        name = cards.get(str(card_id), {}).get("name")
        if name:
            counts[name] = counts.get(name, 0) + copies
    return counts


def startup_odds(
    deck_name: str,
    deck_counts_list: list[dict[str, int]],
    names: dict[str, dict],
    reach_rules: dict | None = None,
    draws: int = 8,
) -> list[dict]:
    """そのデッキが「1番めに引きたいカード」に届く確率をまとめる。

    優勝デッキ1つ1つについて実質枚数を数えて、中央値の形を代表として出す。
    平均を使うと、そのカードを入れていないデッキ (実質0枚) に引っぱられるため。
    """
    import statistics

    rules = reach_rules or load_reach()
    wanted = (rules.get("始動") or {}).get(deck_name) or []
    if not wanted or not deck_counts_list:
        return []

    out = []
    for item in wanted:
        target = item.get("カード")
        if not target:
            continue
        reaches = [
            reach_to(target, counts, names, rules)
            for counts in deck_counts_list
            if target in counts
        ]
        if not reaches:
            continue
        totals = sorted(r.total for r in reaches)
        typical = int(statistics.median(totals))
        # 実質枚数が中央値と同じデッキを、説明用の見本にする。
        # 「入っている枚数」も同じ見本から取る。ちがうデッキの数字を
        # 並べると、見比べたときに話が合わなくなるため。
        sample = min(reaches, key=lambda r: (abs(r.total - typical), -r.total))
        plain = sample.counts.get(target, 0)
        out.append(
            {
                "card": target,
                "why": item.get("ねらい", ""),
                "copies": int(plain),
                "effective": typical,
                "decks": len(reaches),
                "p_plain": round(at_least_one(int(plain), DECK_SIZE, draws) * 100),
                "p_reach": round(at_least_one(typical, DECK_SIZE, draws) * 100),
                "draws": draws,
                "how": sample.lines(5),
            }
        )
    return out


def cards_by_name(cards: dict[str, dict]) -> dict[str, dict]:
    """カード名 → カード1枚ぶんの中身。同じ名前は最初のものを使う。"""
    out: dict[str, dict] = {}
    for card in cards.values():
        name = card.get("name")
        if name and name not in out:
            out[name] = card
    return out
