"""デッキの中身から名前を当てる仕組みのテスト。

シティリーグのランキングは、ここで付けた名前で数える。
当てずっぽうで名前を付けると、子どもに「はやっているデッキ」を
まちがって教えることになるので、「決めない」側の動きも固定しておく。
"""

from __future__ import annotations

from src.pokeca import classify
from src.pokeca.models import DeckResult

DRAGAPULT = {"ドラパルトex": 3, "ドロンチ": 4, "ドラメシヤ": 4, "ハイパーボール": 4, "基本超エネルギー": 6}
DRAGAPULT_NOKO = {**DRAGAPULT, "ノココッチ": 1, "ネストボール": 3, "ノコッチ": 2}
LUCARIO = {"メガルカリオex": 3, "リオル": 4, "ハイパーボール": 4, "基本闘エネルギー": 10}
CARD_NAMES = {"ドラパルトex", "ドロンチ", "ドラメシヤ", "ノココッチ", "メガルカリオex", "リオル"}


def _examples() -> list[tuple[str, dict[str, int]]]:
    return (
        [("ドラパルトex", DRAGAPULT)] * 4
        + [("ドラパルトex＋ノココッチ", DRAGAPULT_NOKO)] * 4
        + [("メガルカリオex", LUCARIO)] * 4
    )


def _namer(examples=None) -> classify.DeckNamer:
    return classify.DeckNamer(examples or _examples(), CARD_NAMES)


def test_中身がそっくりなデッキの名前を付ける():
    guess = _namer().guess(dict(LUCARIO))
    assert guess is not None
    assert guess.deck_name == "メガルカリオex"
    assert guess.votes >= classify.MIN_VOTES


def test_名前に出てくるカードが無ければその名前にしない():
    # ノココッチ入りの手本とほぼ同じでも、ノココッチが抜けていれば
    # 「ドラパルトex＋ノココッチ」とは呼ばない
    deck = {k: v for k, v in DRAGAPULT_NOKO.items() if k != "ノココッチ"}
    guess = _namer().guess(deck)
    assert guess is not None
    assert guess.deck_name == "ドラパルトex"


def test_名前に出てくるカードが入っていればその名前になる():
    guess = _namer().guess(dict(DRAGAPULT_NOKO))
    assert guess is not None
    assert guess.deck_name == "ドラパルトex＋ノココッチ"


def test_似ている手本が無ければ名前を付けない():
    assert _namer().guess({"フーディン": 3, "ユンゲラー": 4, "ケーシィ": 4}) is None


def test_基本エネルギーだけでは決めない():
    assert _namer().guess({"基本超エネルギー": 60}) is None


def test_手本が2件しかない名前は付けない():
    # 3票そろわないので、そっくりでも決めない
    examples = _examples() + [("マリィのオーロンゲex", {"マリィのオーロンゲex": 3, "マリィのベロバー": 4})] * 2
    namer = classify.DeckNamer(examples, CARD_NAMES | {"マリィのオーロンゲex"})
    assert namer.guess({"マリィのオーロンゲex": 3, "マリィのベロバー": 4}) is None


def test_一番近い手本と多数決が食いちがえば決めない():
    near = {**DRAGAPULT, "ヨノワール": 1}
    examples = (
        [("ドラパルトex", near)]
        + [("ドラパルトex＋ヨノワール", {**near, "ヨマワル": 4, "サマヨール": 3})] * 3
        + [("メガルカリオex", LUCARIO)] * 4
    )
    namer = classify.DeckNamer(examples, CARD_NAMES)
    # 1番近いのは「ドラパルトex」だが、5つの多数決は「＋ヨノワール」
    assert namer.guess(dict(near)) is None


def test_手本を1件ずつ抜いて精度を数えられる():
    right, wrong, skipped, mistakes = classify.leave_one_out(_namer())
    assert right + wrong + skipped == 12
    assert wrong == 0


# ------------------------------------------------------------------
# 結果に名前を付ける
# ------------------------------------------------------------------

CARDS = {
    str(i): {"name": name}
    for i, name in enumerate(
        ["ドラパルトex", "ドロンチ", "ドラメシヤ", "ハイパーボール", "基本超エネルギー",
         "メガルカリオex", "リオル", "基本闘エネルギー"],
        start=1,
    )
}
IDS = {c["name"]: i for i, c in CARDS.items()}


def _deck(counts: dict[str, int]) -> dict:
    return {"cards": [[IDS[name], n] for name, n in counts.items() if name in IDS]}


def _result(code: str, name: str = "", event: str = "gym", guessed: bool = False) -> DeckResult:
    return DeckResult(
        date="2026-09-26", store="", rank=1, deck_name=name,
        event_type=event, deck_code=code, deck_name_guessed=guessed,
    )


def _library() -> tuple[list[DeckResult], dict[str, dict]]:
    results, decklists = [], {}
    for i in range(4):
        results.append(_result(f"D{i}", "ドラパルトex"))
        decklists[f"D{i}"] = _deck(DRAGAPULT)
        results.append(_result(f"L{i}", "メガルカリオex"))
        decklists[f"L{i}"] = _deck(LUCARIO)
    return results, decklists


def test_名前の無いシティリーグに名前を付けて印を付ける():
    results, decklists = _library()
    city = _result("C1", event="city")
    decklists["C1"] = _deck(LUCARIO)
    named, undecided = classify.name_decks([*results, city], decklists, CARDS)
    assert (named, undecided) == (1, 0)
    assert city.deck_name == "メガルカリオex"
    assert city.deck_key == "メガルカリオex"
    assert city.deck_name_guessed


def test_収集元が付けた名前には触らない():
    results, decklists = _library()
    # 中身はルカリオだが、収集元が別の名前を付けている
    odd = _result("X1", "ドラパルトex")
    decklists["X1"] = _deck(LUCARIO)
    classify.name_decks([*results, odd], decklists, CARDS)
    assert odd.deck_name == "ドラパルトex"
    assert not odd.deck_name_guessed


def test_前に当てた名前は当て直す():
    results, decklists = _library()
    stale = _result("C1", "ドラパルトex", event="city", guessed=True)
    decklists["C1"] = _deck(LUCARIO)
    classify.name_decks([*results, stale], decklists, CARDS)
    assert stale.deck_name == "メガルカリオex"
    assert stale.deck_name_guessed


def test_決められなければ前に当てた名前も外す():
    results, decklists = _library()
    stale = _result("C1", "ドラパルトex", event="city", guessed=True)
    decklists["C1"] = _deck({"基本超エネルギー": 60})
    named, undecided = classify.name_decks([*results, stale], decklists, CARDS)
    assert (named, undecided) == (0, 1)
    assert stale.deck_name == ""
    assert not stale.deck_name_guessed


def test_当てた名前は手本に使わない():
    results, decklists = _library()
    guessed = _result("G1", "フーディン", event="city", guessed=True)
    decklists["G1"] = _deck(LUCARIO)
    namer = classify.build_namer([*results, guessed], decklists, CARDS)
    assert "フーディン" not in {name for name, _ in namer.examples}


def test_中身をまだ取っていない結果はそのまま():
    results, decklists = _library()
    city = _result("C9", event="city")
    named, undecided = classify.name_decks([*results, city], decklists, CARDS)
    assert (named, undecided) == (0, 0)
    assert city.deck_name == ""
