# modules/writer.py
from __future__ import annotations

TEMPLATE = """タイトル：{title}

本日のポイント
{point1}
{point2}

{para1}

{para2}

本日の指標は、{calendar_line}が発表予定となっている。{title_recall}"""

# タイトルの語尾 → 回収文の語尾（断言禁止の安全マッピング）
_SUFFIX_TO_RECALL = {
    "注視か":   "注視したい。",
    "要注目か": "注目したい。",
    "注目か":   "注目したい。",
    "警戒か":   "警戒したい。",
    "注意か":   "注意したい。",
    "要注意か": "要注意としたい。",
    "静観か":   "静観したい。",
    "見極めたいところか": "見極めたい。",
    "見定めたいところか": "見定めたい。",
}

def build_title_recall(title: str) -> str:
    t = title.strip()
    for suf, recall in _SUFFIX_TO_RECALL.items():
        if t.endswith(suf):
            return t[:-len(suf)] + recall
    # 「〜か」で終わるが表現が辞書外のとき：最も無難な注視に倒す
    if t.endswith("か"):
        return t[:-1] + "に注視したい。"
    # 句点がなければ付与（断言文にはしない）
    if not t.endswith("。"):
        return t + "に注視したい。"
    return t

def render_report(*, title:str, point1:str, point2:str, para1:str, para2:str, calendar_line:str, title_recall:str) -> str:
    return TEMPLATE.format(
        title=title.strip(),
        point1=point1.strip(),
        point2=point2.strip(),
        para1=para1.strip(),
        para2=para2.strip(),
        calendar_line=calendar_line.strip(),
        title_recall=title_recall.strip()
    )

