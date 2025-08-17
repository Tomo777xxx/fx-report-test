# modules/validator.py
from __future__ import annotations
import re

PAIR_ALLOWED = ["ドル円","ユーロドル","ユーロ円","ポンドドル","ポンド円",
                "金/米ドル","ビットコイン/米ドル","豪ドル米ドル","NZドル米ドル"]

def enforce_symbols(text: str) -> str:
    """記号・表記の強制整形（%半角、時刻ゼロ埋めなし、「ほど→約」の条件置換）。"""
    t = text.replace("％", "%")
    # 08:50 → 8:50（時の先頭ゼロ除去）
    t = re.sub(r"\b0(\d):", r"\1:", t)
    # 「数値 + % + ほど + 動詞」のときだけ「約」を挿入
    t = re.sub(r"(\d+(?:\.\d+)?)%ほど(上昇|低下|下落|上振れ|下振れ)", r"約\1%\2", t)
    return t

def validate_layout(text: str) -> list[str]:
    """段落・改行・最後の言い切りなどの体裁チェック。問題があればエラー文を返す。"""
    errors = []
    # 本日のポイント直後に空行があればNG
    if re.search(r"本日のポイント\s*\n\s*\n", text):
        errors.append("『本日のポイント』の直後に空白行があります（禁止）。")
    if text.count("本日のポイント") == 0:
        errors.append("見出し『本日のポイント』がありません。")

    # ダブル改行でざっくり段落抽出
    blocks = [b for b in text.split("\n\n") if b.strip()]
    if len(blocks) < 5:
        errors.append("段落が不足しています（タイトル/ポイント/本文①/本文②/指標1行）。")

    # 最後のブロック＝本文③（指標）は1行のみ
    last_block = blocks[-1] if blocks else ""
    if "\n" in last_block.strip():
        errors.append("本文③（指標）は改行なしの1行で書いてください。")

    # 最後は句点で終わる（タイトル回収）
    if not text.strip().endswith("。"):
        errors.append("最後は句点で終えてください（タイトル回収の言い切り）。")

    return errors

# --- 文字数下限チェック用ヘルパ ---
def _get_paragraphs(text: str):
    blocks = [b for b in text.split("\n\n") if b.strip()]
    if len(blocks) < 5:
        return None, None, None
    # 0:タイトル行, 1:本日のポイント, 2:段落①, 3:段落②, 4:段落③（指標1行）
    return blocks[2], blocks[3], blocks[4]

def validate_char_min(text: str, *, p1_min: int, p2_min: int, p3_min: int) -> list[str]:
    """段落①/②/③の最小文字数を検証。足りなければメッセージを返す。"""
    errors = []
    p1, p2, p3 = _get_paragraphs(text)
    if p1 is None:
        return ["段落を抽出できません（体裁を確認）。"]
    c1 = len(p1.replace("\n",""))
    c2 = len(p2.replace("\n",""))
    c3 = len(p3.replace("\n",""))
    if c1 < p1_min: errors.append(f"段落①が{p1_min}字未満（現在{c1}字）")
    if c2 < p2_min: errors.append(f"段落②が{p2_min}字未満（現在{c2}字）")
    if c3 < p3_min: errors.append(f"段落③が{p3_min}字未満（現在{c3}字）")
    return errors

