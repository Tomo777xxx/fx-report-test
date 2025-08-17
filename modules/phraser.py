# modules/phraser.py
from __future__ import annotations
import random

# 注意喚起・観測系の結びバリエーション（断言を避ける）
CLOSERS = [
    "方向感を見極めたい。",
    "行方を注視したい。",
    "値動きには警戒したい。",
    "一段の変動に要注意としたい。",
    "当面は静観としたい。",
    "続く材料待ちの様相を注視したい。",
    "戻り売り/押し目買いの強弱を見定めたい。",
    "需給の傾きに注意したい。",
    "均衡が崩れるかを見守りたい。",
]

def pick_closer(seed: int | None = None) -> str:
    if seed is not None:
        random.seed(seed)
    return random.choice(CLOSERS)
