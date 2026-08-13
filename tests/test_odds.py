"""確率と「実質枚数」のテスト。

ここが狂うと、子どもに間違った数字を教えることになる。
手で確かめられる小さな例で固定しておく。
"""

from __future__ import annotations

import pytest

from src.pokeca import odds


# ------------------------------------------------------------------
# 確率そのもの
# ------------------------------------------------------------------


def test_引く枚数が山札と同じなら必ず引ける():
    assert odds.at_least_one(1, 60, 60) == 1.0


def test_0枚なら絶対に引けない():
    assert odds.at_least_one(0, 60, 7) == 0.0


def test_4枚を7枚で引ける確率はおよそ40パーセント():
    # 1 - C(56,7)/C(60,7) = 0.399...
    assert odds.at_least_one(4, 60, 7) == pytest.approx(0.3994, abs=1e-3)


def test_後攻の8枚のほうが引きやすい():
    assert odds.at_least_one(4, 60, 8) > odds.at_least_one(4, 60, 7)


def test_2枚以上そろう確率は1枚以上より低い():
    assert odds.at_least(2, 4, 60, 7) < odds.at_least_one(4, 60, 7)


def test_1枚しかないカードは2枚そろわない():
    assert odds.at_least(2, 1, 60, 7) == 0.0


# ------------------------------------------------------------------
# ACE SPEC
# ------------------------------------------------------------------


def test_ACE_SPECを見分けられる():
    assert odds.is_ace_spec({"set": "ACE", "number": "SPEC"})
    assert not odds.is_ace_spec({"set": "M5", "number": "044/081"})


# ------------------------------------------------------------------
# 実質枚数
# ------------------------------------------------------------------

CARDS = {
    "プレシャスキャリー": {"name": "プレシャスキャリー", "section": "グッズ"},
    "ロケット団のラムダ": {"name": "ロケット団のラムダ", "section": "サポート"},
    "ロケット団のレシーバー": {"name": "ロケット団のレシーバー", "section": "グッズ"},
    "ニャースex": {"name": "ニャースex", "section": "ポケモン", "stage": "たね", "hp": 170},
    "ハイパーボール": {"name": "ハイパーボール", "section": "グッズ"},
    "なかよしポフィン": {"name": "なかよしポフィン", "section": "グッズ"},
    "モグリュー": {"name": "モグリュー", "section": "ポケモン", "stage": "たね", "hp": 70},
    "基本鋼エネルギー": {"name": "基本鋼エネルギー", "section": "エネルギー"},
}

RULES = {
    "探す": {
        "ハイパーボール": {"使い方": "グッズ", "出す先": "手札", "区分": ["ポケモン"]},
        "なかよしポフィン": {
            "使い方": "グッズ",
            "出す先": "ベンチ",
            "区分": ["ポケモン"],
            "たね": True,
            "HP上限": 70,
        },
        "ニャースex": {"使い方": "特性", "出す先": "手札", "区分": ["サポート"]},
        "ロケット団のラムダ": {
            "使い方": "サポート",
            "出す先": "手札",
            "区分": ["グッズ", "サポート"],
        },
        "ロケット団のレシーバー": {
            "使い方": "グッズ",
            "出す先": "手札",
            "区分": ["サポート"],
            "名前に": "ロケット団",
        },
    }
}

DECK = {
    "プレシャスキャリー": 1,
    "ロケット団のラムダ": 4,
    "ロケット団のレシーバー": 4,
    "ニャースex": 4,
    "ハイパーボール": 4,
    "なかよしポフィン": 4,
    "モグリュー": 3,
    "基本鋼エネルギー": 36,
}


def test_たどりつけるカードを全部数える():
    reach = odds.reach_to("プレシャスキャリー", DECK, CARDS, RULES)
    # 本体1 + ラムダ4 + レシーバー4 + ニャースex4 + ハイパーボール4 = 17
    assert reach.total == 17
    assert reach.counts["ハイパーボール"] == 4


def test_先攻の1番めはサポートが使えないので実質1枚に戻る():
    reach = odds.reach_to("プレシャスキャリー", DECK, CARDS, RULES, allow_supporter=False)
    assert reach.total == 1


def test_ベンチに出すカードは途中の道にならない():
    # なかよしポフィンはニャースexを山札からベンチに出せないうえ
    # (HP170なので条件外)、そもそもベンチに出す形なので、
    # そこから先の特性にはつながらない。
    reach = odds.reach_to("プレシャスキャリー", DECK, CARDS, RULES)
    assert "なかよしポフィン" not in reach.counts


def test_ベンチに出すカードでも最後の1歩なら数える():
    # モグリューをベンチに出したいだけなら、なかよしポフィンで届く
    reach = odds.reach_to("モグリュー", DECK, CARDS, RULES)
    assert reach.counts.get("なかよしポフィン") == 4
    assert reach.counts.get("ハイパーボール") == 4


def test_入っていないカードには届かない():
    reach = odds.reach_to("入っていないカード", DECK, CARDS, RULES)
    assert reach.total == 0


def test_道すじが説明として出る():
    reach = odds.reach_to("プレシャスキャリー", DECK, CARDS, RULES)
    lines = reach.lines()
    assert any("ロケット団のラムダ 4枚→プレシャスキャリー" == line for line in lines)


def test_実質枚数が増えると確率も上がる():
    reach = odds.reach_to("プレシャスキャリー", DECK, CARDS, RULES)
    naive = odds.at_least_one(1, 60, 8)
    real = odds.at_least_one(reach.total, 60, 8)
    assert naive < 0.15 < 0.9 < real


def test_デッキのIDを名前にまとめる():
    cards = {"1": {"name": "ハイパーボール"}, "2": {"name": "ハイパーボール"}}
    assert odds.deck_counts_of({"1": 2, "2": 2}, cards) == {"ハイパーボール": 4}
