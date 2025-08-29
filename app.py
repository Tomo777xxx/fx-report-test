"""
相場レポート自動生成アプリ（モック版） — 開発者向けサマリー
================================================================

■ 目的
- 海外FXブローカー向けの日本語相場レポートを、毎日・短時間・安定品質で生成する。
- “嘘のない数値”を最優先：価格・指標・時刻はプログラム計算/既知データのみ採用。
- LLMは「言い回しの整形・情報の整理」のみに限定（自由生成の数値は封じる）。

■ 出力の型（サンプル準拠）
- タイトル（主役ペア + 安全語尾）  
- 段落①：市況サマリー（事実ベース、目安220字以上）  
- 段落②：テクニカル（SMA/EMA/BB/ADX 等、目安180字以上。安全語尾で締め）  
- 段落③：本日の指標（JST時刻順の一行列挙）＋タイトル回収（句点で終える）

■ 画面ステップの流れ（Lite：テンプレ生成／Pro：整形のみLLM）
1) 参照PDFの確認 … サンプル／正典の存在チェック  
1.5) （任意）PDF→ルール要約の自動抽出 … 正典PDFから要旨を抽出し rules_digest.txt を更新  
2) イベント候補の確認 … 候補の読み込み状況のみ表示（選択や重要度決定は後で）  
3) 本文の下書き … facts（証拠パック）とテクニカル計算で①②を生成（LLMは自由生成しない）  
4) 指標候補（TopN + チェック） … LLMは“再ランク付け”のみ。最終選択は人が行う  
5) 本日のポイント（2件選択） … UIで必ず「ちょうど2件」を選ばせる（本文①/③へ反映）  
6) プレビュー（公開体裁 + 自動チェック） … JST時刻順・ホワイトリスト語尾・タイトル回収など検査

■ セキュリティ/禁則
- 売買助言・煽り・断定表現は禁止。  
- タイトル語尾・段落②の結びはホワイトリストからのみ選択。  
- LLMの自由生成はさせない（新規の数値・固有名詞は全禁止）。  
- 体裁・語尾・記号・全角/半角などは自動チェック。

■ データソース
- 価格等：yfinance 等（テクニカルはSMA/EMA/BB/ADXの内部計算）  
- 指標API：FxON API（失敗時はCSV→ダミーの順にフォールバック）  
- ルール要約：data/rules_digest.txt（補助）  
- 段落②の安全文：data/para2_boilerplate.yaml（短文時の補強）

■ 秘密情報の取得順（_get_api_key）
1) st.secrets["OPENAI_API_KEY"]  
2) st.secrets["general"]["OPENAI_API_KEY"]  
3) 環境変数 OPENAI_API_KEY  
※ TradingEconomics のキー等も st.secrets を優先

■ NFP（米雇用統計）
- data/bls_empsit_schedule.yaml（BLS公式日程）を優先、無い月は“第1金曜”の目安で表示。
- tools/update_bls_schedule.py で週1自動更新（失敗時は前回値を保持し動作継続）。

■ 保存/履歴
- data/out/ に公開テキストを保存（BOM付きUTF-8）。  
- data/history.jsonl にメタ（タイトル/ポイント/指標/次回NFP等）を1行追記。

■ 開発向け
- show_debug チェックで診断ログを展開（通常非表示）。  
- 例外はUIで明示（st.error / st.warning）。  
- 小さな変更は“1回に1変更”で進める方針。
"""



# ===== 必要インポート（安全版・重複なし） =====
from __future__ import annotations

# 標準ライブラリ
import os
from pathlib import Path
from uuid import uuid4
import json
import random
import re
from datetime import datetime, date, timezone as _tz, timedelta as _td

# サードパーティ（必須系）
import pandas as pd
import numpy as np
import streamlit as st

import yfinance as yf


# サードパーティ（任意系：未導入でもアプリは落とさない）
try:
    import yaml  # 設定YAMLがあれば使う
except Exception:
    yaml = None

# 祝日・市場カレンダー用（未導入なら None にして“未判定”表示）
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

try:
    import jpholiday  # 日本の祝日
except Exception:
    jpholiday = None

try:
    import exchange_calendars as xc  # NYSEカレンダー
except Exception:
    xc = None

# ===== インポートここまで =====


CANON_JST = _tz(_td(hours=9))

def CANON_get_secret(name: str, default: str | None = None) -> str | None:
    """
    secrets → secrets['general'] → 環境変数 → default の順で取得。
    既存の _get_secret 系は当面そのまま／後続ステップで徐々に置換予定。
    """
    try:
        import streamlit as _st
        if name in _st.secrets:
            return _st.secrets[name]
        if "general" in _st.secrets and name in _st.secrets["general"]:
            return _st.secrets["general"][name]
    except Exception:
        pass
    import os as _os
    return _os.environ.get(name, default)
# 目的：Country名の表記ゆれを地域コード（US/JP/EU/UK/AU/NZ/CN/ZA…）に正規化する“正”の関数。今後はこの関数のみを参照する

def CANON_map_country_to_region(country: str) -> str:

    """
    Country文字列 → 地域コード（US/JP/EU/UK/AU/NZ/CN/ZA…）。
    既存の _map_country_to_region との差分は無し（整形のみ）。
    """
    c = (country or "").strip().upper()
    table = {
        "UNITED STATES": "US", "USA": "US", "U S A": "US",
        "JAPAN": "JP",
        "EURO AREA": "EU", "EUROZONE": "EU",
        "UNITED KINGDOM": "UK", "GREAT BRITAIN": "UK",
        "AUSTRALIA": "AU",
        "NEW ZEALAND": "NZ",
        "CHINA": "CN",
        "SOUTH AFRICA": "ZA",
        "GERMANY": "DE",
        "FRANCE": "FR",
        "ITALY": "IT",
        "CANADA": "CA",
        "SPAIN": "ES",
        "SWITZERLAND": "CH",
    }
    if c in table:
        return table[c]
    # 句読点・余分な空白を落として再判定
    c2 = c.replace(".", "").replace("  ", " ").strip()
    if c2 in table:
        return table[c2]
    # 最後の最後は先頭2文字
    return c[:2] if len(c) >= 2 else ""


# --- 任意 modules のやわらか import（無ければフォールバックを後で使う） ---
try:
    from modules.writer import render_report, build_title_recall as _build_title_recall_from_mod
except Exception:
    render_report = None
    _build_title_recall_from_mod = None

try:
    from modules.validator import enforce_symbols as _enforce_symbols_from_mod, validate_layout as _validate_layout_from_mod, validate_char_min as _validate_char_min_from_mod
except Exception:
    _enforce_symbols_from_mod = None
    _validate_layout_from_mod = None
    _validate_char_min_from_mod = None

try:
    from modules.phraser import pick_closer as _pick_closer_from_mod, CLOSERS as _CLOSERS_FROM_MOD
except Exception:
    _pick_closer_from_mod = None
    _CLOSERS_FROM_MOD = None
# ===== TradingEconomics カレンダー取得（キー無しでも guest:guest で試行） =====
from datetime import timezone, timedelta


# --- NFP（米雇用統計）の日付計算：原則「毎月第1金曜」ベースの目安 ---
def _first_friday(year: int, month: int) -> date:
    cal = calendar.Calendar()
    for d in cal.itermonthdates(year, month):
        if d.month == month and d.weekday() == 4:  # 0=Mon ... 4=Fri
            return d
    raise RuntimeError("first Friday not found")

def next_nfp_date(today: date) -> date:
    """原則：毎月第1金曜。today が第1金曜の前日以前なら今月、当日を過ぎていれば翌月（目安）。"""
    ff = _first_friday(today.year, today.month)
    if today <= ff:
        return ff
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return _first_friday(y, m)

# --- 既定CFG（config.yamlが無い/壊れている場合の安全起動用） ---
_DEFAULT_CFG = {
    "app": {"title": "相場レポート自動生成（Streamlit）"},
    "pdf_paths": [
        "PDF.data/サンプル相場レポート.pdf",
        "PDF.data/公開用体裁ルール_正典_v1.1_2025-08-12.pdf",
    ],
    "events_csv_path": "data/events_today.csv",
    "text_guards": {"p1_min_chars": 220, "p2_min_chars": 180},
}

def _deep_update(dst: dict, src: dict) -> dict:
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)
        else:
            dst[k] = v
    return dst

try:
    with open("config.yaml", "r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    CFG = _deep_update(_DEFAULT_CFG.copy(), user_cfg)
except FileNotFoundError:
    CFG = _DEFAULT_CFG.copy()
except Exception as e:
    st.warning(f"config.yaml の読み込みで問題が発生しました（既定値で起動）：{e}")
    CFG = _DEFAULT_CFG.copy()

st.set_page_config(page_title=CFG["app"]["title"], layout="centered")
st.title(CFG["app"]["title"])
# --- BLS公式の発表日(YAML) → 次回NFPを出す（無ければ従来ルールへフォールバック） ---
def _load_bls_empsit_schedule(path: str | Path = "data/bls_empsit_schedule.yaml") -> list[date]:
    """ローカルYAMLからBLSの公式発表日を読み込んでdate配列で返す。失敗時は[]。"""
    # yaml が未インストールでも落ちないように（ヘッダで yaml=None 安全化済み）
    if yaml is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        dates: list[date] = []
        for year, items in (data.items() if isinstance(data, dict) else []):
            for s in (items or []):
                try:
                    d = datetime.strptime(str(s), "%Y-%m-%d").date()
                    dates.append(d)
                except Exception:
                    pass
        return sorted(dates)
    except Exception:
        return []

def next_nfp_official_or_rule(today: date) -> date:
    """
    1) data/bls_empsit_schedule.yaml の公式日程に当日以降があればそれを使用
    2) 見つからなければ既存のルール next_nfp_date(today) にフォールバック
    """
    sched = _load_bls_empsit_schedule()
    for d in sched:
        if d >= today:
            return d
    return next_nfp_date(today)
# === 主役ペア → yfinance ティッカー対応（サイドバーの候補に完全対応） ===
PAIR_TO_TICKER = {
    # 為替（対円）
    "ドル円":             {"1d": "USDJPY=X", "4h": "USDJPY=X"},
    "ユーロ円":           {"1d": "EURJPY=X", "4h": "EURJPY=X"},
    "ポンド円":           {"1d": "GBPJPY=X", "4h": "GBPJPY=X"},
    "豪ドル円":           {"1d": "AUDJPY=X", "4h": "AUDJPY=X"},
    "NZドル円":           {"1d": "NZDJPY=X", "4h": "NZDJPY=X"},
    "カナダドル円":       {"1d": "CADJPY=X", "4h": "CADJPY=X"},
    "スイスフラン円":     {"1d": "CHFJPY=X", "4h": "CHFJPY=X"},
    "メキシコペソ円":     {"1d": "MXNJPY=X", "4h": "MXNJPY=X"},
    "南アフリカランド円": {"1d": "ZARJPY=X", "4h": "ZARJPY=X"},

    # 為替（対米ドル）
    "ユーロドル":         {"1d": "EURUSD=X", "4h": "EURUSD=X"},
    "ポンドドル":         {"1d": "GBPUSD=X", "4h": "GBPUSD=X"},
    "豪ドル米ドル":       {"1d": "AUDUSD=X", "4h": "AUDUSD=X"},
    "米ドルフラン":       {"1d": "USDCHF=X", "4h": "USDCHF=X"},

    # コモディティ / 仮想通貨
    "金/米ドル":          {"1d": "XAUUSD=X", "4h": "XAUUSD=X"},
    "ビットコイン/米ドル": {"1d": "BTC-USD",   "4h": "BTC-USD"},
}


def _tickers_for(pair_label: str) -> tuple[str, str]:
    """ラジオ/セレクトで選ばれた主役ペア → 1D, 4H 各ティッカー"""
    m = PAIR_TO_TICKER.get(pair_label)
    if not m:
        return "USDJPY=X", "USDJPY=X"  # 未定義時の保険
    return m["1d"], m["4h"]



# ====== サイドバー（主役ペア / NFPカウントダウン） ======
PAIRS = [
    "ドル円", "ユーロ円", "ポンド円", "豪ドル円",
    "ユーロドル", "ポンドドル", "豪ドル米ドル", "米ドルフラン",
    "金/米ドル", "ビットコイン/米ドル",
    "NZドル円", "カナダドル円", "スイスフラン円", "メキシコペソ円", "南アフリカランド円",
    # 必要なら "米ドル指数" もここに追加可
]
_default_idx = PAIRS.index("ドル円") if "ドル円" in PAIRS else 0

with st.sidebar:
    from datetime import date  # ← このブロック内だけで使うので局所インポート
    st.subheader("主役ペア / タイトル語尾")

    # ★ ここがユーザー操作（ラベルは PAIRS の要素と一致）
    pair = st.selectbox("主役ペア", PAIRS, index=_default_idx)
    st.session_state["pair"] = pair  # 念のため保存

    # ★ ラジオ/セレクトの選択に基づいて、使うティッカーをセッションに保存（これが肝）
    _sel_1d, _sel_4h = _tickers_for(pair)
    st.session_state["pair_selected"] = pair
    st.session_state["ticker_1d"] = _sel_1d
    st.session_state["ticker_4h"] = _sel_4h

    st.markdown("---")

    # NFPカウントダウン（既存の関数をそのまま利用）
    st.subheader("NFPカウントダウン")
    _today = date.today()
    _nfp = next_nfp_official_or_rule(_today)   # 既存関数
    _days_left = (_nfp - _today).days

    _sched = _load_bls_empsit_schedule()       # 既存関数
    _is_official = _nfp in _sched
    _badge = "（公式）" if _is_official else "（目安）"

    st.write(f"次回 米雇用統計（NFP）：{_nfp:%Y-%m-%d}{_badge}（あと{_days_left}日）")
    st.caption("※ BLS公式スケジュールを優先。未公開月は目安（第1金曜）。JSTは夏/冬で21:30/22:30。")



# --- Secrets / 環境変数の取得を堅牢化（トップレベル / [general] / 環境変数の順） ---
def _get_secret_value(name: str) -> str | None:
    try:
        # ① トップレベル（TE_API_KEY など）
        if name in st.secrets:
            return st.secrets[name]
        # ② [general] セクション配下（general.TE_API_KEY など）
        if "general" in st.secrets and name in st.secrets["general"]:
            return st.secrets["general"][name]
    except Exception:
        pass
    # ③ 最後に環境変数
    import os
    return os.environ.get(name)


# ===== ユーティリティ：市場名・テンプレ =====
YEN_CROSSES = {"ドル円", "ユーロ円", "ポンド円"}
COMMODITY   = {"金/米ドル"}
CRYPTO      = {"ビットコイン/米ドル"}

def _market_word_for(pair: str) -> str:
    if pair in COMMODITY:
        return "貴金属市場"
    if pair in CRYPTO:
        return "暗号資産市場"
    return "為替市場"

PAIR_TEMPLATES_MOCK = {
    "ドル円": (
        "{market}は、ドル円は短期ではテクニカルの節目を意識した推移となった。"
        "1時間足では20SMAの向きに沿った値動きが中心となりやすく、"
        "4時間足ではボリンジャーバンド±2σと20SMAの位置関係が短期の手掛かり。"
        "日足では200SMA/EMAと20SMAの関係が方向感の目安となる。"
    ),
    "ユーロドル": (
        "{market}は、ユーロドルは1時間足で20SMA回りの攻防が続き、"
        "4時間足ではBB±2σと20SMAがレンジ境界となりやすい。"
        "日足は200SMA/EMA付近での反応を確認したい。"
    ),
    "ユーロ円": (
        "{market}は、ユーロ円は1時間足で20SMAの傾き、"
        "4時間足ではBB±2σと20SMAの重なる帯域、"
        "日足では200SMA/EMAの位置関係が焦点となりやすい。"
    ),
    "ポンドドル": (
        "{market}は、ポンドドルは1時間足で20SMAを挟んだ往来、"
        "4時間足はBB±2σと20SMAの組み合わせ、"
        "日足は200SMA/EMAと20SMAの並びで強弱が意識されやすい。"
    ),
    "ポンド円": (
        "{market}は、ポンド円は短期で戻り売りと押し目買いの綱引きになりやすく、"
        "1時間足は20SMA、4時間足はBB±2σと20SMA、"
        "日足は200SMA/EMAと20SMAの位置関係が手掛かり。"
    ),
    "豪ドル米ドル": (
        "{market}は、豪ドル米ドルはセンチメントの影響を受けやすく、"
        "1時間足は20SMA、4時間足はBB±2σ＋20SMA、"
        "日足は200SMA/EMA＋20SMAの並びを確認したい。"
    ),
    "NZドル米ドル": (
        "{market}は、NZドル米ドルはオセアニア指標や資源市況の影響が残り、"
        "1時間足20SMA、4時間足BB±2σ＋20SMA、日足200SMA/EMA＋20SMAが指標。"
    ),
    "金/米ドル": (
        "{market}は、金/米ドルは実質金利やリスク回避の動向を受けやすく、"
        "1時間足の20SMA、4時間足のBB±2σと20SMA、日足の200SMA/EMAと20SMAを意識。"
    ),
    "ビットコイン/米ドル": (
        "{market}は、ビットコイン/米ドルはヘッドラインの影響で短期の振れが生じやすく、"
        "1時間足20SMA、4時間足BB±2σ＋20SMA、日足200SMA/EMA＋20SMAが目安。"
    ),
}

def _default_para2_for(pair: str) -> str:
    market = _market_word_for(pair)
    tpl = PAIR_TEMPLATES_MOCK.get(pair)
    if tpl:
        return tpl.format(market=market)
    return (
        f"{market}は、{pair}は短期ではテクニカルの節目を意識した推移となった。"
        "時間足ではボリンジャーバンド±3σ間での往来が見られ、4時間足では20MA前後の攻防。"
        "日足では長期線（200SMA/EMA）近辺が上値・下値の目安となっている。"
    )

# ===== レジーム矛盾ガード（レンジ/トレンド表現の自動整合） =====
import re

def _regime_from_diag(diag: dict | None) -> str | None:
    """ライブ診断dictから 'range' / 'trend_up' / 'trend_down' を取り出す。無ければNone。"""
    if not isinstance(diag, dict):
        return None
    r = (diag.get("regime") or "").strip()
    return r if r in {"range", "trend_up", "trend_down"} else None

def _enforce_regime_language(para2_text: str, regime: str | None) -> tuple[str, list[str]]:
    """
    段落②のテキストを、判定レジームに合わせて“言い回しだけ”整える。
    事実を変えない／断定しすぎない置換だけを行う。flags に実施内容を返す。
    """
    if not isinstance(para2_text, str) or not para2_text:
        return para2_text, []

    s = para2_text
    flags: list[str] = []

    # 置換ユーティリティ（句読点や余計なスペースを崩さない）
    def rep(pattern, repl, note):
        nonlocal s
        new_s = re.sub(pattern, repl, s)
        if new_s != s:
            flags.append(note)
            s = new_s

    if regime == "range":
        # トレンド断定語を中立化
        rep(r"上昇トレンド(?:が(?:続き|意識され)やすい|入り|基調)?", "上方向への明確なトレンドは確認しづらい", "range: 上昇トレンド系→中立化")
        rep(r"下降トレンド(?:が(?:続き|意識され)やすい|入り|基調)?", "下方向への明確なトレンドは確認しづらい", "range: 下降トレンド系→中立化")
        rep(r"(?<!非)トレンドが出ている", "方向性は限定的", "range: トレンド一般→限定的")
        # “境界”はレンジで許容されるので残す。レンジ語が足りなければ少し補う
        if not re.search(r"(レンジ|持ち合い|もみ合い)", s):
            s = s.rstrip("。") + "。短期は持ち合い（レンジ）を前提とした値動きが意識されやすい。"
            flags.append("range: レンジ補足文を追加")

    elif regime == "trend_up":
        # “レンジ推移”を弱める（境界という語は残す）
        rep(r"レンジ推移", "方向性が出やすい地合い", "trend_up: レンジ推移→方向性が出やすい地合い")
        rep(r"(?<!境界)のレンジ(?!境界)", "の持ち合い帯域", "trend_up: レンジ単語の弱体化")
        # 上向きを示すが断定しない表現を少量補う
        if "上向き" not in s and "上昇" not in s:
            s = s.rstrip("。") + "。上向きバイアスが意識されやすい。"
            flags.append("trend_up: 上向きバイアス補足")

    elif regime == "trend_down":
        rep(r"レンジ推移", "方向性が出やすい地合い", "trend_down: レンジ推移→方向性が出やすい地合い")
        rep(r"(?<!境界)のレンジ(?!境界)", "の持ち合い帯域", "trend_down: レンジ単語の弱体化")
        if "下向き" not in s and "下落" not in s:
            s = s.rstrip("。") + "。下向きバイアスが意識されやすい。"
            flags.append("trend_down: 下向きバイアス補足")

    # 軽い整形（スペースのダブり等）
    s = re.sub(r"[ \t]+", " ", s).replace(" 。", "。").strip()
    return s, flags
# ===== ここまで =====


# 目的：タイトル語尾・段落②の締めをホワイトリストから安全選択（LLM利用時も“候補外禁止”）。rules_digestは文体/禁止語の守るべき要点を短く共有するために使用
# ===== タイトル・結び・ルールダイジェスト =====
ALLOWED_TITLE_TAILS = ["注視か", "警戒か", "静観か", "要注意か", "見極めたい"]

ALLOWED_PARA2_CLOSERS = [
    "行方を注視したい。", "値動きには警戒したい。", "当面は静観としたい。",
    "一段の変動に要注意としたい。", "方向感を見極めたい。"
]


def _llm_pick_from_list(system_msg: str, user_msg: str) -> str | None:
    try:
        from openai import OpenAI
        api_key = _get_api_key()
        if not api_key:
            return None
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-5-thinking",
            messages=[{"role": "system", "content": system_msg},
                      {"role": "user", "content": user_msg}],
            temperature=0.2,
            max_tokens=16,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text.replace("\n", "").strip()
    except Exception:
        return None

def _read_rules_digest(path: str | Path = "data/rules_digest.txt") -> str:
    p = Path(path)
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    return text[:2000]

RULES_DIGEST = _read_rules_digest()

def choose_title_tail(para1: str, para2: str) -> str:
    system = (
        "あなたは金融レポートの校正者です。断定を避けた語尾を選びます。"
        "必ず、与えられた候補の中から、レポート全体の文脈に最も自然なものを『語尾だけ』で返します。"
        "候補に無い語は出さないでください。句点は不要です。"
        + (f"\n【遵守ルール（抜粋）】\n{RULES_DIGEST}\n" if RULES_DIGEST else "")
    )
    user = (
        "候補: " + " / ".join(ALLOWED_TITLE_TAILS) + "\n\n"
        "文脈（段落①②の下書き）:\n" + para1 + "\n---\n" + para2 + "\n\n"
        "出力は語尾（例：警戒か）のみ。"
    )
    picked = _llm_pick_from_list(system, user)
    return picked if picked in ALLOWED_TITLE_TAILS else random.choice(ALLOWED_TITLE_TAILS)

def choose_para2_closer(para1: str, para2: str) -> str:
    system = (
        "あなたは金融レポートの校正者です。断定を避けた結びの一文を選びます。"
        "必ず、与えられた候補の中から、文脈に最も自然なものを『一文そのまま』で返します。"
        "候補に無い文は作らないでください。"
        + (f"\n【遵守ルール（抜粋）】\n{RULES_DIGEST}\n" if RULES_DIGEST else "")
    )
    user = (
        "候補: " + " / ".join(ALLOWED_PARA2_CLOSERS) + "\n\n"
        "文脈（段落①②の下書き）:\n" + para1 + "\n---\n" + para2 + "\n\n"
        "出力は候補の一文のみ。"
    )
    picked = _llm_pick_from_list(system, user)
    return picked if picked in ALLOWED_PARA2_CLOSERS else random.choice(ALLOWED_PARA2_CLOSERS)
# ===== 正典ルールJSON＋共通検証（Step1） =====
CANON_RULES_JSON = {
    "tone": {"avoid_assertive": True, "advice_ban": True},
    "min_chars": {"p1": 220, "p2": 180},
    "title_patterns": [
        r".+の方向感に(注視か|警戒か|静観か|要注意か)$",
        r".+の方向感を見極めたい$",
    ],
    "title_tails": ALLOWED_TITLE_TAILS,  # 既存ホワイトリストを再利用
    "forbidden_phrases": [
        # 売買助言・断定・煽り（必要に応じて拡充）
        "買い", "売り", "エントリー", "利確", "損切り", "両建て", "推奨",
        "必ず", "確実", "断言", "目標到達", "仕掛ける", "勝てる", "儲かる",
        "爆上げ", "暴落", "一択", "上昇確実", "下落確実",
    ],
    "normalize": {
        "fullwidth_parentheses": True,   # （）
        "halfwidth_alnum": True,         # 英数字は半角
        "time_hhmm_colon": True,         # 00:00 のコロン形式
        "sigma_minus_hyphen": True,      # -2σ の「-」統一
        "replacements": [
            ["ぶれ", "振れ"],
            ["とくに", "特に"],
            ["ふたたび", "再び"],
            ["ゆくえ", "行方"],
            ["ほど", "約"],
        ],
    },
}

def _canon_normalize(text: str) -> str:
    if not isinstance(text, str):
        return text
    s = text
    # 全角カッコ
    if CANON_RULES_JSON["normalize"].get("fullwidth_parentheses"):
        s = s.replace("(", "（").replace(")", "）")
    # 英数字は半角（ここでは全角英数の簡易正規化のみ）
    if CANON_RULES_JSON["normalize"].get("halfwidth_alnum"):
        import unicodedata
        s = unicodedata.normalize("NFKC", s)
    # 時刻コロン形式（ざっくり：全角コロン→半角）
    if CANON_RULES_JSON["normalize"].get("time_hhmm_colon"):
        s = s.replace("：", ":")
    # -2σ のハイフン統一（全角/長音をASCIIハイフンに）
    if CANON_RULES_JSON["normalize"].get("sigma_minus_hyphen"):
        s = s.replace("−", "-").replace("ー2σ", "-2σ")
    # 語彙ゆれ
    for a, b in CANON_RULES_JSON["normalize"].get("replacements", []):
        s = s.replace(a, b)
    return s

def _canon_find_forbidden(text: str) -> list[str]:
    hits = []
    if not isinstance(text, str):
        return hits
    t = text
    for ng in CANON_RULES_JSON["forbidden_phrases"]:
        if ng and ng in t:
            hits.append(ng)
    return sorted(set(hits))

def _canon_title_ok(title: str) -> bool:
    import re
    t = (title or "").strip()
    for pat in CANON_RULES_JSON["title_patterns"]:
        if re.fullmatch(pat, t):
            return True
    return False

def _canon_title_recall_ok(title: str, last_line: str) -> bool:
    import re
    try:
        expected = build_title_recall(title)
    except Exception:
        expected = (title or "").strip()
        if not expected.endswith("。"):
            expected += "。"

    def _norm(s: str) -> str:
        # 全角・半角や空白、連続句点などの揺れを吸収
        s = (s or "").strip()
        s = s.replace(" ", "").replace("\u3000", "")
        s = s.replace("｡", "。")
        s = re.sub(r"。+$", "。", s)  # 句点は1つに正規化
        return s

    return _norm(last_line) == _norm(expected)


def _canon_guess_blocks_from_text(report_text: str) -> dict:
    """
    report_text から タイトル/ポイント2件/①/②/③（最後の行）を素直に推定。
    既存構成（タイトル→空行→本日のポイント→…→③一行）の想定に合わせた耐性ロジック。
    """
    lines = [l.rstrip() for l in (report_text or "").splitlines()]
    lines = [l for l in lines if l is not None]
    title = (lines[0] if lines else "").strip()

    # 「本日のポイント」節を探す
    idx_points = None
    for i, l in enumerate(lines[:50]):
        if "本日のポイント" in l:
            idx_points = i
            break
    points = []
    if idx_points is not None:
        # 次の非空2行をポイントとして収集
        j = idx_points + 1
        while j < len(lines) and len(points) < 2:
            if lines[j].strip():
                points.append(lines[j].strip("・：: "))
            j += 1

    # ③：最後の非空行をタイトル回収行とみなす
    last_line = ""
    for l in reversed(lines):
        if l.strip():
            last_line = l.strip()
            break

    # ①②はテキスト全体から「本日の指標は、」以降とポイント節を除いた残差で概算。
    text_wo_title = "\n".join(lines[1:])
    p3_head = "本日の指標は"
    p3_pos = text_wo_title.find(p3_head)
    head_to_p3 = text_wo_title if p3_pos < 0 else text_wo_title[:p3_pos]
    # 「本日のポイント」見出し行以降、最初の2行（ポイント）を除去
    if idx_points is not None:
        head_to_p3 = "\n".join(head_to_p3.splitlines()[:idx_points-1] + head_to_p3.splitlines()[idx_points+3:])
    # 残差を空行分割して①②候補に
    chunks = [c.strip() for c in head_to_p3.split("\n\n") if c.strip()]
    p1 = chunks[0] if len(chunks) >= 1 else ""
    p2 = chunks[1] if len(chunks) >= 2 else ""
    return {"title": title, "points": points, "p1": p1, "p2": p2, "p3_last_line": last_line}

def canon_validate_current_report(report_text: str) -> tuple[list[str], dict]:
    """
    report_text だけを入力に、正典ベースの検証を実施。
    戻り値: (errors[], checks_dict)
    """
    errs: list[str] = []
    blk = _canon_guess_blocks_from_text(report_text)
    title = _canon_normalize(blk.get("title", ""))
    p1 = _canon_normalize(blk.get("p1", ""))
    p2 = _canon_normalize(blk.get("p2", ""))
    last_line = _canon_normalize(blk.get("p3_last_line", ""))

    # タイトルパターン
    if not _canon_title_ok(title):
        tails = " / ".join(CANON_RULES_JSON["title_tails"])
        errs.append(f"タイトルが規定外です（許容語尾: {tails} / 『…の方向感を見極めたい』）。")

    # 長さ（①②）
    if len(p1) < CANON_RULES_JSON["min_chars"]["p1"]:
        errs.append(f"段落①の文字数不足（{len(p1)}字 < {CANON_RULES_JSON['min_chars']['p1']}字）。")
    if len(p2) < CANON_RULES_JSON["min_chars"]["p2"]:
        errs.append(f"段落②の文字数不足（{len(p2)}字 < {CANON_RULES_JSON['min_chars']['p2']}字）。")

    # 禁止語（全文で判定）
    ng_hits = _canon_find_forbidden(report_text)
    if ng_hits:
        errs.append("禁止語検知: " + " / ".join(ng_hits))

    # ③は一行相当（実態はUI改行でも、論理的には1行の想定）
    if "\n" in last_line:
        errs.append("段落③は1行の想定です（イベント列挙＋タイトル回収を一行で）。")

    # タイトル回収
    if not _canon_title_recall_ok(title, last_line):
        errs.append("段落③の末尾が『タイトル回収』になっていません。")

    checks = {
        "title": title,
        "p1_len": len(p1),
        "p2_len": len(p2),
        "forbidden_hits": ng_hits,
        "title_recalled": _canon_title_recall_ok(title, last_line),
    }
    return errs, checks
# ===== 正典ルール検証 ここまで =====

# ---- タイトル初期値（助詞の自動補正を含む“正”の版だけを残す） ----
def _default_title_for(pair: str, tail: str) -> str:
    tail = (tail or "").strip()
    if tail == "見極めたい":  # 「の方向感“を”見極めたい」に統一
        return f"{pair}の方向感を見極めたい"
    return f"{pair}の方向感に{tail}"

# ---- タイトル回収（一文） ----
def build_title_recall(title: str) -> str:
    if _build_title_recall_from_mod:
        try:
            return _build_title_recall_from_mod(title)
        except Exception:
            pass
    t = (title or "").strip()
    tail_map = {
        "注視か": "注視したい。",
        "警戒か": "警戒したい。",
        "静観か": "静観したい。",
        "要注意か": "要注意としたい。",
    }
    for q, fin in tail_map.items():
        if t.endswith(q):
            stem = t[: -len(q)].rstrip()
            if fin.endswith("したい。") and not stem.endswith("に"):
                stem += "に"
            if not stem.endswith("。") and fin.endswith("。"):
                return stem + fin
            return stem + fin
    if t.endswith("見極めたい"):
        t = t.replace("の方向感に見極めたい", "の方向感を見極めたい")
        if not t.endswith("。"):
            t += "。"
        return t
    if not t.endswith("。"):
        t += "。"
    return t




# ===== 指標名の日本語整形（任意辞書 + 地域接頭語）========================

import yaml


# =======================================================================
# ===== 日本語エイリアス（重複接頭ガード＋カテゴリのフォールバック付き） =====

import yaml, re

_IND_ALIASES = None
_CAT_ALIASES = None

def _load_alias_yaml(path: str) -> dict:
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}

# 指標名 → 日本語（「英・」「米・」などの接頭は重複しないように一度剥がして付け直す）
# [統一済み] _ja_indicator_name はこの定義のみを使用（旧定義は削除済み）

def _ja_indicator_name(text: str, region: str) -> str:
    global _IND_ALIASES
    if _IND_ALIASES is None:
        _IND_ALIASES = _load_alias_yaml("data/indicator_alias_ja.yaml")

    t = str(text or "").strip()

    # ① 先頭の接頭（英・米・豪・NZ・欧・中国・南ア）を全部はがす
    prefix_pat = r'^(?:米|日|欧|英|豪|NZ|中国|南ア)・\s*'
    while re.match(prefix_pat, t):
        t = re.sub(prefix_pat, '', t)

    # ② 和名辞書（あれば使う）
    alias = _IND_ALIASES.get(t) or _IND_ALIASES.get(t.lower())
    name = alias if alias else t

    # ③ 地域コード → 日本語接頭を1回だけ付与
    reg_map = {"US":"米","JP":"日","EU":"欧","UK":"英","AU":"豪","NZ":"NZ","CN":"中国","ZA":"南ア"}
    jp_reg = reg_map.get((region or "").upper(), "")
    return f"{jp_reg}・{name}" if jp_reg else name

# カテゴリ → 日本語（辞書優先。無ければキーワードで大まかに丸める）
def _ja_category_name(cat: str, indicator: str = "") -> str:
    global _CAT_ALIASES
    if _CAT_ALIASES is None:
        _CAT_ALIASES = _load_alias_yaml("data/category_alias_ja.yaml")

    c = (cat or "").strip()

    # 1) YAML辞書を最優先
    ja = _CAT_ALIASES.get(c) or _CAT_ALIASES.get(c.lower())
    if ja:
        return ja

    # 2) フォールバック：指標名/カテゴリ名の英語に含まれるキーワードでざっくり分類
    t = (indicator or c).lower()
    if any(k in t for k in ["house", "housing", "mortgage", "home"]):
        return "住宅"
    if any(k in t for k in ["inflation", "cpi", "ppi"]):
        return "インフレ"
    if any(k in t for k in ["employment", "jobs", "payroll", "unemployment", "nfp"]):
        return "雇用"
    if any(k in t for k in ["confidence", "sentiment"]):
        return "信頼感"
    if any(k in t for k in ["gdp", "growth"]):
        return "成長"
    if any(k in t for k in ["current account"]):
        return "経常収支"
    if any(k in t for k in ["trade balance"]):
        return "貿易収支"
    if any(k in t for k in ["retail sales"]):
        return "小売売上高"

    
    return c  # どうしても判定できなければ原文のまま

# ===============================================


# =====（この下から Step1 セクション）=====

# ====== 前日比ランキング（終値ベース / メインに1ブロック） ======
import pandas as pd
import yfinance as yf

# 表示名→Yahoo!Finance ティッカー（未定義なら定義）
if "_pair_to_symbol" not in globals():
    _PAIR_MAP = {
        "ドル円": "USDJPY=X",
        "ユーロ円": "EURJPY=X",
        "ポンド円": "GBPJPY=X",
        "豪ドル円": "AUDJPY=X",
        "ユーロドル": "EURUSD=X",
        "ポンドドル": "GBPUSD=X",
        "豪ドル米ドル": "AUDUSD=X",
        "米ドルフラン": "USDCHF=X",
        "金/米ドル": "XAUUSD=X",
        "ビットコイン/米ドル": "BTC-USD",
        "NZドル円": "NZDJPY=X",
        "カナダドル円": "CADJPY=X",
        "スイスフラン円": "CHFJPY=X",
        "メキシコペソ円": "MXNJPY=X",
        "南アフリカランド円": "ZARJPY=X",
    }
    def _pair_to_symbol(name: str) -> str:
        return _PAIR_MAP.get((name or "").strip(), "USDJPY=X")

# 直近終値2本を取得（t-1, t0）
@st.cache_data(ttl=900)
def _rank_last_two_closes(symbol: str):
    try:
        df = yf.download(symbol, period="10d", interval="1d",
                         auto_adjust=False, progress=False)
        if df is None or df.empty or "Close" not in df:
            return None
        close = df["Close"].dropna()
        if len(close) < 2:
            return None
        d_prev, d_last = close.index[-2], close.index[-1]
        return (d_prev, float(close.iloc[-2]), d_last, float(close.iloc[-1]))
    except Exception:
        return None

def _pct_change(prev: float, last: float) -> float:
    return (last / max(prev, 1e-12) - 1.0) * 100.0

# 集計 → 表示
_rows = []
for _name in PAIRS:
    _sym = _pair_to_symbol(_name)
    _res = _rank_last_two_closes(_sym)
    if not _res:
        continue
    _d_prev, _c_prev, _d_last, _c_last = _res
    _pct = _pct_change(_c_prev, _c_last)
    _rows.append({
        "ペア": _name,
        "変動率(%)": _pct,
        "表示": f"{_pct:+.2f}%",
        "t-1": _d_prev.strftime("%Y-%m-%d"),
        "t0":  _d_last.strftime("%Y-%m-%d"),
    })
# === 祝日・市場カレンダー（日本/米）フラグの表示 ===
from datetime import datetime, timezone, timedelta
import pandas as pd
try:
    from zoneinfo import ZoneInfo  # Python 3.9+ に標準添付
except Exception:
    ZoneInfo = None  # 無くても動く（未判定表示に切替）

# JST の「きょう／きのう」
_now_jst = datetime.now(timezone(timedelta(hours=9)))
_today_jst = _now_jst.date()
_yday_jst = (_now_jst - timedelta(days=1)).date()

# A. 日本の祝日（未導入でもエラーにしない）
_jp_today = _jp_yday = None
try:
    import jpholiday  # 未導入なら except に落ちる
    _jp_today = bool(jpholiday.is_holiday(_today_jst))
    _jp_yday  = bool(jpholiday.is_holiday(_yday_jst))
except Exception:
    _jp_today = _jp_yday = None  # 未判定

# B. 米国（NYSE）の営業／休場（未導入でもエラーにしない）
_us_open_today = _us_open_yday = None
_last_us_session = None
try:
    import exchange_calendars as xc
    _cal = xc.get_calendar("XNYS")  # NYSE
    if ZoneInfo:
        _now_ny = datetime.now(ZoneInfo("America/New_York"))
    else:
        # 簡易フォールバック（厳密でなくてOK：未判定回避用）
        _now_ny = datetime.utcnow()
    _today_us = _now_ny.date()
    _yday_us  = (_now_ny - timedelta(days=1)).date()

    _ts_today = pd.Timestamp(_today_us)
    _ts_yday  = pd.Timestamp(_yday_us)
    _us_open_today = bool(_cal.is_session(_ts_today))
    _us_open_yday  = bool(_cal.is_session(_ts_yday))

    # 直近の米セッション日（カレンダー上）
    if _us_open_today:
        _last_us_session = _today_us
    else:
        _last_us_session = _cal.previous_session(_ts_today).date()
except Exception:
    _us_open_today = _us_open_yday = None
    _last_us_session = None  # 未判定

# 表示ユーティリティ
def _yn(v, yes="はい", no="いいえ", na="（未判定）"):
    return yes if v is True else (no if v is False else na)
def _open_close(v):
    return "営業" if v is True else ("休場" if v is False else "（未判定）")

st.write("**祝日 / 市場カレンダー**")
st.caption(
    "本日 日本の祝日：" + _yn(_jp_today)
    + "　/　本日 米国NYSE：" + _open_close(_us_open_today)
    + "　/　昨日 日本の祝日：" + _yn(_jp_yday)
    + "　/　昨日 米国NYSE：" + _open_close(_us_open_yday)
)
if _last_us_session:
    st.caption(f"直近米セッション日（US/Eastern基準）：{_last_us_session}")

# メタへ保存（後でLLMやログに渡せます）
_meta = st.session_state.get("data_snapshot", {})
_meta.update({
    "jp_holiday_today": _jp_today,
    "jp_holiday_yesterday": _jp_yday,
    "us_nyse_open_today": _us_open_today,
    "us_nyse_open_yesterday": _us_open_yday,
    "us_last_session_date": str(_last_us_session) if _last_us_session else None,
})
st.session_state["data_snapshot"] = _meta
st.markdown("---")
# === 祝日・市場カレンダー 表示ここまで ===
# === 冒頭の自動注記（祝日/休場）: 表示のみ、本文にはまだ合成しない ===
_flags = st.session_state.get("data_snapshot", {})
jp_today = _flags.get("jp_holiday_today")          # True/False/None
us_open_yday = _flags.get("us_nyse_open_yesterday")# True=営業, False=休場, None
us_open_today = _flags.get("us_nyse_open_today")    # True=営業, False=休場, None

_intro_lines = []
# 本日が日本の祝日 → 先頭に一言
if jp_today is True:
    _intro_lines.append("本日は日本が祝日で、東京時間は流動性がやや薄くなりやすい。")

# 昨日が米祝日（休場） → 次に一言
if us_open_yday is False:
    _intro_lines.append("昨日は米国は祝日で株式は休場。直近の取引は前営業日。")

# 本日が米祝日（休場） → 末尾に一言（任意）
_tail_line = "なお、米国は本日祝日で株式は休場見込み。" if us_open_today is False else ""

# 画面に表示（あれば）
st.write("**冒頭の自動注記（祝日/休場）**")
if _intro_lines or _tail_line:
    st.caption("この下の本文に、のちほど自動合成します。今は確認用の表示だけです。")
    if _intro_lines:
        st.write("".join(_intro_lines))
    if _tail_line:
        st.write(_tail_line)
else:
    st.caption("差し込み要素なし（祝日/休場の注記は該当なし）")

# 後工程用にメモしておく（次の手で本文に合成）
st.session_state["intro_overlay_text"] = "".join(_intro_lines) + (_tail_line or "")
st.markdown("---")
# === 冒頭の自動注記（祝日/休場）ここまで ===

# === データスナップショット（今回使用したデータを最上部に明示） ===
from datetime import datetime, timezone, timedelta

_asof_jst = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M JST")
_ok_pairs = [r.get("ペア") for r in _rows] if _rows else []
_ok_syms  = [_pair_to_symbol(p) for p in _ok_pairs] if _ok_pairs else []

st.subheader("本稿で使用したデータ（スナップショット）")
st.caption(f"取得：{_asof_jst}")
if _ok_pairs:
    st.write(f"取得済み：前日比ランキング（終値ベース） {len(_ok_pairs)}ペア")
    st.caption("取得ティッカー（今回）：" + ", ".join(_ok_syms))
else:
    st.write("取得済み：前日比ランキング（終値ベース） 0ペア")

# 後工程（LLM再整形やログ保存）で参照するために保持
st.session_state["data_snapshot"] = {
    "asof_jst": _asof_jst,
    "pairs_fetched": _ok_pairs,
    "tickers_fetched": _ok_syms,
    "source_hint": "Yahoo Finance（日次終値 t-1 / t0）"
}
st.markdown("---")
# === データスナップショット ここまで ===


if _rows:
    _df = pd.DataFrame(_rows)
    _df = _df.sort_values(by="変動率(%)", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)
    _show = _df[["ペア", "表示", "t-1", "t0"]].rename(
        columns={"表示": "前日比", "t-1": "比較日(t-1)", "t0": "直近日(t0)"}
    )
    _show.index = range(1, len(_show) + 1)  # 1始まり
    _show.index.name = "順位"

    st.subheader("前日比ランキング（終値ベース）")
    st.caption("各ペアの直近の有効終値(t0) と その一つ前(t-1) の比較。％は +/− 表示。順位は1始まり。")
    _show.insert(0, "主役", np.where(_show["ペア"] == pair, "⭐", ""))
    st.dataframe(_show, use_container_width=True)
else:
    st.info("有効な終値が取得できませんでした。少し時間を置いて再試行してください。")
# ====== 前日比ランキング ここまで ======

# ====== ステップ1：参照PDFの確認 ======

# === Tech Charts (D1/H4) v2.1 — self-contained block START ===
import importlib.util as _iul

# Plotly 可否（未導入でもアプリは落とさない）
_PLOTLY_OK = _iul.find_spec("plotly") is not None
if _PLOTLY_OK:
    import plotly.graph_objects as _go
    from plotly.subplots import make_subplots as _mk
else:
    _go = None
    _mk = None

# この節だけで完結させるための既定値
_candles = 120             # 表示本数（常に120本）
_h_dpi   = 560             # 図の高さ
_cfg     = {"displayModeBar": True, "scrollZoom": False, "displaylogo": False}

# --- データ取得（1d は素直に、4h は 60m→4H リサンプル） ---
def _dl_ohlc_v21(ticker: str, interval: str, need: int) -> pd.DataFrame | None:
    """
    すべて60分足から作る:
      - interval == "4h": 60m → 4H にリサンプリング
      - interval == "1d": 60m → 1D にリサンプリング（NY 17:00 区切りの“為替日足”）
    """
    try:
        import numpy as np
        import pandas as pd
        import yfinance as yf

        # 60分足を長めに取得（上限は約730日）
        period_for_60m = "730d" if interval in ("1d", "4h") else "120d"
        raw = yf.download(
            ticker, period=period_for_60m, interval="60m",
            auto_adjust=False, progress=False
        )
        if raw is None or raw.empty:
            return None

        # 列名と必要列を整理
        raw = raw.rename(columns=str.title)[["Open", "High", "Low", "Close"]].copy()

        # ==== 1) タイムゾーンの扱い ====
        # yfinance の 60m は通常 tz-aware。なければ UTC を付与
        if getattr(raw.index, "tz", None) is None:
            raw = raw.tz_localize("UTC")

        # ==== 2) 集計 ====
        if interval == "4h":
            base = raw.tz_convert("UTC")  # 4H は UTC 境界でOK
            o = base["Open"].resample("4H").first()
            h = base["High"].resample("4H").max()
            l = base["Low"].resample("4H").min()
            c = base["Close"].resample("4H").last()
            df = pd.concat([o, h, l, c], axis=1).dropna(how="any")
            df.columns = ["Open", "High", "Low", "Close"]
            # ラベルをtz-naiveに（以降の処理が楽）
            df.index = df.index.tz_localize(None)

        elif interval == "1d":
            # ★ 為替日足：NY 17:00（5pm ET）で1日を区切るのが一般的
            ny = raw.tz_convert("America/New_York")

            # インデックスを 17 時間「戻して」日境界を NY17:00 に合わせる
            ny_shift = ny.copy()
            ny_shift.index = ny_shift.index - pd.Timedelta(hours=17)

            o = ny_shift["Open"].resample("1D").first()
            h = ny_shift["High"].resample("1D").max()
            l = ny_shift["Low"].resample("1D").min()
            c = ny_shift["Close"].resample("1D").last()
            df = pd.concat([o, h, l, c], axis=1).dropna(how="any")
            df.columns = ["Open", "High", "Low", "Close"]

            # ラベルを 17 時間「戻す」（元の時刻側へ）
            df.index = (df.index + pd.Timedelta(hours=17)).tz_localize(None)

        else:
            return None

        # ==== 3) 数値化と最終整形 ====
        df = df.apply(pd.to_numeric, errors="coerce").dropna(how="any")

        # High/Low が必ず胴体(Open/Close)を内包するように強制補正（可視化の安全策）
        body_hi = df[["Open", "Close"]].max(axis=1).to_numpy()
        body_lo = df[["Open", "Close"]].min(axis=1).to_numpy()
        df["High"] = np.maximum(df["High"].to_numpy(), body_hi)
        df["Low"]  = np.minimum(df["Low"].to_numpy(),  body_lo)

        # 必要本数を返す
        return df.tail(max(need, _candles))

    except Exception:
        return None


# --- 指標計算（SMA/BB/RSI） ---
def _sma_v21(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=1).mean()

def _bbands_v21(s: pd.Series, n: int = 20, k: float = 2.0):
    ma = s.rolling(n, min_periods=1).mean()
    sd = s.rolling(n, min_periods=1).std(ddof=0)
    return ma + k*sd, ma - k*sd

def _rsi_v21(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    ema_up = up.ewm(alpha=1/n, adjust=False).mean()
    ema_dn = dn.ewm(alpha=1/n, adjust=False).mean()
    rs = ema_up / ema_dn.replace(0, np.nan)
    return (100 - (100/(1+rs))).clip(0, 100)

def _dl_ohlc_v21(ticker: str, interval: str, need: int) -> pd.DataFrame | None:
    """
    すべて 60 分足から作成:
      - interval == "4h": 60m → 4H にリサンプリング（UTC 境界）
      - interval == "1d": 60m → 1D にリサンプリング（NY 17:00 区切りの“為替日足”）
    """
    try:
        import numpy as np
        import pandas as pd
        import yfinance as yf

        # 60分足を長めに取得（最大 ~730 日）
        period_for_60m = "730d" if interval in ("1d", "4h") else "120d"
        raw = yf.download(
            ticker, period=period_for_60m, interval="60m",
            auto_adjust=False, progress=False
        )
        if raw is None or raw.empty:
            return None

        # 列名と必要列を整理
        raw = raw.rename(columns=str.title)[["Open", "High", "Low", "Close"]].copy()

        # yfinance の 60m は通常 tz-aware。なければ UTC を付与
        if getattr(raw.index, "tz", None) is None:
            raw = raw.tz_localize("UTC")

        # === 集計 ===
        if interval == "4h":
            base = raw.tz_convert("UTC")
            o = base["Open"].resample("4H").first()
            h = base["High"].resample("4H").max()
            l = base["Low"].resample("4H").min()
            c = base["Close"].resample("4H").last()
            df = pd.concat([o, h, l, c], axis=1).dropna(how="any")
            df.columns = ["Open", "High", "Low", "Close"]
            df.index = df.index.tz_localize(None)  # tz-naive に

        elif interval == "1d":
            # NY 17:00（5pm ET）を 1 日の区切りにする
            ny = raw.tz_convert("America/New_York")
            ny_shift = ny.copy()
            ny_shift.index = ny_shift.index - pd.Timedelta(hours=17)

            o = ny_shift["Open"].resample("1D").first()
            h = ny_shift["High"].resample("1D").max()
            l = ny_shift["Low"].resample("1D").min()
            c = ny_shift["Close"].resample("1D").last()
            df = pd.concat([o, h, l, c], axis=1).dropna(how="any")
            df.columns = ["Open", "High", "Low", "Close"]

            # インデックスを 17 時間戻す（元の側へ）＆ tz-naive
            df.index = (df.index + pd.Timedelta(hours=17)).tz_localize(None)
        else:
            return None

        # 数値化と安全補正（高値/安値が胴体を必ず包含）
        df = df.apply(pd.to_numeric, errors="coerce").dropna(how="any")
        body_hi = df[["Open", "Close"]].max(axis=1).to_numpy()
        body_lo = df[["Open", "Close"]].min(axis=1).to_numpy()
        df["High"] = np.maximum(df["High"].to_numpy(), body_hi)
        df["Low"]  = np.minimum(df["Low"].to_numpy(),  body_lo)

        # 必要本数を返す（_candles は既存のグローバル）
        return df.tail(max(need, _candles))

    except Exception:
        return None


# --- 指標計算（SMA/BB/RSI） ---
def _sma_v21(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=1).mean()

def _bbands_v21(s: pd.Series, n: int = 20, k: float = 2.0):
    ma = s.rolling(n, min_periods=1).mean()
    sd = s.rolling(n, min_periods=1).std(ddof=0)
    return ma + k*sd, ma - k*sd

def _rsi_v21(close: pd.Series, n: int = 14) -> pd.Series:
    d  = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    ema_up = up.ewm(alpha=1/n, adjust=False).mean()
    ema_dn = dn.ewm(alpha=1/n, adjust=False).mean()
    rs = ema_up / ema_dn.replace(0, np.nan)
    return (100 - (100/(1+rs))).clip(0, 100)

def _build_one_v21(title, v):
    import plotly.graph_objects as go

    # --- 安全ガード：データが空ならプレースホルダ図を返す ---
    if v is None or getattr(v, "empty", True) or len(v.index) == 0:
        fig = go.Figure()
        fig.update_layout(
            title=f"{title}：データなし",
            template="plotly_white",
            height=320,
            margin=dict(l=8, r=8, t=40, b=8),
        )
        return fig
    # --- ここから下は既存の処理（そのまま） ---


def _build_one_v21(fig_title: str, ohlc: pd.DataFrame):
    import numpy as np
    import pandas as pd

    # === 1) OHLC を 1D Series に正規化（MultiIndex/大小文字差も吸収） ===
    v0 = ohlc.copy()

    def _get_1d(df: pd.DataFrame, name: str) -> pd.Series:
        s = None
        if name in df.columns and isinstance(df[name], pd.Series):
            s = df[name]
        if s is None:
            for col in df.columns:
                base = col[0] if isinstance(col, tuple) else col
                if str(base).lower() == name.lower():
                    s = df[col]
                    break
        if s is None and isinstance(df.columns, pd.MultiIndex):
            try:
                tmp = df.xs(name, axis=1, level=0, drop_level=False)
                s = tmp.iloc[:, 0]
            except Exception:
                pass
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        if s is None:
            s = pd.Series(index=df.index, dtype="float64")
        return pd.to_numeric(s, errors="coerce")

    v = pd.DataFrame(index=v0.index)
    for col in ["Open", "High", "Low", "Close"]:
        v[col] = _get_1d(v0, col)

    # NaN 行は除外 & 表示本数を揃える
    v = v.dropna(subset=["Open", "High", "Low", "Close"], how="any")
    # 表示本数を 2/3 に間引き（最低30本は確保）
    _show = max(30, int(round(_candles * 1 / 2))) if not v.empty else 0   #★★★　ここでロウソク足の本数を調整する　★★★★★★★★★★★★★★★★★★　　　　　　
    v = v.tail(_show) if not v.empty else v


    # （安全）高値/安値が胴体を必ず包含
    v["High"] = pd.concat([v["High"], v["Open"], v["Close"]], axis=1).max(axis=1)
    v["Low"]  = pd.concat([v["Low"],  v["Open"], v["Close"]], axis=1).min(axis=1)

    # === 2) 指標 ===
    v["SMA20"]  = v["Close"].rolling(20,  min_periods=1).mean()
    v["SMA200"] = v["Close"].rolling(200, min_periods=1).mean()
    bb_mid = v["Close"].rolling(20, min_periods=1).mean()
    bb_sd  = v["Close"].rolling(20, min_periods=1).std(ddof=0)
    v["BBU"] = bb_mid + 2.0 * bb_sd
    v["BBL"] = bb_mid - 2.0 * bb_sd
    v["RSI14"] = _rsi_v21(v["Close"], 14)

    # === 3) 上段 Y レンジ（ローソク + SMA） ===
    def _smin(s: pd.Series) -> float:
        s = pd.to_numeric(s, errors="coerce")
        mn = s.min(skipna=True)
        return float(mn) if pd.notna(mn) else np.nan

    def _smax(s: pd.Series) -> float:
        s = pd.to_numeric(s, errors="coerce")
        mx = s.max(skipna=True)
        return float(mx) if pd.notna(mx) else np.nan

    lows  = [_smin(v["Low"]),  _smin(v["SMA20"]),  _smin(v["SMA200"])]
    highs = [_smax(v["High"]), _smax(v["SMA20"]), _smax(v["SMA200"])]
    try:
        y_min = np.nanmin(lows)
        y_max = np.nanmax(highs)
    except Exception:
        y_min, y_max = float(v["Close"].min()), float(v["Close"].max())

    if (not np.isfinite(y_min)) or (not np.isfinite(y_max)) or y_max <= y_min:
        y_min, y_max = float(v["Close"].min()), float(v["Close"].max())
    if not np.isfinite(y_min) or not np.isfinite(y_max) or y_max <= y_min:
        y_min, y_max = 0.0, 1.0

    pad   = (y_max - y_min) * 0.01
    y_rng = [y_min - pad, y_max + pad]

    # X 右側の余白
    xpad = (v.index[-1] - v.index[-3]) if len(v) >= 3 else pd.Timedelta(days=2)

    # === 4) 図（上：ローソク＋SMA/BB、下：RSI） ===
    fig = _mk(
        rows=2, cols=1, shared_xaxes=True,
        vertical_spacing=0.06, row_heights=[0.76, 0.24],
        specs=[[{"type": "xy"}], [{"type": "xy"}]],
    )

    # 背景レイヤ（SMA/BB は先に描画）※ connectgaps で途切れ対策
    fig.add_trace(_go.Scatter(x=v.index, y=v["SMA20"],  mode="lines",
                              line=dict(width=2, color="red"),
                              name="SMA20", connectgaps=True),
                  row=1, col=1)
    fig.add_trace(_go.Scatter(x=v.index, y=v["SMA200"], mode="lines",
                              line=dict(width=2, color="blue"),
                              name="SMA200", connectgaps=True),
                  row=1, col=1)
    fig.add_trace(_go.Scatter(x=v.index, y=v["BBU"], mode="lines",
                              line=dict(width=1, color="rgba(0,0,0,0.25)"),
                              name="BB+2σ", connectgaps=True),
                  row=1, col=1)
    fig.add_trace(_go.Scatter(x=v.index, y=v["BBL"], mode="lines",
                              line=dict(width=1, color="rgba(0,0,0,0.25)"),
                              name="BB-2σ", connectgaps=True),
                  row=1, col=1)

    # ローソク（最前面）。hovertemplate は使わず text + hoverinfo を使用
    _ht = (
        "Open: " + v["Open"].round(3).astype(str) + "<br>"
        "High: " + v["High"].round(3).astype(str) + "<br>"
        "Low : " + v["Low"].round(3).astype(str) + "<br>"
        "Close: " + v["Close"].round(3).astype(str)
    )
    fig.add_trace(_go.Candlestick(
        x=v.index, open=v["Open"], high=v["High"], low=v["Low"], close=v["Close"],
        increasing_line_color="green", increasing_fillcolor="green",
        decreasing_line_color="red",   decreasing_fillcolor="red",
        whiskerwidth=0.9, opacity=1.0,
        text=_ht, hoverinfo="x+text",
        name="Candle", showlegend=False
    ), row=1, col=1)

    # RSI（下段）
    fig.add_trace(_go.Scatter(x=v.index, y=v["RSI14"], mode="lines",
                              line=dict(width=1.6),
                              name="RSI(14)", connectgaps=True),
                  row=2, col=1)
    fig.add_hline(y=30, line_width=1, line_dash="dot", line_color="gray", row=2, col=1)
    fig.add_hline(y=70, line_width=1, line_dash="dot", line_color="gray", row=2, col=1)

    # === 週末・祝日などの“空白”を圧縮 ===
    # 週末（土→月）は固定でスキップ + 祝日等の丸一日欠損もスキップ
    _rb = [dict(bounds=["sat", "mon"])]
    try:
        _all_days = pd.date_range(v.index.min().normalize(),
                                  v.index.max().normalize(),
                                  freq="D")
        _have_days = pd.to_datetime(v.index).normalize().unique()
        _missing_days = _all_days.difference(_have_days)
        if len(_missing_days) > 0:
            _rb.append(dict(values=_missing_days))
    except Exception:
        pass

    # X 軸（上下共通設定）
    fig.update_xaxes(range=[v.index[0], v.index[-1] + xpad],
                     rangebreaks=_rb,
                     showgrid=True, gridcolor="rgba(0,0,0,0.08)",
                     row=1, col=1)
    fig.update_xaxes(range=[v.index[0], v.index[-1] + xpad],
                     rangebreaks=_rb,
                     showgrid=True, gridcolor="rgba(0,0,0,0.08)",
                     row=2, col=1)

    # Y 軸
    fig.update_yaxes(range=y_rng, fixedrange=True,
                     showgrid=True, gridcolor="rgba(0,0,0,0.08)",
                     row=1, col=1)
    fig.update_yaxes(range=[0, 100], fixedrange=True,
                     showgrid=True, gridcolor="rgba(0,0,0,0.08)",
                     row=2, col=1)

    # レンジスライダー抑止
    fig.update_layout(
        title=dict(text=fig_title, y=0.98, x=0.01, xanchor="left", yanchor="top"),
        margin=dict(l=8, r=24, t=30, b=8),
        height=_h_dpi, showlegend=False,
        xaxis_rangeslider_visible=False,
        xaxis2_rangeslider_visible=False
    )
    return fig





# ---- 表示本体（常時 D1/H4 の2枚を縦並びで表示）----
st.subheader("テクニカル・チャート（D1/H4）")

# フル/標準の切り替え（キー名は他と被らないように）
_view = st.selectbox("表示", ["標準", "フルスクリーン"], index=0, key="tech_view_mode_v21")
_is_full = (_view == "フルスクリーン")
_h = int(_h_dpi * (1.25 if _is_full else 1.0))

# 表示本数（見やすさ優先で少なめに）
_candles = int(st.session_state.get("candles", 180))

# サイドバーで選ばれた主役ペア → ティッカーを決定
_pair_label = st.session_state.get("pair", "ドル円")
_t1d, _t4h = _tickers_for(_pair_label)
_ticker_1d = st.session_state.get("ticker_1d", _t1d)
_ticker_4h = st.session_state.get("ticker_4h", _t4h)

# データ取得（60m→日足/4Hを生成するv21ロジック）
d1 = _dl_ohlc_v21(_ticker_1d, "1d", _candles)
h4 = _dl_ohlc_v21(_ticker_4h, "4h", _candles)
# --- 安全描画ユーティリティ（空データ/Noneでも落ちない） ---
def _is_empty_like(v):
    try:
        if v is None:
            return True
        if hasattr(v, "empty") and v.empty:
            return True
        if hasattr(v, "index") and len(v.index) == 0:
            return True
    except Exception:
        return True
    return False

def _placeholder_fig(title):
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.update_layout(
        title=f"{title}：データなし",
        template="plotly_white",
        height=320,
        margin=dict(l=8, r=8, t=40, b=8),
    )
    return fig

def _safe_plotly_chart(title, v, _cfg):
    fig = _placeholder_fig(title) if _is_empty_like(v) else _build_one_v21(title, v)
    st.plotly_chart(fig, use_container_width=True, config=_cfg)
# --- /安全描画ユーティリティ ---

# --- 描画 ---
if d1 is None:
    st.info("日足データを取得できませんでした。")
else:
    _safe_plotly_chart(f"{_ticker_1d}（日足）", d1, _cfg)

if h4 is None:
    st.info("4時間足データを取得できませんでした。")
else:
    _safe_plotly_chart(f"{_ticker_4h}（4時間足）", h4, _cfg)




# ==== 段落② UIブロック（置き換え版） =====================================

# ---- 時間軸の使い方（旧ラジオの代替） ----
st.markdown("#### 時間軸の使い方")
st.radio(
    "段落②で参照する時間軸",
    ["日足のみ", "4時間足のみ", "両方（半々）"],
    horizontal=True,
    key="tf_mix_mode",
    help="『両方（半々）』を選ぶと、本文を日足と4時間足で50:50の比重で組み立てます。"
)

# ---- 互換シム：旧コードが参照する tf_base_choice を自動設定 ----
_mix = st.session_state.get("tf_mix_mode", "両方（半々）")
if _mix == "日足のみ":
    st.session_state["tf_base_choice"] = "日足"
elif _mix == "4時間足のみ":
    st.session_state["tf_base_choice"] = "4時間足"
else:
    # 『両方（半々）』は旧ロジック上は「自動」と等価に扱う
    st.session_state["tf_base_choice"] = "自動"

# ---- D1/H4の印象（必須） ----
st.markdown("#### D1/H4の印象（必須）")
_IMP_CHOICES = ["横ばい", "緩やかなアップ", "アップ", "強いアップ", "緩やかなダウン", "ダウン", "強いダウン"]

# 時間軸の使い方（未設定時は両方扱い）
mix = st.session_state.get("tf_mix_mode", "両方（半々）")

col_d1, col_h4 = st.columns(2)

with col_d1:
    # ✅ D1は常に操作可能（H4のみでも必ず選べる）
    st.selectbox(
        "日足（D1）の印象",
        _IMP_CHOICES,
        index=_IMP_CHOICES.index(st.session_state.get("d1_imp", "横ばい"))
        if st.session_state.get("d1_imp") in _IMP_CHOICES else 0,
        key="d1_imp",
        help="直感でOK。横ばい/アップ/ダウン＋強弱から最も近いものを選択。",
        disabled=False,  # ← ここを常に False
    )

with col_h4:
    # ✅ 「日足のみ」のときだけH4を無効化（それ以外は操作可）
    if mix == "日足のみ":
        st.selectbox("4時間足（H4）の印象", ["（日足のみを選択中：操作できません）"], index=0, key="h4_imp_dummy", disabled=True)
    else:
        st.selectbox(
            "4時間足（H4）の印象",
            _IMP_CHOICES,
            index=_IMP_CHOICES.index(st.session_state.get("h4_imp", "横ばい"))
            if st.session_state.get("h4_imp") in _IMP_CHOICES else 0,
            key="h4_imp",
            help="直感でOK。横ばい/アップ/ダウン＋強弱から最も近いものを選択。",
            disabled=False,
        )



# ---- ここから：段落②の時間軸制御フラグ ----
_mix = st.session_state.get("tf_mix_mode", "両方（半々）")
DISABLE_H4 = (_mix == "日足のみ")
DISABLE_D1 = (_mix == "4時間足のみ")
st.session_state["p2_disable_h4"] = DISABLE_H4
st.session_state["p2_disable_d1"] = DISABLE_D1

# 段落②の自動補完（初心者向け）デフォルトON
if "p2_auto_complete" not in st.session_state:
    st.session_state["p2_auto_complete"] = True

def _coarse_trend(label: str) -> str:
    s = str(label or "")
    if "アップ" in s: return "上向き"
    if "ダウン" in s: return "下向き"
    return "横ばい"

# 自動/日足/H4 の解決（インジごと）
def _resolve_axis(pref_key: str, category: str) -> str:
    """戻り: 'D1' or 'H4'"""
    pref = str(st.session_state.get(pref_key, "自動") or "自動")
    mix  = st.session_state.get("tf_mix_mode", "両方（半々）")
    if pref != "自動":
        return "D1" if "日足" in pref or "D1" in pref.upper() else "H4"
    # 自動規則
    if mix == "日足のみ":      return "D1"
    if mix == "4時間足のみ":   return "H4"
    # 両方（半々）の既定
    if category == "MA":       return "D1"  # 長期骨格
    return "H4"                 # 短期の張りつき/過熱
# ---- ここまで：段落②の時間軸制御フラグ ----

# ==== NEW: 移動平均クロス（20↔200, 任意） ====
st.markdown("#### 移動平均クロス（20↔200, 任意）")

# 参照時間軸（MA）— 重複キー回避の新ラジオ
_mix = st.session_state.get("tf_mix_mode", "両方（半々）")
if _mix == "日足のみ":
    _ma_axis_options = ["日足（D1）"]
elif _mix == "4時間足のみ":
    _ma_axis_options = ["4時間足（H4）"]
else:
    _ma_axis_options = ["自動", "日足（D1）", "4時間足（H4）"]

_ma_axis_selected = st.radio(
    "参照時間軸（MA）",
    _ma_axis_options,
    horizontal=True,
    key="p2_ma_axis_ui2",   # ← ユニークな新キー（重複回避）
    help="MAの時間軸。日足のみ/4時間足のみを選ぶと固定になります。"
)

# 実効値を統一キーに格納（後段互換のため）
st.session_state["p2_ma_axis_effective"] = _ma_axis_selected
st.session_state["p2_ma_axis"] = st.session_state["p2_ma_axis_effective"]  # 旧ロジック互換

# 旧UI（参照時間軸ラジオ／時間軸セレクト）は撤去し、互換値だけ同期
# gc_axis を参照する既存コード向けに、実効値から対応づけておく
_gc_map = {"自動": "未選択", "日足（D1）": "日足（D1）", "4時間足（H4）": "4時間足（H4）"}
st.session_state["gc_axis"] = _gc_map.get(st.session_state["p2_ma_axis_effective"], "未選択")

# レイアウトは従来通り2カラムのまま（左は説明だけ、右に状態セレクト）
col_axis, col_cross = st.columns([1, 2])
with col_axis:
    st.caption("（参照軸は上のラジオで制御します。旧「参照時間軸/時間軸」は廃止）")

with col_cross:
    st.selectbox(
        "状態",
        [
            "未選択",
            "ゴールデンクロス（短期20MAが長期200MAを上抜け）",
            "デッドクロス（短期20MAが長期200MAを下抜け）",
        ],
        key="gc_state",
        help="ゴールデンクロス＝短期（20MA）が長期（200MA）を上抜け。デッドクロス＝短期が長期を下抜け。発生日の厳密指定は不要です。"
    )


# ==== 整合性判定（D1/H4の印象 × 20/200クロス） ====

def _trend_sign_from_label(label: str) -> int:
    """
    印象テキストから、上向き=+1 / 下向き=-1 / 横ばい=0 を返す。
    """
    s = str(label or "")
    if "アップ" in s:
        return 1
    if "ダウン" in s:
        return -1
    return 0  # 横ばい等

def _cross_sign(gc_state: str) -> int | None:
    """
    GC/DCの状態から、GC=+1 / DC=-1 / 未選択=None を返す。
    """
    s = str(gc_state or "")
    if "ゴールデンクロス" in s:
        return 1
    if "デッドクロス" in s:
        return -1
    return None  # 未選択など

def _consistency_judge(d1_imp: str, h4_imp: str, gc_axis: str, gc_state: str) -> tuple[str, str]:
    """
    戻り値: (バッジ文, 補足説明)
    バッジは本文には入れず、UIに小さく表示する想定。
    """
    ax = str(gc_axis or "")
    cs = _cross_sign(gc_state)           # +1 / -1 / None
    if cs is None or ax == "未選択":
        return ("", "")  # 比較対象なし → 何も出さない

    # 比較するトレンドの軸を決定
    if "日足" in ax:
        ts = _trend_sign_from_label(d1_imp)
    elif "4" in ax:  # 「4時間足」をざっくり検出
        ts = _trend_sign_from_label(h4_imp)
    else:
        ts = 0

    # 片方が中立(0)なら「△」
    if ts == 0:
        return ("**🟡 整合性△**", "比較材料が限定的。断定は避けつつ、様子見が無難。")

    # 同方向 → 「◯」、逆方向 → 「⚠」
    if ts == cs:
        return ("**🟢 整合性◯**", "人の印象と20/200の基調がおおむね整合。流れの確認がしやすい局面。")
    else:
        return ("**🟠 整合性⚠**", "人の印象と20/200の基調が逆行気味。本文は人の選択を優先しつつ過度な決めつけは避けたい。")


st.caption("※ 発生日の厳密指定は不要。文章では『発生して以降』など時制をぼかして表現します。")

# ==== 整合性バッジ表示（本文には出しません） ====
badge, tip = _consistency_judge(
    st.session_state.get("d1_imp", "横ばい"),
    st.session_state.get("h4_imp", "横ばい"),
    st.session_state.get("gc_axis", "未選択"),
    st.session_state.get("gc_state", "未選択"),
)
if badge:
    st.markdown(badge)   # 🟢/🟠/🟡 のいずれか
    st.caption(tip)      # 補足説明（小さく表示）

# ==== NEW: ボリンジャーバンド（任意） ====
st.markdown("#### ボリンジャーバンド（任意）")

# 参照時間軸（BB）— 時間軸の使い方に連動（重複キー回避の新キー）
_mix = st.session_state.get("tf_mix_mode", "両方（半々）")
if _mix == "日足のみ":
    _bb_axis_options = ["日足（D1）"]
elif _mix == "4時間足のみ":
    _bb_axis_options = ["4時間足（H4）"]
else:
    _bb_axis_options = ["自動", "日足（D1）", "4時間足（H4）"]

_bb_axis_selected = st.radio(
    "参照時間軸（BB）",
    _bb_axis_options,
    horizontal=True,
    key="p2_bb_axis_ui2",   # ← ユニークな新キー（重複回避）
    help="BBの時間軸。日足のみ/4時間足のみを選ぶと固定になります。"
)

# 実効値を互換キーへ同期（既存ロジックを壊さない）
st.session_state["p2_bb_axis_effective"] = _bb_axis_selected
st.session_state["p2_bb_axis"] = st.session_state["p2_bb_axis_effective"]
_bb_map = {"自動": "未選択", "日足（D1）": "日足（D1）", "4時間足（H4）": "4時間足（H4）"}
st.session_state["bb_axis"] = _bb_map.get(st.session_state["p2_bb_axis_effective"], "未選択")

# レイアウト：左に説明、右に状態セレクト（旧UIは撤去）
col_axis, col_state = st.columns([1, 2])
with col_axis:
    st.caption("（参照軸は上のラジオで制御します。旧UIは廃止）")

with col_state:
    _bb_state_val = st.selectbox(
        "状態（ボリンジャーバンド(20, ±2σ)）",
        [
            "未選択",
            "上限付近（価格が上バンドに接近）",
            "中心線付近（価格がミドルバンド付近）",
            "下限付近（価格が下バンドに接近）",
            "収縮（バンド幅が狭い＝ボラ低下）",
            "拡大（バンド幅が広がる＝ボラ上昇）",
            "上方向のバンドウォーク（上バンド沿いに推移）",
            "下方向のバンドウォーク（下バンド沿いに推移）",
        ],
        key="p2_bb_state",
        help="未選択のままでOK。選んだ場合のみ短句で反映します。"
    )
    # 互換キー（もし他所で参照していれば）
    st.session_state["bb_state"] = _bb_state_val

st.caption("※ 表記は ボリンジャーバンド(20, ±2σ)。ここでの選択は後の文章反映で自然に使います。")

# ---- BB選択肢→短句マッピング（軸併記つき・公開本文にはまだ入れない）----
def _bb_short_sentence(state_label: str) -> str:
    label = (state_label or "").strip()
    if not label or label == "未選択":
        return ""
    mapping = {
        "上限付近（価格が上バンドに接近）": "ボリンジャーバンド(20, ±2σ)は上限付近。",
        "中心線付近（価格がミドルバンド付近）": "ボリンジャーバンド(20, ±2σ)は中心線付近。",
        "下限付近（価格が下バンドに接近）": "ボリンジャーバンド(20, ±2σ)は下限付近。",
        "収縮（バンド幅が狭い＝ボラ低下）": "ボリンジャーバンド(20, ±2σ)は収縮気味。",
        "拡大（バンド幅が広がる＝ボラ上昇）": "ボリンジャーバンド(20, ±2σ)は拡大型。",
        "上方向のバンドウォーク（上バンド沿いに推移）": "上方向のバンドウォーク気味。",
        "下方向のバンドウォーク（下バンド沿いに推移）": "下方向のバンドウォーク気味。",
    }
    return mapping.get(label, "")

# 軸（D1/H4）の併記
axis_eff = str(st.session_state.get("p2_bb_axis_effective", "自動"))
axis_suffix = ""
if "日足" in axis_eff or axis_eff.upper() == "D1":
    axis_suffix = "（日足）"
elif "4時間" in axis_eff or axis_eff.upper() == "H4":
    axis_suffix = "（4時間足）"

_bb_sentence = _bb_short_sentence(st.session_state.get("bb_state", "未選択"))
if _bb_sentence and axis_suffix:
    _bb_sentence = (_bb_sentence[:-1] if _bb_sentence.endswith("。") else _bb_sentence) + axis_suffix + "。"

# 次の手順で本文組み込みに使うため、セッションに保存
st.session_state["p2_bb_sentence_preview"] = _bb_sentence

# 画面で軽く確認用（公開本文には混ぜない）
st.caption(f"プレビュー（BB短句）：{_bb_sentence or '—（未選択）'}")
# ---- /BB短句マッピングここまで ----

# ==== NEW: RSI（任意） ====
st.markdown("#### RSI（任意）")

# 参照時間軸（RSI）— 重複キー回避の新ラジオ
_mix = st.session_state.get("tf_mix_mode", "両方（半々）")
if _mix == "日足のみ":
    _rsi_axis_options = ["日足（D1）"]
elif _mix == "4時間足のみ":
    _rsi_axis_options = ["4時間足（H4）"]
else:
    _rsi_axis_options = ["自動", "日足（D1）", "4時間足（H4）"]

_rsi_axis_selected = st.radio(
    "参照時間軸（RSI）",
    _rsi_axis_options,
    horizontal=True,
    key="p2_rsi_axis_ui2",   # ← 新しいユニークキー（重複回避）
    help="RSIの時間軸。日足のみ/4時間足のみを選ぶと固定になります。"
)

# 実効値を互換キーへ同期（後段の既存ロジックを壊さない）
st.session_state["p2_rsi_axis_effective"] = _rsi_axis_selected
st.session_state["p2_rsi_axis"] = st.session_state["p2_rsi_axis_effective"]
_map = {"自動": "未選択", "日足（D1）": "日足（D1）", "4時間足（H4）": "4時間足（H4）"}
st.session_state["rsi_axis"] = _map.get(st.session_state["p2_rsi_axis_effective"], "未選択")

# レイアウトは従来通り：左に説明、右に状態セレクト（旧UIは撤去）
col_axis, col_state = st.columns([1, 2])
with col_axis:
    st.caption("（参照軸は上のラジオで制御します。旧UIは廃止）")

with col_state:
    _rsi_state_val = st.selectbox(
        "状態（RSI 14）",
        ["未選択", "70接近", "50前後", "30接近", "ダイバージェンス示唆"],
        key="p2_rsi_state",
        help="未選択のままでも本文には出しません。選んだ場合のみ短句で反映。"
    )
    # 互換キー（もし他所で参照していれば）
    st.session_state["rsi_state"] = _rsi_state_val
# ---- RSI選択肢→短句マッピング（現行/旧ラベルどちらも対応）----
def _rsi_short_sentence(state_label: str) -> str:
    label = (state_label or "").strip()
    if not label or label == "未選択":
        return ""
    mapping = {
        # 現行ラベル
        "70接近": "RSI(14)は70接近。",
        "50前後": "RSI(14)は50前後。",
        "30接近": "RSI(14)は30接近。",
        "ダイバージェンス示唆": "RSI(14)にダイバージェンス示唆。",
        # 旧ラベル（互換）
        "70以上（買われすぎ気味）": "RSI(14)は70超で買われすぎ気味。",
        "60〜70（上向きバイアス）": "RSI(14)は60台で上向きバイアス。",
        "40〜50（下向きバイアス）": "RSI(14)は40〜50で下向きバイアス。",
        "30〜40（売られ気味）": "RSI(14)は30台で売られ気味。",
        "30未満（売られすぎ気味）": "RSI(14)は30割れで売られすぎ気味。",
        "ダイバージェンスあり（価格とRSIの方向が逆）": "RSI(14)にダイバージェンス示唆。",
    }
    return mapping.get(label, "")

# 現在の選択から短句を作成＋軸（D1/H4）を併記
axis_eff = str(st.session_state.get("p2_rsi_axis_effective", "自動"))
axis_suffix = ""
if "日足" in axis_eff or axis_eff.upper() == "D1":
    axis_suffix = "（日足）"
elif "4時間" in axis_eff or axis_eff.upper() == "H4":
    axis_suffix = "（4時間足）"

_rsi_sentence = _rsi_short_sentence(st.session_state.get("rsi_state", "未選択"))
if _rsi_sentence and axis_suffix:
    # 文末の句点を一旦外して軸を付け足す
    _rsi_sentence = (_rsi_sentence[:-1] if _rsi_sentence.endswith("。") else _rsi_sentence) + axis_suffix + "。"

# 次の手順で本文組み込みに使うため、セッションに保存
st.session_state["p2_rsi_sentence_preview"] = _rsi_sentence

# 画面で軽く確認用（公開本文には混ぜない）
st.caption(f"プレビュー（RSI短句）：{_rsi_sentence or '—（未選択）'}")
# ---- /RSI短句マッピングここまで ----




# ---- ブレークポイント（任意・共通） ----
st.markdown("#### ブレークポイント（任意）")
_pair_label = st.session_state.get("pair", "")
# 円絡みは小数2桁（JPY or '円' を検出）、それ以外は小数4桁
_decimals = 2 if (("JPY" in _pair_label) or ("円" in _pair_label)) else 4
_step = 10 ** (-_decimals)
_fmt = f"%.{_decimals}f"

col_up, col_dn = st.columns(2)
with col_up:
    _bp_up_default = float(st.session_state.get("bp_up_default", 0.0))
    bp_up = st.number_input(
        "上側（例：155.00 など）",
        min_value=0.0, value=_bp_up_default,
        step=_step, format=_fmt,
        help="未入力でもOK。入力した場合は本文に『○○付近』として使います。"
    )
    st.session_state["bp_up"] = bp_up if bp_up > 0 else None

with col_dn:
    _bp_dn_default = float(st.session_state.get("bp_dn_default", 0.0))
    bp_dn = st.number_input(
        "下側（例：153.80 など）",
        min_value=0.0, value=_bp_dn_default,
        step=_step, format=_fmt,
        help="未入力でもOK。入力した場合は本文に『○○付近』として使います。"
    )
    st.session_state["bp_dn"] = bp_dn if bp_dn > 0 else None

# --- 共通BPを段落②用キーに同期（文章が読む名前へコピー） ---
st.session_state["p2_bp_upper"] = st.session_state.get("bp_up")
st.session_state["p2_bp_lower"] = st.session_state.get("bp_dn")

st.caption("※ 入力は“ざっくり”でOK。文章では常に『○○付近／どころ／前後』などの近似表現に整えます。")

# ---- 時間軸別ブレークポイント（任意） ----
with st.expander("時間軸別に設定（任意）", expanded=False):
    mix = st.session_state.get("tf_mix_mode", "両方（半々）")

    col_d1_bp, col_h4_bp = st.columns(2)

    # ---- D1 側 ----
    with col_d1_bp:
        st.markdown("**日足（D1）**")
        st.number_input(
            "D1 上側",
            min_value=0.0,
            value=float(st.session_state.get("bp_d1_up", 0.0)),
            step=_step, format=_fmt, key="bp_d1_up",
            disabled=(mix == "4時間足のみ"),  # H4のみ→D1のBPは操作不可
        )
        st.number_input(
            "D1 下側",
            min_value=0.0,
            value=float(st.session_state.get("bp_d1_dn", 0.0)),
            step=_step, format=_fmt, key="bp_d1_dn",
            disabled=(mix == "4時間足のみ"),
        )

    # ---- H4 側 ----
    with col_h4_bp:
        st.markdown("**4時間足（H4）**")
        st.number_input(
            "H4 上側",
            min_value=0.0,
            value=float(st.session_state.get("bp_h4_up", 0.0)),
            step=_step, format=_fmt, key="bp_h4_up",
            disabled=(mix == "日足のみ"),    # 日足のみ→H4のBPは操作不可
        )
        st.number_input(
            "H4 下側",
            min_value=0.0,
            value=float(st.session_state.get("bp_h4_dn", 0.0)),
            step=_step, format=_fmt, key="bp_h4_dn",
            disabled=(mix == "日足のみ"),
        )



def _compose_para2_base_from_state() -> str:
    """段落②の冒頭ベース文（D1/H4の出し分け規律）。"""
    pair = str(st.session_state.get("pair", "") or "")
    mix  = st.session_state.get("tf_mix_mode", "両方（半々）")
    d1   = st.session_state.get("d1_imp", "横ばい")
    h4   = st.session_state.get("h4_imp", "横ばい")

    if mix == "日足のみ":
        return f"為替市場は、{pair}は日足は{d1}。"
    if mix == "4時間足のみ":
        return f"為替市場は、{pair}は日足は{_coarse_trend(d1)}。4時間足は{h4}。"
    # 両方（半々）
    return f"為替市場は、{pair}は日足は{d1}、4時間足は{h4}。"

# ---- 段落②：フロー整形（軸ごとに統合・重複抑制・順序整備）----
def _p2_flow_polish(text: str) -> str:
    """
    ・「日足では…」「4時間足では…」をそれぞれ1文にまとめる（読点で接続）
    ・軸なしRSI（例:『RSIは50前後。』）が軸ありRSIと重なるときは軸ありを優先
    ・『為替市場は、…』→『ブレークポイント言及（〜付近〜）』→『日足では…』『4時間足では…』→その他
    ・文の重複/空白差重複を除去、句点を正規化
    """
    import re
    import unicodedata

    s = (text or "").strip()
    if not s:
        return s

    # 文ごとに分割
    parts = [p.strip() for p in re.split(r"。+", s) if p.strip()]

    d1_frags, h4_frags, others = [], [], []
    for p in parts:
        if p.startswith("日足では"):
            d1_frags.append(p.replace("日足では", "", 1).strip(" 、"))
        elif p.startswith("4時間足では"):
            h4_frags.append(p.replace("4時間足では", "", 1).strip(" 、"))
        else:
            others.append(p)

    def _key(x: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", x or ""))

    def _dedupe(seq):
        seen, out = set(), []
        for x in seq:
            k = _key(x)
            if k in seen:
                continue
            seen.add(k)
            out.append(x)
        return out

    d1_frags = _dedupe(d1_frags)
    h4_frags = _dedupe(h4_frags)

    # 軸ありRSIがどちらかに含まれるなら、軸なしRSIの独立文を落とす
    def _has_axis_rsi(frags) -> bool:
        return any(re.search(r"\bRSI(?:\s*\(14\))?\b", f) for f in frags)

    if _has_axis_rsi(d1_frags) or _has_axis_rsi(h4_frags):
        others = [p for p in others if not re.match(r"^\s*RSI(?:\s*\(14\))?\s*は", p)]

    # 軸ごとに1文へ凝縮
    grouped = []
    mix = st.session_state.get("tf_mix_mode", "両方（半々）")
    if d1_frags:
        grouped.append("日足では" + "、".join(d1_frags))
    if (mix != "日足のみ") and h4_frags:
        grouped.append("4時間足では" + "、".join(h4_frags))


    # 並び順：為替市場は→ブレークポイント（〜付近〜）→軸文→その他
    base = None
    rest = []
    for p in others:
        if base is None and p.startswith("為替市場は、"):
            base = p
        else:
            rest.append(p)

    bp_parts = [x for x in rest if "付近" in x]
    rest = [x for x in rest if x not in bp_parts]

    new_parts = []
    if base:
        new_parts.append(base)
    new_parts += bp_parts
    new_parts += grouped
    new_parts += rest

    # 重複除去＋句点正規化
    new_parts = _dedupe(new_parts)
    out = "。".join(new_parts).strip()
    out = re.sub(r"([。])\1+", r"\1", out).strip()
    if out and not out.endswith("。"):
        out += "。"
    return out



# ---- 共通の最終整形＋品質ガード（サンプル超えの体裁/語彙/重複管理）----
def _final_polish_and_guard(text: str, para: str = "full") -> str:
    """
    サンプル準拠ルールを満たしつつ、重複・体裁を除去して“サンプル超え”の読み味に仕上げる最終フィルタ。
    - 句点/読点の二重化除去、空白の正規化
    - ボリンジャーバンド／RSI／MA 表記の統一
    - よく出る定型文の多発を1回に圧縮
    - 文単位の NFKC 正規化＋空白除去キーで厳密重複排除
    - 結び文（行方を注視したい／値動きには警戒したい／方向感(性)を見極めたい／当面は静観としたい）を末尾1本に統一
    - 段落①（para="p1"）では「米国市場は、主要3指数…」の長短二重表現を長文だけ残す
    """
    import re
    import unicodedata

    t = (text or "").strip()

    # 1) 句読点・空白の体裁
    t = re.sub(r"([。])\1+", r"\1", t)        # 句点の重複
    t = re.sub(r"、{2,}", "、", t)            # 読点の重複
    t = re.sub(r"(、|。)\s*。", r"。", t)     # 読点直後の句点など
    t = re.sub(r"\s+", " ", t).strip()        # 余分な空白

    # 2) 用語の表記ロック（BB/RSI/MA）
    t = re.sub(r"ボリンジャーバンド\s*(?:\(20,\s*[±\+\-]?\s*2σ\))?\s*の\s*拡大", "ボリンジャーバンド(20, ±2σ)は拡大型", t)
    t = re.sub(r"ボリンジャーバンド\s*(?:\(20,\s*[±\+\-]?\s*2σ\))?\s*の\s*収縮", "ボリンジャーバンド(20, ±2σ)は収縮気味", t)
    t = re.sub(r"ボリンジャーバンド\s*(?:\(20,\s*[±\+\-]?\s*2σ\))?\s*は\s*拡大(?:中|傾向)?", "ボリンジャーバンド(20, ±2σ)は拡大型", t)
    t = re.sub(r"ボリンジャーバンド\s*(?:\(20,\s*[±\+\-]?\s*2σ\))?\s*は\s*収縮(?:中|傾向)?", "ボリンジャーバンド(20, ±2σ)は収縮気味", t)
    t = re.sub(r"\bBB\b", "ボリンジャーバンド(20, ±2σ)", t)
    t = re.sub(r"(拡大型)型\b", r"\1", t)
    t = re.sub(r"(収縮気味)気味\b", r"\1", t)

    t = re.sub(r"RSI\s*\(?\s*14\s*\)?", "RSI(14)", t)     # RSI 14 → RSI(14)
    t = re.sub(r"\bRSI\b\s*が\s*", "RSI(14)は", t)         # 「RSI が …」→「RSI(14)は…」
    t = re.sub(r"RSI\(14\)\s*が\s*", "RSI(14)は", t)      # 「RSI(14) が …」→「RSI(14)は…」
    t = re.sub(r"(\d+)\s*(接近|前後|割れ)", r"\1\2", t)   # 70 接近 → 70接近

    t = re.sub(r"(?<!S)20\s*MA", "20SMA", t)
    t = re.sub(r"(?<!S)200\s*MA", "200SMA", t)
    t = re.sub(r"20SMA\s+", "20SMA", t)
    t = re.sub(r"200SMA\s+", "200SMA", t)
    # p3 専用：冗長表現の正規化（段落③のタイトル回収を1文に統一）
    t = re.sub(r"方向感の見極めを確認したい。", "方向感を見極めたい。", t)

    # 3) 段落①専用：株式3指数の長短二重表現を長い方だけ残す（ゆらぎ吸収）
    if para == "p1":
        pattern_long  = r"米国(?:の)?(?:株式)?市場は、?\s*主要[3３]指数が(?:そろって|揃って)(?:上昇|下落|まちまち)とな(?:り|って)[^。]*。"
        pattern_short = r"米国(?:の)?(?:株式)?市場は、?\s*主要[3３]指数が(?:そろって|揃って)(?:上昇|下落|まちまち)となっ[たて]。"

        # 長文→短文 を 長文に圧縮
        t = re.sub(rf"({pattern_long})\s*{pattern_short}", r"\1", t)
        # 短文→長文 を 長文に圧縮
        t = re.sub(rf"{pattern_short}\s*({pattern_long})", r"\1", t)
        # 長文が存在するなら残りの短文は全消去
        if re.search(pattern_long, t):
            t = re.sub(rf"(?:^|。)\s*{pattern_short}", "。", t)
        # 3.1) 段落①専用：『まちまち』＋『1上昇・2下落』の重複を長文だけ残す
    if para == "p1":
        pat_mix = r"米国(?:の)?(?:株式)?市場は、?\s*主要[3３]指数はまちまちとなり、[^。]*。"
        pat_12  = r"米国(?:の)?(?:株式)?市場は、?\s*主要[3３]指数のうち[０-９0-9]指数が上昇・[０-９0-9]指数が下落となった。"

        # 長文→短文 の並びを長文だけに圧縮
        t = re.sub(rf"({pat_mix})\s*{pat_12}", r"\1", t)
        # 短文→長文 の並びを長文だけに圧縮
        t = re.sub(rf"{pat_12}\s*({pat_mix})", r"\1", t)
        # 長文が本文に存在するなら、残る同短文は全消去
        if re.search(pat_mix, t):
            t = re.sub(rf"(?:^|。)\s*{pat_12}", "。", t)

    # 4) よく出る定型文の多発を圧縮（重複2連以上 → 1回）
    boiler = r"短期は20SMAやボリンジャーバンド周辺の反応を確かめつつ、過度な方向感は決めつけない構えとしたい。"
    t = re.sub(rf"(?:{boiler})\s*(?:{boiler})+", boiler, t)

    # 5) RSI短句の連続重複を除去（最初の1回だけ残す）
    _rsi_seen = False
    def _dedupe_rsi(m):
        nonlocal _rsi_seen
        if _rsi_seen:
            return ""
        _rsi_seen = True
        return m.group(0)
    t = re.sub(r"(?:、\s*)?RSI\(14\)は(?:70接近|60台|50前後|40〜50|30台|30接近|30割れ)(?:。|、)?", _dedupe_rsi, t)

    # 6) 文単位の厳格重複排除（NFKC 正規化＋空白・句点除去キー）
    def _key(s: str) -> str:
        x = unicodedata.normalize("NFKC", s or "")
        x = re.sub(r"\s+", "", x)
        x = x.replace("。", "")
        return x
    parts = [p for p in re.split(r"。+", t) if p.strip()]
    seen, uniq = set(), []
    for p in parts:
        k = _key(p)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p.strip())

        # 7) 途中に現れる結び文は一旦除去し、末尾に1本だけ戻す
    closers = {"方向感を見極めたい", "方向性を見極めたい", "行方を注視したい", "値動きには警戒したい", "当面は静観としたい", "一段の変動に要注意としたい"}
    closer_pat = r"(方向感を見極めたい|方向性を見極めたい|行方を注視したい|値動きには警戒したい|当面は静観としたい|一段の変動に要注意としたい)"
    found_closers = re.findall(closer_pat, t)  # 元文中に出た順序を尊重
    body = [p for p in uniq if p not in closers]
    tails = [p for p in uniq if p in closers]

    # ★タイトル尾語に合わせて段落②のクローザーを強制整合（para="p2" のときのみ）
    closer_map = {
        "注視か": "行方を注視したい",
        "警戒か": "値動きには警戒したい",
        "静観か": "当面は静観としたい",
        "要注意か": "一段の変動に要注意としたい",
        "見極めたい": "方向感を見極めたい",
    }
    desired_closer = ""
    if para == "p2":
        try:
            tail = (st.session_state.get("title_tail") or "").strip()
            if tail in closer_map:
                desired_closer = closer_map[tail]
        except Exception:
            pass

    if desired_closer:
        # タイトル尾語に一致する1本だけを末尾へ
        t = "。".join(body + [desired_closer]) + "。"
    else:
        # 従来ロジック（最後に出現したクローザー or 先頭からの tails[-1]）
        closer_to_use = (found_closers[-1] if found_closers else (tails[-1] if tails else ""))
        if closer_to_use:
            t = "。".join(body + [closer_to_use]) + "。"
        else:
            t = "。".join(uniq) + "。"


    # 8) 最終の句点重複と末尾句点の保証
    t = re.sub(r"([。])\1+", r"\1", t).strip()
    if t and not t.endswith("。"):
        t += "。"
    return t




# ---- 段落②インジ短句の合流＋時間軸規律（呼び出しに flow-polish を追加）----
def _p2_merge_indicators(txt: str) -> str:
    """
    段落②テキストに MA / RSI / BB の短句を合流。
    ・サンプル準拠の語彙は既存通り（『日足では／4時間足では』『20MA 下位→上位』『ボリンジャーバンド+2σ/-2σ』『中心線に向けての回帰』『RSI が 50 前後』等）
    ・BBが『拡大／収縮』のみでも +2σ / -2σ / 中心線 を自動補足（既存実装を維持）
    ・最後に _p2_flow_polish() で文章を統合して自然な流れに
    """
    import re

    def _ensure_period(s: str) -> str:
        s = (s or "").strip()
        return (s + "。") if (s and not s.endswith("。")) else s

    def _append_once(text: str, add: str) -> str:
        t = (text or "").strip()
        a = (add  or "").strip()
        if not a:
            return t
        if re.sub(r"\s+", "", a) in re.sub(r"\s+", "", t):
            return t
        t = _ensure_period(t)
        return (t + " " + a) if t else a

    out = (txt or "").strip()
    mix = st.session_state.get("tf_mix_mode", "両方（半々）")

    # ---- 軸ラベル（末尾スペース無し）----
    def _axis_text(raw: str, fallback_mix: str) -> str:
        ax = str(raw or "")
        if ("日足" in ax) or (ax.upper() == "D1"):
            return "日足では"
        if ("4時間" in ax) or (ax.upper() == "H4"):
            return "4時間足では"
        if fallback_mix == "日足のみ":
            return "日足では"
        if fallback_mix == "4時間足のみ":
            return "4時間足では"
        return ""

    # ---- MA ----
    ma_sentence = ""
    gc = str(st.session_state.get("gc_state", "未選択") or "")
    if gc and gc != "未選択":
        ma_ax = st.session_state.get("p2_ma_axis_effective", "自動")
        head  = _axis_text(ma_ax, mix)
        if "ゴールデン" in gc:
            core = "20MA 下位から上位へと移行。"
        elif "デッド" in gc:
            core = "20MA 上位から下位へと移行。"
        else:
            core = "20MA 近辺での推移。"
        ma_sentence = (head + core) if head else core

    # ---- RSI ----
    rsi_sentence = ""
    rsi_ax   = st.session_state.get("p2_rsi_axis_effective", "自動")
    rsi_head = _axis_text(rsi_ax, mix)
    rsi_val  = str(st.session_state.get("p2_rsi_state", "未選択") or "").strip()
    old2new = {
        "70以上（買われすぎ気味）": "70接近",
        "60〜70（上向きバイアス）": "60台",
        "50前後（中立）": "50前後",
        "40〜50（下向きバイアス）": "40〜50",
        "30〜40（売られ気味）": "30台",
        "30未満（売られすぎ気味）": "30割れ",
        "ダイバージェンスあり（価格とRSIの方向が逆）": "ダイバージェンス示唆",
    }
    rsi_val = old2new.get(rsi_val, rsi_val)
    if rsi_val and rsi_val != "未選択":
        if rsi_val == "70接近":
            core = "RSI が 70 接近。"
        elif rsi_val in ("60台", "40〜50", "30台"):
            core = f"RSI が {rsi_val}。"
        elif rsi_val == "50前後":
            core = "RSI が 50 前後。"
        elif rsi_val == "30接近":
            core = "RSI が 30 接近。"
        elif rsi_val == "30割れ":
            core = "RSI が 30 割れ。"
        elif rsi_val == "ダイバージェンス示唆":
            core = "RSI にダイバージェンス示唆。"
        else:
            core = f"RSI が {rsi_val}。"
        rsi_sentence = (rsi_head + core) if rsi_head else core

    # ---- BB（既存：拡大/収縮のみでもシグマ補足を必ず付与）----
    bb_sentence = ""
    bb_ax   = st.session_state.get("p2_bb_axis_effective", "自動")
    bb_head = _axis_text(bb_ax, mix)
    bb_val  = str(st.session_state.get("p2_bb_state", "未選択") or "").strip()

    def _bb_autofill_fragment(bb_axis_eff: str) -> str:
        try:
            pair_label = st.session_state.get("pair", "ドル円")
            t1d, t4h = _tickers_for(pair_label)
            sym = t1d
            axu = str(bb_axis_eff or "").upper()
            if ("H4" in axu) or ("4" in axu):
                sym = t4h
            h1, h4, d1 = ta_block(sym, days=120)
            df = d1 if sym == t1d else h4
            close = float(df["Close"].iloc[-1])
            up    = float(df["BB_up"].iloc[-1])
            dn    = float(df["BB_dn"].iloc[-1])
            mid   = float(df["SMA20"].iloc[-1]) if "SMA20" in df.columns else (up + dn) / 2.0
            width = max(1e-9, (up - dn))
            pos = (close - mid) / width
            if pos >= 0.40:  return "ボリンジャーバンド+2σ 付近での推移。"
            if pos <= -0.40: return "ボリンジャーバンド-2σ 付近での推移。"
            return "中心線に向けての回帰。"
        except Exception:
            return ""

    if bb_val and bb_val != "未選択":
        if "上方向のバンドウォーク" in bb_val:
            core = "ボリンジャーバンド+3σ に沿ってのバンドウォーク。"
        elif "下方向のバンドウォーク" in bb_val:
            core = "ボリンジャーバンド-3σ に沿ってのバンドウォーク。"
        elif "上限付近" in bb_val:
            core = "ボリンジャーバンド+2σ 付近での推移。"
        elif "下限付近" in bb_val:
            core = "ボリンジャーバンド-2σ 付近での推移。"
        elif "中心線付近" in bb_val:
            core = "中心線に向けての回帰。"
        elif "収縮" in bb_val:
            core = "ボリンジャーバンドの収縮。"
        elif "拡大" in bb_val:
            core = "ボリンジャーバンドの拡大。"
        else:
            core = ""
        if core:
            bb_sentence = (bb_head + core) if bb_head else core
        if core in ("ボリンジャーバンドの拡大。", "ボリンジャーバンドの収縮。"):
            if all(x not in bb_sentence for x in ("+2σ", "-2σ", "中心線")):
                extra = _bb_autofill_fragment(bb_ax)
                if extra:
                    bb_sentence = _append_once(bb_sentence, extra)

    # 旧表現の除去（保険）
    out = re.sub(r"(?:^|。)\s*20/200の[^。]*。", "。", out).strip()
    out = re.sub(r"(?:^|。)\s*ボリンジャーバンド\([^。]*\)[^。]*。", "。", out).strip()
    out = re.sub(r"\(±?2σ\)|（±?2σ）", "", out).strip()

    # 合流
    for frag in [ma_sentence, rsi_sentence, bb_sentence]:
        out = _append_once(out, frag)

    # 未選択/結び/重複の掃除（従来通り）
    out = re.sub(r"(?:^|。)\s*[^。]*未選択[^。]*。", "。", out).strip()
    out = re.sub(r"(?:^|。)\s*(方向性を見極めたい|方向感を見極めたい|行方を注視したい|値動きには警戒したい)[。]?", "。", out).strip()
    parts = [p for p in re.split(r"。+", out) if p.strip()]
    seen, uniq = set(), []
    for p in parts:
        k = re.sub(r"\s+", "", p)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p.strip())
    out = "。".join(uniq).strip()

    # ---- ★NEW：ここでフロー整形をかける ----
    out = _p2_flow_polish(out)

    # 時間軸規律（従来通り）
    if out:
        first = re.split(r"。+", out)[0]
        if mix == "日足のみ":
            first = re.sub(r"、\s*4時間足は[^。]*", "", first)
            rest  = re.sub(r"(?:^|。)\s*4時間足は[^。]*。", "。", out[len(first):]).strip("。")
            out   = (first + "。") + (rest + "。" if rest else "")
        elif mix == "4時間足のみ":
            def _coarse(x: str) -> str:
                s2 = str(x or "")
                if "アップ" in s2: return "上向き"
                if "ダウン" in s2: return "下向き"
                return "横ばい"
            first = re.sub(r"日足は([^、。]+)", lambda m: f"日足は{_coarse(m.group(1))}", first, count=1)
            out_parts = [first] + re.split(r"。+", out)[1:]
            out = "。".join([p for p in out_parts if p]).strip()
            if out and not out.endswith("。"):
                out += "。"

    # 結び（従来通り1本）
    try:
        p1_ctx = (st.session_state.get("p1_ui_preview_text")
                  or st.session_state.get("para1_text") or "")
        closer = choose_para2_closer(str(p1_ctx), out)
    except Exception:
        closer = "方向性を見極めたい。"
    out = re.sub(r"(?:^|。)\s*(方向性を見極めたい|方向感を見極めたい|行方を注視したい|値動きには警戒したい)[。]?\s*$", "", out).rstrip("。")
    out = (out + "。" if out else "") + (closer if closer.endswith("。") else closer + "。")

    # ========== 3) 句点の二重化などを軽く正規化 ==========
    out = _final_polish_and_guard(out, para="p2")
    return out













# ---- NEW: 段落② 専用・固定表記ロック（サンプル準拠） ----
def _p2_style_lock(text: str) -> str:
    """
    段落②の最終文に対して“表記だけ”をサンプル準拠へ揃えるロック。
    ※ 生成内容・構造は触らない。語句の表記ゆれ統一のみ。
    - RSI: 「RSI(14)」へ統一（全角/半角やスペース・括弧ゆれを吸収）
    - 軸表記: (日足)/(4時間足) を 全角括弧 に統一
    - BB: どんな書き方でも「ボリンジャーバンド(20, ±2σ)」に統一
          「…の拡大/収縮」→「…は拡大型/収縮気味」に正規化
    """
    import re

    s = str(text or "")

    # --- RSI(14) の統一（表記ゆれ吸収）
    # 例: "RSI 14" "RSI（14）" "RSI( 14 )" など → "RSI(14)"
    s = re.sub(r"RSI\s*[（(]?\s*14\s*[)）]?", "RSI(14)", s)

    # --- 軸表記の括弧を全角へ
    s = s.replace("(日足)", "（日足）").replace("(4時間足)", "（4時間足）")

    # --- 既存のBB表記を一旦 正規形へ寄せる（(20, ±2σ) に統一）
    # 1) すでに括弧付きだが表記ゆれしているもの
    s = re.sub(r"ボリンジャーバンド\s*[（(]\s*20\s*[,，]?\s*±?\s*2\s*σ\s*[)）]", "ボリンジャーバンド(20, ±2σ)", s)
    s = re.sub(r"ボリンジャーバンド\s*[（(]\s*±?\s*2\s*σ\s*[)）]",               "ボリンジャーバンド(20, ±2σ)", s)
    s = re.sub(r"ボリンジャーバンド\s*[（(]\s*20\s*[)）]",                         "ボリンジャーバンド(20, ±2σ)", s)

    # 2) 括弧が無い or 直後が括弧でないものには (20, ±2σ) を付与
    #    （すでに括弧が続くケースは上の置換で正規化済みなので除外）
    s = re.sub(r"ボリンジャーバンド(?!\s*[（(])", "ボリンジャーバンド(20, ±2σ)", s)

    # 3) 「…の拡大/収縮」をサンプル準拠の述語に整形
    s = re.sub(r"ボリンジャーバンド\(20, ±2σ\)の拡大", "ボリンジャーバンド(20, ±2σ)は拡大型", s)
    s = re.sub(r"ボリンジャーバンド\(20, ±2σ\)の収縮", "ボリンジャーバンド(20, ±2σ)は収縮気味", s)

    # 4) 述語の微表記ゆれ（「拡大。」→「拡大型。」等）を補正
    s = re.sub(r"ボリンジャーバンド\(20, ±2σ\)は拡大([。])",    r"ボリンジャーバンド(20, ±2σ)は拡大型\1", s)
    s = re.sub(r"ボリンジャーバンド\(20, ±2σ\)は収縮([。])",    r"ボリンジャーバンド(20, ±2σ)は収縮気味\1", s)
    s = re.sub(r"ボリンジャーバンド\(20, ±2σ\)\s*拡大",         "ボリンジャーバンド(20, ±2σ)は拡大型", s)
    s = re.sub(r"ボリンジャーバンド\(20, ±2σ\)\s*収縮",         "ボリンジャーバンド(20, ±2σ)は収縮気味", s)

    # 5) 軽い重複ガード： "...(20, ±2σ)(20, ±2σ)..." のような事故の連結を1つに
    s = re.sub(r"\(20, ±2σ\)\(20, ±2σ\)", "(20, ±2σ)", s)

    return s

# ---- /固定表記ロック ----



# ---- 段落② 最終表示前サニタイザ（重複/フィラー/結び整理・強化版）----
def _p2_scrub_redundancy(text: str) -> str:
    import re
    s = (text or "").strip()
    if not s:
        return s

    # 句点の軽い正規化
    s = re.sub(r"[。]+\s*", "。", s).strip()

    # 1) 本文中に紛れた「結び」系を一旦すべて除去（最後に1回だけ付け直す）
    closer_pat = r"(?:方向感を見極めたい|方向性を見極めたい|行方を注視したい|値動きには警戒したい|当面は静観としたい)"
    s = re.sub(rf"(?:^|。)\s*{closer_pat}[。]?", "。", s).strip()

    # 2) 機械感の強い定型フィラーの重複を抑止
    #    （同義・表記ゆれを吸収するゆるめのパターン）
    filler_pat = (
        r"短期は[^。]{0,120}?(?:20SMA|20SMAや|20MA|ボリンジャーバンド)[^。]{0,120}?"
        r"(?:過度[^。]{0,40}?方向感[^。]{0,40}?決めつけない[^。]{0,40}?構えとしたい)"
    )
    had_filler = bool(re.search(filler_pat, s))
    # いったん全文から当該フィラーを除去（複数あっても全部消す）
    s = re.sub(rf"(?:^|。)\s*(?:{filler_pat})[。]?", "。", s).strip()

    # 3) 文単位の重複削除（空白差無視・順序保持）
    parts = [p for p in re.split(r"。+", s) if p.strip()]
    seen, uniq = set(), []
    for p in parts:
        key = re.sub(r"\s+", "", p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p.strip())
    s = "。".join(uniq).strip()

    # 4) もしフィラーが元文に存在していた場合のみ、規定文面で1回だけ末尾へ再配置
    #    （結び文の直前に置く／同文が既にあれば何もしない）
    canonical_filler = "短期は20SMAやボリンジャーバンド周辺の反応を確かめつつ、過度な方向感は決めつけない構えとしたい。"
    if had_filler and (re.sub(r"\s+", "", canonical_filler) not in re.sub(r"\s+", "", s)):
        s = s.rstrip("。")
        s = (s + "。") if s else ""
        s += canonical_filler

    # 5) 最後に結びを1本だけ付与（サンプル準拠：既存ヘルパを利用）
    try:
        p1_ctx = (st.session_state.get("p1_ui_preview_text")
                  or st.session_state.get("para1_text") or "")
        closer = choose_para2_closer(str(p1_ctx), s)  # 内部で安全フォールバック
    except Exception:
        closer = "方向感を見極めたい。"

    # 念のため、末尾に残っている結び系を落としてから付け直す
    s = re.sub(rf"(?:^|。)\s*{closer_pat}[。]?\s*$", "", s).rstrip("。")
    s = (s + "。") if s else ""
    s += closer if closer.endswith("。") else closer + "。"

    # 6) 句点の二重化など最終正規化
    s = re.sub(r"([。])\1+", r"\1", s).strip()
    # ← ここで表記ゆれをサンプル準拠にロック
    try:
        s = _p2_style_lock(s)
    except Exception:
        # 万一エラーでも本文生成を止めない
        pass
    return s
# ---- /サニタイザここまで ----

def _final_para2_sanitize(s: str) -> str:
    """
    段落②の最終直前でだけかけるサニタイザ。
    - 「短期は20SMA…構えとしたい。」の多発を1回に圧縮
    - 「RSI(14)は50前後。」や「ボリンジャーバンド(20, ±2σ)は拡大型。」等の重複行を除去
    - 文単位で NFKC 正規化＋空白除去キーによる厳格重複排除
    - 結び（行方を注視したい／値動きには警戒したい／方向感(性)を見極めたい／当面は静観としたい）を末尾に1本だけ残す
    """
    import re, unicodedata
    if not s:
        return s

    s = s.strip()

    # 軽い句点統一
    s = re.sub(r"([。])\1+", r"\1", s)

    # よく重複する定型文を1回に圧縮
    boiler = r"短期は20SMAやボリンジャーバンド周辺の反応を確かめつつ、過度な方向感は決めつけない構えとしたい。"
    s = re.sub(rf"(?:{boiler})\s*(?:{boiler})+", boiler, s)

    # RSI / BB の同内容重複を後勝ちで抑制（前方の重複を削除）
    s = re.sub(
        r"(?:、\s*)?RSI\(14\)は(?:70接近|60台|50前後|40〜50|30台|30接近|30割れ)(?:。|、)?(?=.*RSI\(14\)は)",
        "",
        s,
    )
    s = re.sub(
        r"(?:、\s*)?ボリンジャーバンド\(20,\s*±2σ\)は(?:拡大型|収縮気味)(?:。|、)?(?=.*ボリンジャーバンド\(20,\s*±2σ\)は)",
        "",
        s,
    )

    # 文単位の厳格重複排除（NFKC→空白除去→句点除去で同一判定）
    parts = [p for p in re.split(r"。+", s) if p.strip()]
    seen, uniq = set(), []
    for p in parts:
        key = unicodedata.normalize("NFKC", re.sub(r"\s+", "", p))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p.strip())

    # 結びを末尾に1本だけ残す
    closers = (
        "方向感を見極めたい",
        "方向性を見極めたい",
        "行方を注視したい",
        "値動きには警戒したい",
        "当面は静観としたい",
    )
    body = [p for p in uniq if p not in closers]
    tail = next((p for p in reversed(uniq) if p in closers), None)
    if tail:
        uniq = body + [tail]

    s = "。".join(uniq) + "。"

    # 最終の句読点整形
    s = re.sub(r"([。])\1+", r"\1", s).strip()
    return s


def _compose_para2_preview_mix() -> str:
    """
    段落②プレビューの確定文（UI選択＋手入力＋BP短句を統合、重複は抑制）。
    - ベース: _compose_para2_preview_from_ui()
    - 追記: MA(20/200), BB(20,±2σ), RSI(14)（UI/手入力のどちらでも拾う）
    - BP短句: 上/下のいずれか（両方あれば両方）を1本だけ追加
    - 既に含まれる表現は二重にしない（簡易デデュープ）
    """
    import re

    # ---------- 1) ベース（UIプレビュー素文） ----------
    try:
        base = _compose_para2_preview_from_ui()
    except Exception:
        pair   = str(st.session_state.get("pair", "") or "")
        d1     = st.session_state.get("d1_imp", "横ばい")
        h4     = st.session_state.get("h4_imp", "横ばい")
        tfmode = st.session_state.get("tf_mix_mode", "両方（半々）")
        market = _market_word_for(pair)

        if tfmode == "日足のみ":
            base = f"{market}は、{pair}は日足は{d1}。"
        elif tfmode == "4時間足のみ":
            base = f"{market}は、{pair}は4時間足は{h4}。"
        else:  # 両方（半々）
            base = f"{market}は、{pair}は日足は{d1}、4時間足は{h4}。"
    base = (base or "").strip()


    # ---------- 2) session_state から柔軟に値を拾うユーティリティ ----------
    def _ss_pick(keys=None, substrings=None, default=""):
        """
        keys: 優先して見るキーの配列（順番重視）
        substrings: 見当たらない時に、キー名に含まれる部分文字列で総当り検索
        """
        ss = st.session_state
        # 第一優先: 明示キー
        if keys:
            for k in keys:
                v = ss.get(k)
                if v is None: 
                    continue
                if isinstance(v, (list, tuple)) and v:
                    v = v[0]
                s = str(v).strip()
                if s:
                    return s
        # 第二優先: 部分一致検索
        if substrings:
            subs = [s.lower() for s in substrings]
            for k, v in ss.items():
                if not isinstance(k, str):
                    continue
                kl = k.lower()
                if all(sub in kl for sub in subs):
                    if v is None:
                        continue
                    if isinstance(v, (list, tuple)) and v:
                        v = v[0]
                    s = str(v).strip()
                    if s:
                        return s
        return default

    def _axis_label(raw: str) -> str:
        s = str(raw or "").upper()
        if "H4" in s or "4" in s:
            return "4時間足"
        return "日足"  # 既定はD1扱い

        # ---------- 3) MA / BB / RSI を拾って短句を作る（UI・手入力両対応） ----------
    # 軸の解決（自動→D1/H4）
    ma_axis  = _resolve_axis("p2_ma_axis",  category="MA")
    bb_axis  = _resolve_axis("p2_bb_axis",  category="BB")
    rsi_axis = _resolve_axis("p2_rsi_axis", category="RSI")

    def _axis_label_jp(ax: str) -> str:
        return "日足" if ax.upper() == "D1" else "4時間足"

    # === MA(20↔200)
    ma_state = _ss_pick(
        keys=["p2_ma_state","ma_state","ma_cross_state","p2_ma_manual","ma_state_select","ma_manual","gc_state"],
        substrings=["gc","ma","state","cross"],
        default=""
    )
    # 自動補完（未選択なら推定）
    if not ma_state and st.session_state.get("p2_auto_complete", True):
        try:
            pair_label = st.session_state.get("pair", "ドル円")
            t1d, t4h = _tickers_for(pair_label)
            sym = t1d if ma_axis == "D1" else t4h
            h1, h4, d1 = ta_block(sym, days=180)
            df = d1 if ma_axis == "D1" else h4
            s20 = df["SMA20"].iloc[-60:]
            s200 = df["SMA200"].iloc[-60:]
            diff = (s20 - s200)
            sign = (diff > 0).astype(int) - (diff < 0).astype(int)
            cross_points = (sign.diff().fillna(0) != 0)
            last_cross_idx = diff.index[cross_points][-1] if cross_points.any() else None
            if last_cross_idx is not None:
                recent_bars = (len(diff) - list(diff.index).index(last_cross_idx))
                if diff.iloc[-1] > 0:
                    ma_state = "ゴールデンクロス（短期20MAが長期200MAを上抜け）"
                elif diff.iloc[-1] < 0:
                    ma_state = "デッドクロス（短期20MAが長期200MAを下抜け）"
                else:
                    ma_state = ""
            # 時制（直近/少し前/過去）は短句に含めず、本文のマクロ側で吸収
        except Exception:
            pass

    ma_sentence = ""
    if ma_state:
        ma_sentence = f"20/200の{('ゴールデンクロス' if 'ゴールデン' in ma_state else 'デッドクロス') if ('ゴールデン' in ma_state or 'デッド' in ma_state) else '関係'}を{_axis_label_jp(ma_axis)}で確認。"

    # === RSI(14)
    rsi_state = _ss_pick(
        keys=["p2_rsi_state","rsi_state","p2_rsi_manual","rsi_manual","rsi_state_select"],
        substrings=["rsi","state"],
        default=""
    )
    if not rsi_state and st.session_state.get("p2_auto_complete", True):
        try:
            pair_label = st.session_state.get("pair", "ドル円")
            t1d, t4h = _tickers_for(pair_label)
            sym = t1d if rsi_axis == "D1" else t4h
            h1, h4, d1 = ta_block(sym, days=120)
            df = d1 if rsi_axis == "D1" else h4
            val = float(df["RSI"].iloc[-1])
            if val >= 67:   rsi_state = "70接近"
            elif val <= 33: rsi_state = "30接近"
            else:           rsi_state = "50前後"
        except Exception:
            pass
    rsi_sentence = f"RSI(14)は{rsi_state}。" if rsi_state else ""

    # === ボリンジャーバンド(20, ±2σ)
    bb_state = _ss_pick(
        keys=["p2_bb_state","bb_state","p2_bb_manual","bb_manual","bb_state_select"],
        substrings=["bb","state"],
        default=""
    )
    if not bb_state and st.session_state.get("p2_auto_complete", True):
        try:
            pair_label = st.session_state.get("pair", "ドル円")
            t1d, t4h = _tickers_for(pair_label)
            sym = t1d if bb_axis == "D1" else t4h
            h1, h4, d1 = ta_block(sym, days=120)
            df = d1 if bb_axis == "D1" else h4
            close = df["Close"].iloc[-1]; up = df["BB_up"].iloc[-1]; dn = df["BB_dn"].iloc[-1]; mid = df["SMA20"].iloc[-1]
            width = (up - dn) / mid if mid else 0.0
            pos = (close - mid) / (up - dn) if (up != dn) else 0.0
            if width < 0.01:              bb_state = "収縮（バンド幅が狭い＝ボラ低下）"
            elif pos >= 0.40:             bb_state = "上限付近（価格が上バンドに接近）"
            elif pos <= -0.40:            bb_state = "下限付近（価格が下バンドに接近）"
            else:                         bb_state = "中心線付近（価格がミドルバンド付近）"
        except Exception:
            pass
    bb_sentence = f"ボリンジャーバンド(±2σ)は{('拡大型' if '拡大' in bb_state else '収縮気味' if '収縮' in bb_state else '中心線付近' if '中心線' in bb_state else '上限付近' if '上限' in bb_state else '下限付近' if '下限' in bb_state else '観測')}（{_axis_label_jp(bb_axis)}）。" if bb_state else ""


    # ---------- 4) 重複を避けながら base に付け足す ----------
    def _append_once(text: str, add: str) -> str:
        t = (text or "").strip()
        a = (add or "").strip()
        if not a:
            return t
        # 空白を無視した包含チェックで重複を抑止
        if a.replace(" ", "") in t.replace(" ", ""):
            return t
        if t and not t.endswith("。"):
            t += "。"
        return t + a

    base = _append_once(base, ma_sentence)
    base = _append_once(base, rsi_sentence)
    base = _append_once(base, bb_sentence)

    # ---------- 5) ブレークポイント短句を1本だけ追加 ----------
    bp_sentence = ""
        # 修正: 「日足のみ」選択時はBP軸を強制D1（AUTO/H4を上書き）
    try:
        if st.session_state.get("tf_mix_mode") == "日足のみ":
            axis_now = st.session_state.get("p2_bp_axis", "AUTO")
            if str(axis_now).upper() in ("AUTO", "H4"):
                st.session_state["p2_bp_axis"] = "D1"
    except Exception:
        pass
    
            # 修正C: 「日足のみ」選択時はBP軸を強制的にD1へ（AUTO/H4を上書き）
        if st.session_state.get("tf_mix_mode") == "日足のみ":
            axis_now = st.session_state.get("p2_bp_axis", "AUTO")
            if axis_now in ("AUTO", "H4"):
                st.session_state["p2_bp_axis"] = "D1"


    base = _append_once(base, bp_sentence)

    # ---------- 6) 仕上げ：句点正規化＋簡易デデュープ ----------
    base = base.strip()
    # 連続句点を1つに
    base = re.sub(r"。{2,}", "。", base)

    # 文単位の重複削除（順序保持）
    parts = [p for p in re.split(r"。+", base) if p]
    seen = set()
    uniq = []
    for p in parts:
        key = p.replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    base = "。".join(uniq).strip() + "。"

    return _p2_merge_indicators(base)



# --- D1/H4 の数値も段落②用キーに同期（文章が読む名前へコピー） ---
st.session_state["p2_bp_d1_upper"] = st.session_state.get("bp_d1_up")
st.session_state["p2_bp_d1_lower"] = st.session_state.get("bp_d1_dn")
st.session_state["p2_bp_h4_upper"] = st.session_state.get("bp_h4_up")
st.session_state["p2_bp_h4_lower"]  = st.session_state.get("bp_h4_dn")

# ---- どの時間軸の値を本文で使うか（既存ラジオの選択を読む） ----
_choice = st.session_state.get("bp_apply_mode", "自動（基準に合わせる）")
_axis_map = {"自動（基準に合わせる）": "AUTO", "日足のみを使う": "D1", "4時間足のみを使う": "H4"}
st.session_state["p2_bp_axis"] = _axis_map.get(_choice, "AUTO")


# --- FIX: _compose_para2_preview_mix を呼ぶ前に必ず定義しておく（未定義時のみ） ---
if "_decimals_from_pair" not in globals():
    def _decimals_from_pair(pair_label: str) -> int:
        s = (pair_label or "")
        return 2 if ("JPY" in s.upper() or "円" in s) else 4

if "_first_num" not in globals():
    def _first_num(*vals):
        """先に見つかった有効な数値を返す（0以下やNaNは除外）。list/tuple/文字列/NumPyにも耐性。"""
        import math
        for v in vals:
            try:
                if v is None:
                    continue
                if isinstance(v, (list, tuple)) and v:
                    v = v[0]
                x = float(str(v).replace(",", "").strip())
                if math.isfinite(x) and x > 0:
                    return x
            except Exception:
                pass
        return None

if "_bp_sentence_mix" not in globals():
    def _bp_sentence_mix():
        """
        プレビュー用のBP文を作る。
        優先（上）: D1 -> H4 -> 共通
        優先（下）: H4 -> D1 -> 共通
        """
        mix = st.session_state.get("tf_mix_mode", "両方（半々）")

        def _allow_d1() -> bool:
            return mix in ("日足のみ", "両方（半々）")

        def _allow_h4() -> bool:
            return mix in ("4時間足のみ", "両方（半々）")

        ss = st.session_state
        up_val = _first_num(
            ss.get("p2_bp_d1_upper"), ss.get("bp_d1_up"),
            ss.get("p2_bp_h4_upper"), ss.get("bp_h4_up"),
            ss.get("p2_bp_upper"),    ss.get("bp_up"),
        )
        dn_val = _first_num(
            ss.get("p2_bp_h4_lower"), ss.get("bp_h4_dn"),
            ss.get("p2_bp_d1_lower"), ss.get("bp_d1_dn"),
            ss.get("p2_bp_lower"),    ss.get("bp_dn"),
        )

        pair = str(ss.get("pair", "") or "")
        decimals = _decimals_from_pair(pair)
        fmt = f"{{:.{decimals}f}}"

        debug = {
            "up": up_val, "dn": dn_val, "pair": pair, "decimals": decimals,
            "used_order_up":  ["p2_bp_d1_upper","bp_d1_up","p2_bp_h4_upper","bp_h4_up","p2_bp_upper","bp_up"],
            "used_order_down":["p2_bp_h4_lower","bp_h4_dn","p2_bp_d1_lower","bp_d1_dn","p2_bp_lower","bp_dn"],
        }

        if up_val is None and dn_val is None:
            return "", debug
        if up_val is not None and dn_val is not None:
            sent = f"{fmt.format(up_val)}付近の上抜け／{fmt.format(dn_val)}付近割れのどちらに傾くかを見極めたい。"
        elif up_val is not None:
            sent = f"{fmt.format(up_val)}付近の上抜けの有無をまず確かめたい。"
        else:
            sent = f"{fmt.format(dn_val)}付近割れの可否をまず確認したい。"
        return sent, debug

if "_compose_para2_preview_mix" not in globals():
    def _compose_para2_preview_mix() -> str:
        """UIプレビューの素文 + BP短句（必要なら）を返し、session_stateにも保存。"""
        # 1) UI生成のベース文
        try:
            base = _compose_para2_preview_from_ui()
        except Exception:
            pair   = str(st.session_state.get("pair", "") or "")
            d1     = st.session_state.get("d1_imp", "横ばい")
            h4     = st.session_state.get("h4_imp", "横ばい")
            tfmode = st.session_state.get("tf_mix_mode", "両方（半々）")
            market = _market_word_for(pair) if '_market_word_for' in globals() else "為替市場"

            if tfmode == "日足のみ":
                base = f"{market}は、{pair}は日足は{d1}。"
            elif tfmode == "4時間足のみ":
                base = f"{market}は、{pair}は4時間足は{h4}。"
            else:  # 両方（半々）
                base = f"{market}は、{pair}は日足は{d1}、4時間足は{h4}。"

                # 2) BP短句（数値）を必要なら付与
        bp_sentence, dbg = _bp_sentence_mix()
        if st.session_state.get("show_debug", False):
            st.write("DEBUG_BP_PICK:", dbg)
        if bp_sentence and (bp_sentence not in base):
            base = (base.rstrip("。") + "。" if base else "") + bp_sentence



# --- /FIX ---

# ---- / 段落②（プレビュー：新ロジック） ----
# --- 段落②（プレビュー：新ロジック）SOTエクスポート ---
try:
    _p2_preview = _compose_para2_preview_mix()  # ※ここはBP短句も含む“最終に近いプレビュー文”
except Exception:
    # 新ロジック: '時間軸の使い方'（tf_mix_mode）に厳密準拠
    try:
        _p2_preview = _compose_para2_base_from_state()
    except Exception:
        mix  = st.session_state.get("tf_mix_mode", "両方（半々）")
        pair = str(st.session_state.get("pair", "") or "")
        d1   = st.session_state.get("d1_imp", "横ばい")
        h4   = st.session_state.get("h4_imp", "横ばい")
        if mix == "日足のみ":
            _p2_preview = f"為替市場は、{pair}は日足は{d1}。"
        elif mix == "4時間足のみ":
            # 4時間足のみ＝短期フォーカス。ただし日足の“方向”だけは残す
            _p2_preview = f"為替市場は、{pair}は日足は{_coarse_trend(d1)}、4時間足は{h4}。"
        else:
            _p2_preview = f"為替市場は、{pair}は日足は{d1}、4時間足は{h4}。"


# 句点を1つに正規化
_p2_preview = (_p2_preview or "").strip().rstrip("。") + "。"

# ← 以降どのセクションからも“同じ文”にアクセスできるよう固定
st.session_state["p2_ui_preview_text"] = _p2_preview  # 推奨：session_state
globals()["_para2_preview"] = _p2_preview            # 互換：既存コードが見る可能性に対応

# === 段落②プレビュー（確定文をSOTに保存して、以後のSOTに統一）===
preview_text = _compose_para2_preview_mix()
# 句点を1つに正規化
preview_text = (preview_text or "").strip().rstrip("。") + "。"

# SOTへ保存（Step6/編集欄の初期値がこれを参照）
st.session_state["p2_ui_preview_text"] = preview_text
globals()["_para2_preview"] = preview_text

# 表示（読み取り専用）
st.text_area(
    "生成結果（読み取り専用・BP反映版）",
    value=preview_text,
    height=140,
    disabled=True
)


# SOTに保存（句点は1つに正規化）
st.session_state["p2_ui_preview_text"] = (preview_text or "").strip().rstrip("。") + "。"
globals()["_para2_preview"] = st.session_state["p2_ui_preview_text"]



st.radio(
    "本文に反映するブレークポイントの時間軸",
    ["自動（基準に合わせる）", "日足のみを使う", "4時間足のみを使う"],
    horizontal=True,
    key="bp_apply_mode",
    help="時間軸別に値を入れたときの反映対象。『自動』は上の『時間軸の使い方』に合わせます。未入力なら共通ブレークポイントを使います。"
)

# --- bridge: ラジオ選択を段落②用の軸キーに同期 ---
_axis_map = {"自動（基準に合わせる）": "AUTO", "日足のみを使う": "D1", "4時間足のみを使う": "H4"}
st.session_state["p2_bp_axis"] = _axis_map.get(st.session_state.get("bp_apply_mode"), "AUTO")


# ==== 文体仕上げ：句頭の連続回避 & 連続文の簡易重複除去 ====
import re

_OPENERS_ROTATION = ("短期は", "目先は", "当面は", "直近では")

def _avoid_repeated_openers(text: str) -> str:
    """
    - 隣接する同一文を除去
    - 文頭の「短期は」が連続する場合、2回目以降をローテーションで言い換え
    - 句点の直後だけでなく改行や空白を挟んだケースも検知
    """
    t = (text or "").strip().replace("。。", "。")

    # 1) 隣接する同一文の除去（句点/ピリオド/改行でざっくり分割）
    sents = [s.strip() for s in re.split(r"[。\.]\s*", t) if s.strip()]
    dedup = []
    for s in sents:
        if dedup and dedup[-1] == s:
            continue
        dedup.append(s)
    t = "。".join(dedup)
    if not t.endswith("。"):
        t += "。"

    # 2) 「短期は」の句頭連発をローテーションで置換
    idx = 0
    def repl(m):
        nonlocal idx
        idx += 1  # 1回目はそのまま、2回目以降はローテ
        word = _OPENERS_ROTATION[min(idx - 1, len(_OPENERS_ROTATION) - 1)]
        return f"{m.group(1)}{word}"

    # 句頭（行頭）/ 句点 / 改行の直後に空白を挟んでもマッチ
    pattern = re.compile(r'(^|[。\n\r])\s*短期は')
    t = pattern.sub(repl, t)

    return t.replace("。。", "。")



# 文字数ガード（180〜210字に収める）
# 文字数ガード（締めを1つだけ残しつつ、180〜210字に収める）
def _enforce_length_bounds(text: str, min_len: int = 180, max_len: int = 210) -> str:
    t = (text or "").replace("。。", "。").strip()
    if not t:
        return t
    if not t.endswith("。"):
        t += "。"

    # 文に分割
    sents = [s for s in t.split("。") if s]
    # 末尾の「締め」候補（_CLOSE_STEMS に合致）を検出
    close_idx = -1
    for i, s in enumerate(sents):
        if any(stem in s for stem in _CLOSE_STEMS):
            close_idx = i
    # 末尾の締めを最後に1つだけ残す（存在しない場合はそのまま）
    if close_idx != -1:
        closer = sents[close_idx]
        sents = [s for j, s in enumerate(sents) if not any(stem in s for stem in _CLOSE_STEMS)]
        sents.append(closer)

    def join(ss):
        out = "。".join(ss)
        if not out.endswith("。"):
            out += "。"
        return out

    # 180字未満なら、締めの"直前"にだけ安全な補助文を差し込む（最大2文）
    fillers = [
        "移動平均線周辺の反応を確かめつつ様子を見たい",
        "節目水準の手前では反応がぶれやすい",
        "指標通過後の方向性を見極めたい",
        "短期は値動きの粗さに留意したい",
    ]
    # 決定論的な開始位置（同じ日×同じペアで同じ順になり機械感を抑制）
    start = 0
    try:
        start_pick = _stable_pick(list(range(len(fillers))), category="pad")
        start = fillers.index(start_pick) if isinstance(start_pick, str) and (start_pick in fillers) else 0
    except Exception:
        start = 0

    add_cnt = 0
    while len(join(sents)) < min_len and add_cnt < 2:
        # まだ入れていない候補を順に
        cand = fillers[(start + add_cnt) % len(fillers)]
        if cand not in sents:
            # 締めの直前に差し込み（締めを押し下げる）
            if close_idx != -1 or (sents and any(stem in sents[-1] for stem in _CLOSE_STEMS)):
                sents.insert(len(sents) - 1, cand)
            else:
                sents.append(cand)
            add_cnt += 1
        else:
            add_cnt += 1  # 念のためループ前進

    out = join(sents)

    # 長すぎる場合は、締め以外の末尾文から落として調整
    if len(out) > max_len:
        trimmed = [s for s in sents]
        # 締め文を退避
        closer_final = None
        if trimmed and any(stem in trimmed[-1] for stem in _CLOSE_STEMS):
            closer_final = trimmed.pop()
        # 末尾から1文ずつ間引き
        while trimmed and len(join(trimmed + ([closer_final] if closer_final else []))) > max_len:
            trimmed.pop()
        if closer_final:
            trimmed.append(closer_final)
        out = join(trimmed) if trimmed else out[:max_len] + "。"

    return out

# ==== 語彙バリエーション（導入文専用 v1） ====
def _stable_pick(cands, category="intro"):
    """
    候補リストから『通貨ペア×日付×カテゴリ』で決定論的に1つ選ぶ。
    同じ日・同じペアなら同じ表現になる（機械感を抑えつつ再現性を確保）。
    """
    try:
        pair = str(st.session_state.get("pair", "") or "")
    except Exception:
        pair = ""
    # 日付は session_state の asof（あれば）を優先、無ければ今日
    try:
        asof = str(st.session_state.get("asof_date", "")) or __import__("datetime").date.today().isoformat()
    except Exception:
        asof = "1970-01-01"
    seed = f"{pair}|{asof}|{category}"
    import hashlib
    h = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16)
    return cands[h % len(cands)] if cands else ""

# D1/H4の組合せごとの導入文バリエーション
# キケンな断定・売買助言は一切含めません
_LEX_INTRO = {
    ("up", "up"): [
        "日足・4時間足とも上方向が優勢。",
        "日足・4時間足そろって上向きが意識される。",
        "日足・4時間足ともに上値を試しやすい。",
        "日足・4時間足で上向きが続きやすい流れ。"
    ],
    ("down", "down"): [
        "日足・4時間足とも下方向が優勢。",
        "日足・4時間足そろって下押しが意識される。",
        "日足・4時間足ともに戻り売りが入りやすい。",
        "日足・4時間足で下向きが続きやすい流れ。"
    ],
    ("flat", "flat"): [
        "日足・4時間足ともレンジ色が濃い。",
        "日足・4時間足とも方向感が限定的。",
        "日足・4時間足とも様子見の展開。",
        "日足・4時間足とも見極め待ちの局面。"
    ],
    ("up", "flat"): [
        "日足は上向きが意識されやすい一方、4時間足はもみ合い。",
        "日足は持ち直し基調、4時間足はレンジ気味。",
        "日足は上向きが意識される半面、4時間足は方向感が乏しい。"
    ],
    ("down", "flat"): [
        "日足は下押しが意識されやすい一方、4時間足はもみ合い。",
        "日足は上値の重さが意識される半面、4時間足はレンジ気味。",
        "日足は戻り売りが入りやすいなか、4時間足は方向感が乏しい。"
    ],
    ("flat", "up"): [
        "日足は方向感を探る局面、4時間足は上向きを試す。",
        "日足は見極め待ち、4時間足は上値トライが入りやすい。",
        "日足はレンジ気味、4時間足は上方向がやや優勢。"
    ],
    ("flat", "down"): [
        "日足は方向感を探る局面、4時間足は下押しを試す。",
        "日足は見極め待ち、4時間足は下方向がやや優勢。",
        "日足はレンジ気味、4時間足は戻り売りが入りやすい。"
    ],
    ("up", "down"): [
        "日足は上向きが意識されやすいなか、4時間足は戻り売りが入りやすい。",
        "日足は上向き基調、4時間足は戻りを抑えられやすい。",
        "日足は上方向、4時間足は重さが残る。"
    ],
    ("down", "up"): [
        "日足は下押しが意識されやすいなか、4時間足は戻りを試す動き。",
        "日足は重さが意識される一方、4時間足は持ち直しを試す。",
        "日足は下方向、4時間足は戻りが入りやすい。"
    ],
}

# しつこさを取る整形：重複文を除去し、締め文は最大1つ、文数は最大4文に制限
_CLOSE_STEMS = ["反応を確かめたい", "行方を見極めたい", "値動きには警戒したい", "過度な一方向は決めつけにくい"]

def _tidy_para2(text: str, max_sents: int = 4) -> str:
    t = (text or "").replace("。。", "。").strip()
    if not t:
        return t

    # 文に分割して順序を保ったまま重複排除
    sents = [s.strip() for s in t.split("。") if s.strip()]
    seen = set()
    ordered = []
    for s in sents:
        if s not in seen:
            seen.add(s)
            ordered.append(s)

    # 締め文（_CLOSE_STEMS を含む文）はいったん除去し、最後に1つだけ残す
    closer_buf = [s for s in ordered if any(stem in s for stem in _CLOSE_STEMS)]
    body = [s for s in ordered if not any(stem in s for stem in _CLOSE_STEMS)]
    if closer_buf:
        body.append(closer_buf[-1])  # 最後に選ばれた締めだけ残す

    # 文数の上限
    if len(body) > max_sents:
        body = body[:max_sents]

    out = "。".join(body)
    if not out.endswith("。"):
        out += "。"
    return out

# ==== 為替市場リード文（FXは「為替市場は、…」/ BTC・金は従来） ====

def _is_crypto_or_gold(pair_label: str) -> bool:
    pl = (pair_label or "")
    plu = pl.upper()
    return (
        ("ビットコイン" in pl) or ("仮想通貨" in pl) or ("暗号" in pl) or
        ("BTC" in plu) or
        ("金" in pl) or ("ゴールド" in pl) or ("XAU" in plu) or ("GOLD" in plu)
    )

# D1/H4組み合わせ別：FX向けの「為替市場は、〜」のリード候補
_LEX_LEAD = {
    ("up", "up"): [
        "為替市場は、{PAIR}は上方向が優勢。",
        "為替市場は、{PAIR}は上向きが意識される。",
    ],
    ("down", "down"): [
        "為替市場は、{PAIR}は下方向が優勢。",
        "為替市場は、{PAIR}は下押しが意識される。",
    ],
    ("flat", "flat"): [
        "為替市場は、{PAIR}は方向感が限定的。",
        "為替市場は、{PAIR}はレンジ色が濃い。",
    ],
    ("up", "flat"): [
        "為替市場は、{PAIR}は上向き基調ながら短期はもみ合い。",
    ],
    ("down", "flat"): [
        "為替市場は、{PAIR}は上値が重い一方で短期はもみ合い。",
    ],
    ("flat", "up"): [
        "為替市場は、{PAIR}は方向感探りつつ短期は上向き。",
    ],
    ("flat", "down"): [
        "為替市場は、{PAIR}は方向感探りつつ短期は下押し。",
    ],
    ("up", "down"): [
        "為替市場は、{PAIR}は上値試しと戻り売りが交錯。",
    ],
    ("down", "up"): [
        "為替市場は、{PAIR}は下押しのなか戻りを試す局面。",
    ],
}

# ==== 語彙バリエーション：RSI（決定論ローテーション） ====
_LEX_RSI = {
    "70+": [
        "RSIは70超で過熱感に留意",
        "RSIは70台で短期の一服に注意",
        "RSIは高止まりで逆方向の揺り戻しに警戒",
    ],
    "60-70": [
        "RSIは60台で上向きバイアス",
        "RSIは60台に乗せ気味で上値を試しやすい",
        "RSIはやや強めの推移",
    ],
    "50": [
        "RSIは50近辺で中立",
        "RSIは50どころで方向感は限定的",
        "RSIはニュートラル圏で様子見",
    ],
    "40-50": [
        "RSIは40〜50台で下向きバイアス",
        "RSIはやや弱めの推移",
        "RSIは戻りが抑えられやすい",
    ],
    "30-40": [
        "RSIは30〜40台で売られ気味",
        "RSIは低下気味で戻りは鈍い",
        "RSIは弱含みで上値の重さ",
    ],
    "30-": [
        "RSIは30割れで売られすぎ気味",
        "RSIは極端な低位で過熱感に留意",
        "RSIは行き過ぎ感もあり反動には注意",
    ],
    "div": [
        "RSIのダイバージェンスも意識したい",
        "RSIが価格と逆行する兆しに留意",
        "RSIの逆行シグナルは参考材料",
    ],
}
# ==== 語彙バリエーション：ボリンジャーバンド（決定論ローテーション） ====
_LEX_BB = {
    "上限付近": [
        "ボリンジャーバンド上限付近は勢いの確認材料",
        "上バンド接近は伸びの持続性を測る材料",
        "上側のバンドタッチは強さの裏付けに"
    ],
    "下限付近": [
        "ボリンジャーバンド下限付近は勢いの確認材料",
        "下バンド接近は下押しの強さの測りどころ",
        "下側のバンドタッチは弱さの裏付けに"
    ],
    "中心線付近": [
        "ボリンジャーバンドの中心線付近で基調を見極めたい",
        "ミドルバンド付近で傾きの確認をしたい",
        "中心線どころの反応をまず確かめたい"
    ],
    "収縮": [
        "ボリンジャーバンドの収縮は次の振れに注意",
        "バンドの収縮は方向性の出直しに備えたい",
        "収縮局面はブレークの方向を見極めたい"
    ],
    "拡大": [
        "ボリンジャーバンドの拡大はボラ上昇のサイン",
        "バンド幅の拡大は値動きの荒さに留意",
        "拡大型は一方向の行き過ぎに注意したい"
    ],
    "上方向のバンドウォーク": [
        "ボリンジャーバンドの上方向バンドウォークが続くかを確認",
        "上バンド沿いの推移が続くか注視",
        "上側へ張り付く動きの持続性を見極めたい"
    ],
    "下方向のバンドウォーク": [
        "ボリンジャーバンドの下方向バンドウォークが続くかを確認",
        "下バンド沿いの推移が続くか注視",
        "下側へ張り付く動きの持続性を見極めたい"
    ],
}

# ==== 語彙バリエーション：ゴールデンクロス／デッドクロス（決定論ローテーション） ====
_LEX_GC = {
    "D1": {
        "GC": [
            "日足では20SMAが200SMAを上回り上向き基調",
            "日足では短期20SMAが長期200SMAを上抜けるゴールデンクロス後の推移",
            "日足はゴールデンクロス後の地合い"
        ],
        "DC": [
            "日足では20SMAが200SMAを下回り下向き基調",
            "日足では短期20SMAが長期200SMAを下抜けるデッドクロス後の推移",
            "日足はデッドクロス後の地合い"
        ],
    },
    "H4": {
        "GC": [
            "4時間足では20SMAが200SMAを上回り上向き基調",
            "4時間足では短期20SMAが長期200SMAを上抜けるゴールデンクロス後の推移",
            "4時間足はゴールデンクロス後の地合い"
        ],
        "DC": [
            "4時間足では20SMAが200SMAを下回り下向き基調",
            "4時間足では短期20SMAが長期200SMAを下抜けるデッドクロス後の推移",
            "4時間足はデッドクロス後の地合い"
        ],
    },
    "GENERIC": {
        "GC": [
            "20SMAが200SMAを上回り上向き基調",
            "短期20SMAが長期200SMAを上抜けるゴールデンクロス後の推移",
        ],
        "DC": [
            "20SMAが200SMAを下回り下向き基調",
            "短期20SMAが長期200SMAを下抜けるデッドクロス後の推移",
        ],
    },
}

# ==== 語彙バリエーション：ブレークポイント（決定論ローテーション） ====
_LEX_BP = {
    "UP_ONLY": [
        "上値{U}の上抜けを試す動きには留意したい",
        "{U}の上抜けなら基調と整合的とみたい",
        "{U}突破の反応をまず確かめたい",
        "{U}をしっかり上抜けられるか注視したい",
    ],
    "DN_ONLY": [
        "下値{L}の下抜けには警戒したい",
        "{L}割れの可否をまず確認したい",
        "{L}を維持できるかに留意したい",
        "{L}の割れなら下押しが強まりやすい",
    ],
    "BOTH_NEUTRAL": [
        "{U}の上抜け／{L}の割れのいずれに振れるか反応を確かめたい",
        "まずは{U}の上抜けと{L}割れのどちらに傾くか見極めたい",
        "{U}突破か{L}割れか、方向感の手掛かりを探りたい",
    ],
    "BOTH_UP_BIAS": [
        "{U}の上抜けを試す動きに注目したい（下は{L}を意識）",
        "基調に沿えば{U}上抜けの確認が焦点（{L}は下支えの目安）",
    ],
    "BOTH_DOWN_BIAS": [
        "{L}の割れに警戒したい（上は{U}で戻りの強さを測りたい）",
        "下方向に傾くなら{L}割れの可否が焦点（{U}は上値の目安）",
    ],
}

# ==== 時間軸配分：単一時間軸用の導入文生成（既存のままでOKならそのまま） ====
def _intro_single_frame(tf_label: str, imp: str) -> str:
    if not isinstance(imp, str):
        imp = "横ばい"
    if "横ばい" in imp:
        return f"{tf_label}はもみ合い"
    if "強いアップ" in imp or ("アップ" in imp and "緩やか" not in imp):
        return f"{tf_label}は上方向が優勢"
    if "緩やかなアップ" in imp:
        return f"{tf_label}はじり高基調"
    if "強いダウン" in imp or ("ダウン" in imp and "緩やか" not in imp):
        return f"{tf_label}は下方向が優勢"
    if "緩やかなダウン" in imp:
        return f"{tf_label}はじり安基調"
    return f"{tf_label}は方向感が限定的"

# ==== 導入文（配分反映版）: H4のみでもD1を必ず含める ====
def _intro_from_impressions_weighted(d1_imp: str, h4_imp: str, tf_mix_mode: str) -> str:
    # 日足のみ → 日足だけ
    if tf_mix_mode == "日足のみ":
        return _intro_single_frame("日足", d1_imp)
    # 4時間足のみ → 「日足〜一方、4時間足〜」の複合（=必ずD1を含める）
    if tf_mix_mode == "4時間足のみ":
        return _intro_from_impressions(d1_imp, h4_imp)
    # 両方（半々） → 従来の複合導入
    return _intro_from_impressions(d1_imp, h4_imp)

# ==== リード文（配分反映版）: 為替は「為替市場は、…」を維持 ====
def _lead_sentence_weighted(pair_label: str, d1_imp: str, h4_imp: str, tf_mix_mode: str) -> str:
    if _is_crypto_or_gold(pair_label):
        return f"{pair_label}のテクニカルでは、"

    def _pick(cands, key):
        return _stable_pick(cands, category=key)

    if tf_mix_mode == "日足のみ":
        cat = _cat_trend(d1_imp)
        table = {
            "up":   ["為替市場は、{PAIR}は日足で上向きが意識される。", "為替市場は、{PAIR}は日足で上方向が優勢。"],
            "down": ["為替市場は、{PAIR}は日足で下押しが意識される。", "為替市場は、{PAIR}は日足で下方向が優勢。"],
            "flat": ["為替市場は、{PAIR}は日足で方向感が限定的。",     "為替市場は、{PAIR}は日足でレンジ色が濃い。"],
        }
        templ = _pick(table.get(cat, table["flat"]), f"lead:D1:{cat}")
        return templ.replace("{PAIR}", pair_label or "主要通貨")

    if tf_mix_mode == "4時間足のみ":
        # リードは短期フォーカス、導入でD1+H4の複合を続けて補完
        cat = _cat_trend(h4_imp)
        table = {
            "up":   ["為替市場は、{PAIR}は短期は上向きが意識される。", "為替市場は、{PAIR}は短期は上方向が優勢。"],
            "down": ["為替市場は、{PAIR}は短期は下押しが意識される。", "為替市場は、{PAIR}は短期は下方向が優勢。"],
            "flat": ["為替市場は、{PAIR}は短期は方向感が限定的。",     "為替市場は、{PAIR}は短期はレンジ色が濃い。"],
        }
        templ = _pick(table.get(cat, table["flat"]), f"lead:H4:{cat}")
        return templ.replace("{PAIR}", pair_label or "主要通貨")

    # 両方（半々）
    return _lead_sentence(pair_label, d1_imp, h4_imp)



def _trend_bias(d1_imp: str, h4_imp: str) -> str:
    """D1/H4の印象から上げ/下げ/中立のバイアスを判定（超安全版）"""
    def sc(x: str) -> int:
        if not isinstance(x, str):
            return 0
        if "アップ" in x:  # 強い/緩やか含む
            return 1
        if "ダウン" in x:
            return -1
        return 0
    s = sc(d1_imp) + sc(h4_imp)
    if s > 0:
        return "up"
    if s < 0:
        return "down"
    return "neutral"

def _bp_phrase(up_txt, dn_txt, pair_label=None, bias_tag=None) -> str:
    """
    ブレークポイント文を固定形に統一（語彙ローテーション/括弧付き文を廃止）。
    - 両方あり:  "{U}付近の上抜け／{L}付近割れのどちらに傾くかを見極めたい。"
    - 上だけ:    "{U}付近の上抜けの有無をまず確かめたい。"
    - 下だけ:    "{L}付近割れの可否をまず確認したい。"
    - どちらも無: 空文字
    """
    u = (up_txt.strip() if isinstance(up_txt, str) and up_txt else None)
    d = (dn_txt.strip() if isinstance(dn_txt, str) and dn_txt else None)

    if u and d:
        return f"{u}付近の上抜け／{d}付近割れのどちらに傾くかを見極めたい。"
    if u:
        return f"{u}付近の上抜けの有無をまず確かめたい。"
    if d:
        return f"{d}付近割れの可否をまず確認したい。"
    return ""



def _lead_sentence(pair_label: str, d1_imp: str, h4_imp: str) -> str:
    # BTC・金は従来の導入
    if _is_crypto_or_gold(pair_label):
        return f"{pair_label}のテクニカルでは、"

    # FXは「為替市場は、…」で簡潔に
    key = (_cat_trend(d1_imp), _cat_trend(h4_imp))
    cands = _LEX_LEAD.get(key) or ["為替市場は、{PAIR}は方向感が限定的。"]
    templ = _stable_pick(cands, category=f"lead:{key[0]}-{key[1]}")
    return templ.replace("{PAIR}", pair_label or "主要通貨")

    # ==== 文体仕上げ：句頭の連続回避 & 連続文の簡易重複除去 ====
import re

def _avoid_repeated_openers(text: str) -> str:
    """
    - 同一文の連続重複を除去
    - 「短期は」が文頭で連続する場合、2回目以降を「目先は」「当面は」「直近では」に置換
    """
    t = (text or "").strip().replace("。。", "。")

    # 1) 隣接する同一文を除去
    sents = [s for s in t.split("。") if s != ""]
    dedup = []
    for s in sents:
        if dedup and dedup[-1].strip() == s.strip():
            continue
        dedup.append(s)
    t = "。".join(dedup)
    if not t.endswith("。"):
        t += "。"

    # 2) 「短期は」の連発を言い換え
    pattern = re.compile(r'(^|。)短期は')
    idx = 0
    def repl(m):
        nonlocal idx
        idx += 1
        if idx == 1:
            word = "短期は"
        elif idx == 2:
            word = "目先は"
        elif idx == 3:
            word = "当面は"
        else:
            word = "直近では"
        return f"{m.group(1)}{word}"
    t = pattern.sub(repl, t)

    return t


# ==== 段落② 最終整形（for_build 同期用） ====
def _finalize_para2_for_build(text: str) -> str:
    """
    段落②を公開体裁に合わせて最終整形する。
    手順:
    1) 事前整形（重複/句点整理）
    2) 句頭連発の言い換え（「短期は」→2回目以降はローテ）
    3) 規定文字数(180–210)に収める
    4) 仕上げの整形
    """
    tmp = _tidy_para2((text or "").strip())        # 1) 下ごしらえ
    tmp = _avoid_repeated_openers(tmp)             # 2) 句頭言い換え ★重要
    tmp = _enforce_length_bounds(tmp, 180, 210)    # 3) 文字数ガード
    text = text.replace("。。", "。").replace("。 、", "。")

    return _tidy_para2(tmp)                        # 4) 最終仕上げ


# ==== / 段落② 最終整形 =====================================================

# ==== 整合性バッジ（UIのみ表示／本文には出さない） ====
def _trend_sign_from_label(label: str) -> int:
    s = str(label or "")
    if "アップ" in s: return 1
    if "ダウン" in s: return -1
    return 0

def _cross_sign(gc_state: str) -> int | None:
    s = str(gc_state or "")
    if "ゴールデンクロス" in s: return 1
    if "デッドクロス" in s:   return -1
    return None

def _consistency_judge(d1_imp: str, h4_imp: str, gc_axis: str, gc_state: str) -> tuple[str, str]:
    ax = str(gc_axis or "")
    cs = _cross_sign(gc_state)
    if cs is None or ax == "未選択":
        return ("", "")
    if "日足" in ax:
        ts, axis_name = _trend_sign_from_label(d1_imp), "日足"
    elif "4" in ax:
        ts, axis_name = _trend_sign_from_label(h4_imp), "4時間足"
    else:
        return ("", "")
    if ts == 0:
        return ("🟡 整合性：参考", f"{axis_name}の印象が横ばいのため、クロス方向は参考扱い。")
    if ts == cs:
        return ("🟢 整合性：良好", f"{axis_name}の印象とクロス方向は整合的。")
    return ("🟠 整合性：注意", f"{axis_name}の印象とクロス方向が逆方向です。本文には書かずUIで注意表示。")


# ==== 整合性ジャッジ（人の印象 × 20/200の向き）====
def _consistency_judge(d1_imp: str, h4_imp: str, gc_axis: str, gc_state: str):
    """
    戻り値: (badge_md:str, tip_md:str) どちらか空文字なら非表示
    - 人の印象（D1/H4）と、選択された時間軸のGC/DCの“向き”が矛盾していないかを軽く表示
    - 本文には一切含めない（UIのみに表示）
    """
    def _norm_imp(x: str) -> str:
        if not isinstance(x, str): return "flat"
        if "横ばい" in x: return "flat"
        if "アップ" in x: return "up"
        if "ダウン" in x: return "down"
        return "flat"

    def _cross_dir(gc_state: str) -> str | None:
        if not isinstance(gc_state, str): return None
        if "ゴールデンクロス" in gc_state: return "up"
        if "デッドクロス"  in gc_state: return "down"
        return None

    axis = (gc_axis or "未選択")
    cdir = _cross_dir(gc_state or "未選択")
    if axis == "未選択" or cdir is None:
        return "", ""  # 比較材料なし → 非表示

    d1 = _norm_imp(d1_imp or "横ばい")
    h4 = _norm_imp(h4_imp or "横ばい")
    imp = d1 if ("日足" in axis) else (h4 if "4" in axis else None)
    if imp is None:
        return "", ""

    if imp == "flat":
        badge = "**🟡 整合性△**"
        tip   = "人の印象は『横ばい』、一方で20/200は方向性が示唆。強くは踏み込まず“確認優先”が無難です。"
    elif imp == cdir:
        badge = "**🟢 整合性◯**"
        tip   = "人の印象と20/200の基調がおおむね整合。断定は避けつつも、流れの確認がしやすい局面です。"
    else:
        badge = "**🟠 整合性⚠**"
        tip   = "人の印象と20/200の基調が逆行気味。本文は人の選択を優先しつつ、過度な決めつけを避けたいところ。"

    return badge, tip



# ==== NEW: 段落②NEW: 段落②（プレビュー：新ロジック==================================

def _fmt_price(val: float, decimals: int) -> str:
    try:
        if val is None or val <= 0:
            return ""
        return f"{val:.{decimals}f}"
    except Exception:
        return ""

def _detect_decimals_from_pair(pair_label: str) -> int:
    return 2 if (("JPY" in pair_label) or ("円" in pair_label)) else 4

def _cat_trend(x: str) -> str:
    if not isinstance(x, str):
        return "flat"
    if "横ばい" in x:
        return "flat"
    if "アップ" in x:
        return "up"
    if "ダウン" in x:
        return "down"
    return "flat"

def _intro_from_impressions(d1_imp: str, h4_imp: str) -> str:
    def _cat(x: str) -> str:
        if not isinstance(x, str): return "flat"
        if "横ばい" in x: return "flat"
        if "アップ" in x: return "up"
        if "ダウン" in x: return "down"
        return "flat"

    key = (_cat(d1_imp), _cat(h4_imp))
    cands = _LEX_INTRO.get(key)
    if not cands:
        # フォールバック（万一辞書漏れがあっても安全）
        if key == ("flat","flat"): return "日足・4時間足ともレンジ色が濃い。"
        if key == ("up","up"):     return "日足・4時間足とも上方向が優勢。"
        if key == ("down","down"): return "日足・4時間足とも下方向が優勢。"
        return "日足と4時間足で見方が分かれる。"
    return _stable_pick(cands, category=f"intro:{key[0]}-{key[1]}")


def _gc_phrase(gc_axis: str, gc_state: str) -> str:
    """
    gc_axis: UIの「時間軸」ラベル（例：'日足', '4時間足', '未選択'）
    gc_state: UIの「状態」ラベル（例：'ゴールデンクロス（短期20MAが長期200MAを上抜け）', 'デッドクロス（…）', '未選択'）
    戻り値は句点なしの短句（組み立て側で「。」を付与）
    """
    axis = (gc_axis or "").strip()
    state = (gc_state or "").strip()
    if not axis or axis == "未選択" or not state or state == "未選択":
        return ""

    # 軸の正規化
    ax_key = "D1" if "日足" in axis else ("H4" if "4" in axis else "GENERIC")

    # 状態の正規化
    if "ゴールデンクロス" in state:
        st_key = "GC"
    elif "デッドクロス" in state:
        st_key = "DC"
    else:
        return ""

    # 候補の取得（軸別 → 汎用の順でフォールバック）
    cands = _LEX_GC.get(ax_key, {}).get(st_key) or _LEX_GC.get("GENERIC", {}).get(st_key, [])
    if not cands:
        return ""

    return _stable_pick(cands, category=f"gc:{ax_key}:{st_key}")



# ==== ボリンジャーバンド：文言ローテ（トレンド/レンジで使い分け） ====
def _bb_phrase(bb_state: str, d1_imp: str, h4_imp: str) -> str:
    """
    bb_state: UIの『状態（ボリンジャーバンド(20, ±2σ)）』の文字列（例：'拡大', '縮小', '未選択' など）
    d1_imp, h4_imp: 印象（'アップ' / 'ダウン' / '横ばい' など）
    戻り値：句点なしの短句（組み立て側で句点付与）

    ルール：
    - D1/H4のどちらかにトレンド（アップ/ダウン）があれば『勢い/サイン』系を優先
    - D1/H4の両方が横ばい（＝完全レンジ）のときは『振れ/荒さに留意』系を優先
    - '縮小' は共通の穏当表現
    """
    state = (bb_state or "").strip()
    if not state or state == "未選択":
        return ""

    # 印象の簡易カテゴリ化（既存の _cat_trend を利用）
    d1_cat = _cat_trend(d1_imp)   # 'up' / 'down' / 'flat'
    h4_cat = _cat_trend(h4_imp)

    trending = (d1_cat in ("up", "down")) or (h4_cat in ("up", "down"))
    full_range = (d1_cat == "flat") and (h4_cat == "flat")

    if "拡大" in state:
        if trending:
            # トレンドがある → 前向き（勢い/サイン）系
            cands = [
                "ボリンジャーバンドの拡大は勢いの確認材料",
                "ボリンジャーバンドの拡大はボラ上昇のサイン",
            ]
            return _stable_pick(cands, category="bb:expand:trend")
        elif full_range:
            # 完全レンジ → 慎重（振れ/荒さ）系
            cands = [
                "ボリンジャーバンドの拡大は振れが出やすい",
                "バンド拡大は値動きの荒さに留意",
            ]
            return _stable_pick(cands, category="bb:expand:range")
        else:
            # 混在（例：D1＝flat/H4＝flat以外）→ 中立寄り（勢い/サイン と 留意 表現の中庸）
            cands = [
                "ボリンジャーバンドの拡大はボラ上昇のサイン",
                "バンド幅の拡大は値動きの振れに注意",
            ]
            return _stable_pick(cands, category="bb:expand:mixed")

    if "縮小" in state or "収れん" in state:
        cands = [
            "ボリンジャーバンドの縮小はエネルギー蓄積のサイン",
            "バンドの収れんは様子見の局面",
        ]
        return _stable_pick(cands, category="bb:contract")

    # その他（明示されない状態）は出力なし
    return ""





def _rsi_phrase(rsi_state: str) -> str:
    s = rsi_state or "未選択"
    if s == "未選択":
        return ""

    # 状態→カテゴリ判定
    key = None
    if "70以上" in s:
        key = "70+"
    elif "60〜70" in s:
        key = "60-70"
    elif "50前後" in s or "中立" in s:
        key = "50"
    elif "40〜50" in s:
        key = "40-50"
    elif "30〜40" in s:
        key = "30-40"
    elif "30未満" in s:
        key = "30-"
    elif "ダイバージェンス" in s:
        key = "div"

    if not key:
        return ""

    cands = _LEX_RSI.get(key, [])
    return _stable_pick(cands, category=f"rsi:{key}") if cands else ""

# ==== RSI未選択時の自動補完（語尾ローテ対応） ====
_RSI_AUTO_POOL = [
    "RSIは50近辺で中立",
    "RSIは50前後で中立",
    "RSIは概ね中立圏",
]

def _rsi_with_auto(rsi_state: str, d1_imp: str, h4_imp: str) -> str:
    """
    rsi_state が '未選択' なら、中立系の定型句をローテで返す。
    既に選択されている場合は従来の _rsi_phrase の結果を返す。
    """
    s = (rsi_state or "").strip()
    if not s or s == "未選択":
        # ※必要なら D1/H4 を見て強含み/弱含みも拡張可能だが、まずは中立系で安定運用
        return _stable_pick(_RSI_AUTO_POOL, category="rsi:auto")
    return _rsi_phrase(s)


def _choose_breakpoints():
    """
    UIのブレークポイント入力を吸い上げて、(up_txt, dn_txt, axis) を返す。
    - 軸: AUTO / D1 / H4（st.session_state["p2_bp_axis"] から読む）
    - 値: 共通（p2_bp_upper/lower）, D1専用, H4専用 を状況に応じて選ぶ
    - 上下とも、優先順は axis に厳密に揃える（不整合防止）
    """
    ss = st.session_state

    def to_num(x):
        try:
            x = float(str(x).strip())
            return x if x > 0 else None
        except Exception:
            return None

    axis = ss.get("p2_bp_axis", "AUTO")  # "AUTO" / "D1" / "H4"

    # 共通欄
    com_up = to_num(ss.get("p2_bp_upper"))
    com_dn = to_num(ss.get("p2_bp_lower"))

    # D1専用
    d1_up = to_num(ss.get("p2_bp_d1_upper"))
    d1_dn = to_num(ss.get("p2_bp_d1_lower"))

    # H4専用
    h4_up = to_num(ss.get("p2_bp_h4_upper"))
    h4_dn = to_num(ss.get("p2_bp_h4_lower"))

    # 旧ロジック由来の補助（存在しなければ None）
    vals = {
        "bp_up":      to_num(ss.get("bp_up")),
        "bp_dn":      to_num(ss.get("bp_dn")),
        "bp_d1_up":   to_num(ss.get("bp_d1_up")),
        "bp_d1_dn":   to_num(ss.get("bp_d1_dn")),
        "bp_h4_up":   to_num(ss.get("bp_h4_up")),
        "bp_h4_dn":   to_num(ss.get("bp_h4_dn")),
        "p2_bp_upper":    com_up,
        "p2_bp_lower":    com_dn,
        "p2_bp_d1_upper": d1_up,
        "p2_bp_d1_lower": d1_dn,
        "p2_bp_h4_upper": h4_up,
        "p2_bp_h4_lower": h4_dn,
    }

    def order(axis: str, kind: str):  # kind: "upper" or "lower"
        if axis == "D1":
            return [f"p2_bp_d1_{kind}", f"bp_d1_{'up' if kind=='upper' else 'dn'}",
                    f"p2_bp_h4_{kind}", f"bp_h4_{'up' if kind=='upper' else 'dn'}",
                    f"p2_bp_{'upper' if kind=='upper' else 'lower'}", f"bp_{'up' if kind=='upper' else 'dn'}"]
        elif axis == "H4":
            return [f"p2_bp_h4_{kind}", f"bp_h4_{'up' if kind=='upper' else 'dn'}",
                    f"p2_bp_d1_{kind}", f"bp_d1_{'up' if kind=='upper' else 'dn'}",
                    f"p2_bp_{'upper' if kind=='upper' else 'lower'}", f"bp_{'up' if kind=='upper' else 'dn'}"]
        else:  # AUTO
            return [f"p2_bp_d1_{kind}", f"bp_d1_{'up' if kind=='upper' else 'dn'}",
                    f"p2_bp_h4_{kind}", f"bp_h4_{'up' if kind=='upper' else 'dn'}",
                    f"p2_bp_{'upper' if kind=='upper' else 'lower'}", f"bp_{'up' if kind=='upper' else 'dn'}"]

    def pick(keys):
        for k in keys:
            v = vals.get(k)
            if v is not None:
                return v
        return None

    up_num = pick(order(axis, "upper"))
    dn_num = pick(order(axis, "lower"))

    # JPY系は小数2桁、それ以外は4桁
    pair_label = (ss.get("pair") or "")
    decimals = 2 if ("JPY" in pair_label.upper() or "円" in pair_label) else 4
    fmt = f"{{:.{decimals}f}}"

    up_txt = fmt.format(up_num) if up_num is not None else None
    dn_txt = fmt.format(dn_num) if dn_num is not None else None

    # （必要なら）デバッグ表示の整合性もここで揃えられます
    ss["__bp_debug_used_order_up__"] = order(axis, "upper")
    ss["__bp_debug_used_order_down__"] = order(axis, "lower")

    return up_txt, dn_txt, axis


# ==== 結び語尾：状況に応じた安全ローテ ====
def _closing_sentence(d1_imp: str, h4_imp: str) -> str:
    """
    句点なしで返す（呼び出し側で句点を付与）
    - D1/H4とも横ばい: 決めつけ回避寄り
    - D1/H4が逆方向（どちらもトレンド）: 警戒・反応確認寄り
    - それ以外: 中立〜注視寄り
    """
    def _cat(x: str) -> str:
        s = str(x or "")
        if "横ばい" in s:
            return "flat"
        if "アップ" in s:
            return "up"
        if "ダウン" in s:
            return "down"
        return "flat"

    d1c = _cat(d1_imp)
    h4c = _cat(h4_imp)

    # 候補セットの選択
    if d1c == "flat" and h4c == "flat":
        cands = [
            "過度な一方向は決めつけにくい",
            "様子を見極めたい",
            "反応を確かめたい",
        ]
        category = "close:flat-flat"
    elif d1c != h4c and ("flat" not in (d1c, h4c)):
        # 互いに逆方向（どちらもトレンド）
        cands = [
            "いずれの方向にも振れやすい",
            "値動きには警戒したい",
            "反応を確かめたい",
        ]
        category = f"close:conflict:{d1c}-{h4c}"
    else:
        # 片方トレンド or 方向感はあるが強すぎない
        cands = [
            "行方を注視したい",
            "方向感を見極めたい",
            "過度な一方向は決めつけにくい",
        ]
        category = f"close:normal:{d1c}-{h4c}"

    # 安定ローテ（存在しなければ先頭採用）
    try:
        return _stable_pick(cands, category=category)
    except Exception:
        return cands[0]

# ==== 長さ下限フィラー（安全文のみ、重複回避） ====
def _pad_to_min_length(text: str, min_len: int = 180) -> str:
    """
    - 180字未満のときだけ、短い中立フレーズを重複なしで末尾に挿入
    - 余計な重複や句点の連続を避ける
    """
    base = (text or "").strip().replace("。。", "。")
    fillers = [
        "反応を確かめたい。",
        "行方を注視したい。",
        "方向感を見極めたい。",
        "値動きには警戒したい。",
        "20SMAやボリンジャーバンド周辺の反応を確かめたい。",
    ]
    # 既出の文は入れない
    for f in fillers:
        if len(base) >= min_len:
            break
        if f.rstrip("。") not in base:
            if not base.endswith("。"):
                base += "。"
            base += f
            base = base.replace("。。", "。").strip()
    return base

# ==== PREVIEW用・不足分を安全に埋める最小ヘルパー（180字保証＋BPを優先反映） ====
def _pad_para2_to_min(text: str, min_len: int = None) -> str:
    t = (text or "").strip().replace("。。", "。")
    if min_len is None:
        min_len = int(CFG.get("text_guards", {}).get("p2_min_chars", 180))

    d1_imp = st.session_state.get("d1_imp", "横ばい")
    h4_imp = st.session_state.get("h4_imp", "横ばい")
    bb_state = st.session_state.get("bb_state", "未選択")
    try:
        up_txt, dn_txt, _axis = _choose_breakpoints()
    except Exception:
        up_txt, dn_txt = ("", "")

    fillers: list[str] = []

    # --- 0) ブレークポイントを“最優先”で反映（重複は回避） ---
    bp_candidates = []
    if up_txt and up_txt != "0.00":
        bp_candidates.append(f"上値{up_txt}付近の上抜けの有無をまずは見極めたい。")
    if dn_txt and dn_txt != "0.00":
        bp_candidates.append(f"下値{dn_txt}付近の下抜けには警戒したい。")
    for bp in bp_candidates:
        if bp and bp not in t:
            if not t.endswith("。") and t != "":
                t += "。"
            t += bp
            t = t.replace("。。", "。")

    # --- 1) レンジ補足（なければ） ---
    if "レンジ" not in t and "持ち合い" not in t:
        fillers.append("短期は持ち合い（レンジ）を前提とした値動きが意識されやすい。")

    # --- 2) BB補足（UIで選択済みなら本文に無ければ） ---
    if bb_state and bb_state != "未選択" and "ボリンジャーバンド" not in t:
        if "拡大" in bb_state:
            fillers.append("ボリンジャーバンド(20, ±2σ)の拡大はボラ上昇のサイン。")
        elif "縮小" in bb_state:
            fillers.append("ボリンジャーバンド(20, ±2σ)の縮小は値動きの収れんを示唆。")

    # --- 3) SMA補強（足りなければ） ---
    if "20SMA" not in t and "200SMA" not in t:
        fillers.append("20SMAや200SMAとの位置関係を確認しつつ、過度な方向感は決めつけない構えとしたい。")

    # 必要分だけ追加して 180 字を必ず確保
    for add in fillers:
        if len(t) >= min_len:
            break
        if add not in t:
            if not t.endswith("。") and t != "":
                t += "。"
            t += add
            t = t.replace("。。", "。")

    return t

# ==== 根拠句の取り合わせ最適化（RSIが中立ならBBを優先） ====
def _choose_grounds_sentences(gc: str, rsi: str, bb: str) -> list[str]:
    """
    最大2句を返す。優先度は以下の通り：
    - まずGCがあれば採用
    - RSIとBBの両方がある場合、RSIが『中立』系ならBBを優先、それ以外はRSIを優先
    - どちらか一方ならその一方
    """
    picks: list[str] = []
    if gc:
        picks.append(gc)

    has_rsi = bool(rsi)
    has_bb  = bool(bb)
    if has_rsi and has_bb:
        # “RSIは…中立” っぽいとき → BBを優先
        if ("RSI" in rsi) and ("中立" in rsi):
            picks.append(bb)
        else:
            picks.append(rsi)
    elif has_rsi or has_bb:
        picks.append(rsi or bb)

    # 最大2句に制限
    return picks[:2]





# ==== / 段落② プレビュー本体（統一版） ====================================



# ==== 段落②（プレビュー：新ロジック） ====
st.markdown("#### 段落②（プレビュー：新ロジック）")

def _num_or_none(x):
    """文字列/None/配列/NumPyスカラーにも耐性。0以下・非数は None。"""
    import math
    try:
        if x is None:
            return None
        if isinstance(x, (list, tuple)) and x:
            x = x[0]
        v = float(str(x).replace(",", "").strip())
        if not math.isfinite(v):
            return None
        return v if v > 0 else None
    except Exception:
        return None

def _pick_first_valid(*keys):
    """最初に見つかった有効数値と、そのキー名を返す。"""
    ss = st.session_state
    for k in keys:
        v = _num_or_none(ss.get(k))
        if v is not None:
            return v, k
    return None, None


# ==== 段落②（プレビュー：新ロジック）・BP反映ミックス ====

def _decimals_from_pair(pair_label: str) -> int:
    s = (pair_label or "")
    return 2 if ("JPY" in s.upper() or "円" in s) else 4

def _first_num(*vals):
    """先に見つかった有効な数値を返す（0以下やNaNは除外）。list/tuple/文字列/NumPyにも耐性。"""
    import math
    for v in vals:
        try:
            if v is None:
                continue
            if isinstance(v, (list, tuple)) and v:
                v = v[0]
            x = float(str(v).replace(",", "").strip())
            if math.isfinite(x) and x > 0:
                return x
        except Exception:
            pass
    return None

def _bp_sentence_mix() -> tuple[str, dict]:
    """
    プレビュー用のBP文を作る。
    優先順位（上側）: D1 -> H4 -> 共通
    優先順位（下側）: H4 -> D1 -> 共通
    """
    ss = st.session_state

    up_candidates = []
    if _allow_d1():
            up_candidates += [ss.get("p2_bp_d1_upper"), ss.get("bp_d1_up")]
    if _allow_h4():
            up_candidates += [ss.get("p2_bp_h4_upper"), ss.get("bp_h4_up")]
    up_candidates += [ss.get("p2_bp_upper"), ss.get("bp_up")]
    up_val = _first_num(*up_candidates)

    dn_candidates = []
    if _allow_h4():
            dn_candidates += [ss.get("p2_bp_h4_lower"), ss.get("bp_h4_dn")]
    if _allow_d1():
            dn_candidates += [ss.get("p2_bp_d1_lower"), ss.get("bp_d1_dn")]
    dn_candidates += [ss.get("p2_bp_lower"), ss.get("bp_dn")]
    dn_val = _first_num(*dn_candidates)


    pair = str(ss.get("pair", "") or "")
    decimals = _decimals_from_pair(pair)
    fmt = f"{{:.{decimals}f}}"

    debug = {
        "up": up_val, "dn": dn_val,
        "pair": pair, "decimals": decimals,
        "used_order_up":  ["p2_bp_d1_upper","bp_d1_up","p2_bp_h4_upper","bp_h4_up","p2_bp_upper","bp_up"],
        "used_order_down":["p2_bp_h4_lower","bp_h4_dn","p2_bp_d1_lower","bp_d1_dn","p2_bp_lower","bp_dn"],
    }

    if up_val is None and dn_val is None:
        return "", debug

    if up_val is not None and dn_val is not None:
        sent = f"{fmt.format(up_val)}付近の上抜け／{fmt.format(dn_val)}付近割れのどちらに傾くかを見極めたい。"
    elif up_val is not None:
        sent = f"{fmt.format(up_val)}付近の上抜けの有無をまず確かめたい。"
    else:
        sent = f"{fmt.format(dn_val)}付近割れの可否をまず確認したい。"

    return sent, debug

def _compose_para2_preview_mix_legacy() -> str:
    """[ラッパ] 旧名から新ロジックへ委譲（一本化）。"""
    return _compose_para2_preview_mix()




def _first_num(*vals):
    """
    先に見つかった有効な数値を返す（0以下やNaNは除外）。
    list/tuple/文字列/NumPyにも耐性。
    """
    import math
    for v in vals:
        try:
            if v is None:
                continue
            if isinstance(v, (list, tuple)) and v:
                v = v[0]
            x = float(str(v).replace(",", "").strip())
            if math.isfinite(x) and x > 0:
                return x
        except Exception:
            pass
    return None


def _bp_sentence_mix():
    """
    プレビュー用のBP文を作る。
    優先順位（上側）: D1 -> H4 -> 共通
    優先順位（下側）: H4 -> D1 -> 共通
    どちらか片方だけでも文を作る。
    """
    ss = st.session_state

    # 候補（それぞれの優先順で先に取れたものを採用）
    up_val = _first_num(
        ss.get("p2_bp_d1_upper"), ss.get("bp_d1_up"),
        ss.get("p2_bp_h4_upper"), ss.get("bp_h4_up"),
        ss.get("p2_bp_upper"),    ss.get("bp_up"),
    )
    dn_val = _first_num(
        ss.get("p2_bp_h4_lower"), ss.get("bp_h4_dn"),
        ss.get("p2_bp_d1_lower"), ss.get("bp_d1_dn"),
        ss.get("p2_bp_lower"),    ss.get("bp_dn"),
    )

    pair = str(ss.get("pair", "") or "")
    decimals = _decimals_from_pair(pair)
    fmt = f"{{:.{decimals}f}}"

    debug = {
        "up": up_val, "dn": dn_val,
        "pair": pair, "decimals": decimals,
        "used_order_up":  ["p2_bp_d1_upper","bp_d1_up","p2_bp_h4_upper","bp_h4_up","p2_bp_upper","bp_up"],
        "used_order_down":["p2_bp_h4_lower","bp_h4_dn","p2_bp_d1_lower","bp_d1_dn","p2_bp_lower","bp_dn"],
    }

    if up_val is None and dn_val is None:
        return "", debug

    if up_val is not None and dn_val is not None:
        sent = f"{fmt.format(up_val)}付近の上抜け／{fmt.format(dn_val)}付近割れのどちらに傾くかを見極めたい。"
    elif up_val is not None:
        sent = f"{fmt.format(up_val)}付近の上抜けの有無をまず確かめたい。"
    else:
        sent = f"{fmt.format(dn_val)}付近割れの可否をまず確認したい。"

    return sent, debug













# ==== / 段落② UIブロック（置き換え版） ===================================



def _vol_score(df: pd.DataFrame | None, ind: dict | None) -> float:
    """変化の大きさスコア：直近20本のBB幅/20SMAの平均（大きいほど変化が激しい）"""
    try:
        bbw = (ind["bb_upper"] - ind["bb_lower"]).tail(20).mean()
        mid = ind["sma20"].tail(20).mean()
        if mid is None or np.isnan(mid) or mid == 0:
            return -1.0
        return float(bbw / abs(mid))
    except Exception:
        return -1.0
# --- 指標計算（SMA/BB/RSI + クロス判定）: DataFrameの揺れに強い版 ---
def _close_series(df: _pd.DataFrame) -> _pd.Series:
    """
    df["Close"] が DataFrame になってしまう/列名の大小混在/重複などのケースを吸収し、
    必ず float の Series を返す。
    """
    if df is None or getattr(df, "empty", True):
        return _pd.Series(dtype="float64")

    # "Close" をケースインセンシティブで探索
    col_name = None
    for c in df.columns:
        if str(c).lower() == "close":
            col_name = c
            break
    if col_name is None:
        # 近い名前もないなら空Series
        return _pd.Series(dtype="float64", index=df.index)

    col = df[col_name]
    # 万一 DataFrame なら先頭列を採用
    if isinstance(col, _pd.DataFrame):
        col = col.iloc[:, 0]

    return _pd.to_numeric(col, errors="coerce").astype("float64")


def _sma_s(df_close: _pd.Series, n: int) -> _pd.Series:
    return df_close.rolling(n, min_periods=1).mean()


def _bbands_s(df_close: _pd.Series, n: int = 20, k: float = 2.0):
    ma = df_close.rolling(n, min_periods=1).mean()
    sd = df_close.rolling(n, min_periods=1).std(ddof=0)
    return ma + k * sd, ma - k * sd


def _rsi_s(close: _pd.Series, n: int = 14) -> _pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    ema_up = up.ewm(alpha=1 / n, adjust=False).mean()
    ema_dn = dn.ewm(alpha=1 / n, adjust=False).mean()
    rs = ema_up / ema_dn.replace(0, _np.nan)
    return (100 - (100 / (1 + rs))).clip(0, 100)


def _indicators(df: _pd.DataFrame) -> dict:
    """
    返り値：
      {
        "close": Series, "sma20": Series, "sma200": Series,
        "bb_u": Series, "bb_l": Series, "rsi14": Series,
        "gc_recent": bool, "dc_recent": bool
      }
    """
    if df is None or getattr(df, "empty", True):
        return {
            "close": None, "sma20": None, "sma200": None,
            "bb_u": None, "bb_l": None, "rsi14": None,
            "gc_recent": False, "dc_recent": False,
        }

    close = _close_series(df)
    sma20 = _sma_s(close, 20)
    sma200 = _sma_s(close, 200)
    bb_u, bb_l = _bbands_s(close, 20, 2.0)
    rsi14 = _rsi_s(close, 14)

    # ゴールデン/デッドクロス（直近20本のどこかで発生していれば True）
    diff = sma20 - sma200
    gc = (diff.shift(1) <= 0) & (diff > 0)
    dc = (diff.shift(1) >= 0) & (diff < 0)

    return {
        "close": close,
        "sma20": sma20, "sma200": sma200,
        "bb_u": bb_u, "bb_l": bb_l,
        "rsi14": rsi14,
        "gc_recent": bool(gc.tail(20).any()),
        "dc_recent": bool(dc.tail(20).any()),
    }

# === 段落② 用：ベース足の決定 + ヘルパ群 + 本文生成 ===

# 1) ベース足の決定（自動/手動）。ついでに d1_ind / h4_ind を必ず用意して互換にする
tf_base_choice = st.session_state.get("tf_base_choice", "自動")


if tf_base_choice == "自動":
    d1_ind = _indicators(_d1) if ('_d1' in globals() and _d1 is not None) else None
    h4_ind = _indicators(_h4) if ('_h4' in globals() and _h4 is not None) else None

    # volスコア（=直近20本のBB幅/20SMA）。キー表記の差異に両対応
    def _vol_score_auto(ind: dict | None) -> float:
        try:
            if not ind:
                return -1.0
            bb_hi = ind.get("bb_u", ind.get("bb_upper"))
            bb_lo = ind.get("bb_l", ind.get("bb_lower"))
            s20   = ind.get("sma20")
            if bb_hi is None or bb_lo is None or s20 is None:
                return -1.0
            bbw = (bb_hi - bb_lo).tail(20).mean()
            mid = s20.tail(20).mean()
            if mid is None or _np.isnan(mid) or mid == 0:
                return -1.0
            return float(bbw / abs(mid))
        except Exception:
            return -1.0

    s_d1 = _vol_score_auto(d1_ind)
    s_h4 = _vol_score_auto(h4_ind)
    # どちらかが欠落している場合のフォールバック
    if s_d1 < 0.0 and s_h4 >= 0.0:
        tf_base_effective = "4時間足"
    elif s_h4 < 0.0 and s_d1 >= 0.0:
        tf_base_effective = "日足"
    else:
        tf_base_effective = "4時間足" if s_h4 > s_d1 else "日足"
else:
    # 手動選択：必ず両方のインジ計算を持っておく（後工程の互換目的）
    d1_ind = _indicators(_d1) if ('_d1' in globals() and _d1 is not None) else None
    h4_ind = _indicators(_h4) if ('_h4' in globals() and _h4 is not None) else None
    tf_base_effective = tf_base_choice

# 2) 表現ヘルパ
def _mv_rel_text(s20: float | None, s200: float | None) -> str:
    if s20 is None or s200 is None:
        return ""
    return "短期20SMAは200SMAを上回り基調は上向き" if s20 > s200 else "短期20SMAは200SMAを下回り基調は下向き"

def _rsi_label(v: float | None) -> str:
    if v is None or _np.isnan(v):
        return ""
    if v >= 60:
        return "RSIは60台で上向きバイアス"
    if v < 40:
        return "RSIは40割れで上値の重さ"
    return "RSIは中立圏"

def _intro_from_impressions(d1_imp: str, h4_imp: str) -> str:
    def _cat(x: str) -> str:
        if not isinstance(x, str): return "flat"
        if "横ばい" in x: return "flat"
        if "アップ" in x: return "up"
        if "ダウン" in x: return "down"
        return "flat"

    key = (_cat(d1_imp), _cat(h4_imp))
    cands = _LEX_INTRO.get(key)
    if not cands:
        # フォールバック（万一辞書漏れがあっても安全）
        if key == ("flat","flat"): return "日足・4時間足ともレンジ色が濃い。"
        if key == ("up","up"):     return "日足・4時間足とも上方向が優勢。"
        if key == ("down","down"): return "日足・4時間足とも下方向が優勢。"
        return "日足と4時間足で見方が分かれる。"
    return _stable_pick(cands, category=f"intro:{key[0]}-{key[1]}")


# 3) 段落② 本文生成（v2.1）— 修正版（この関数をそのまま置き換えてください）
# 3) 段落② 本文生成（v2.1）
def _compose_para2_v21() -> str:
    # --- 安全取得（UIの印象） ---
    d1_imp = st.session_state.get("d1_imp", st.session_state.get("imp_d1", "横ばい"))
    h4_imp = st.session_state.get("h4_imp", st.session_state.get("imp_h4", "横ばい"))

    # --- 末端値ヘルパ ---
    def _last_float(s):
        try:
            if s is None:
                return None
            tail = s.iloc[-1:]
            return None if tail.isna().any() else float(tail.iloc[-1])
        except Exception:
            return None

    # グローバルの指標がある前提（無ければ None で続行）
    d1_s20  = _last_float(globals().get("d1_ind", {}).get("sma20"))   if globals().get("d1_ind") else None
    d1_s200 = _last_float(globals().get("d1_ind", {}).get("sma200"))  if globals().get("d1_ind") else None
    d1_rsi  = _last_float(globals().get("d1_ind", {}).get("rsi14"))   if globals().get("d1_ind") else None
    h4_s20  = _last_float(globals().get("h4_ind", {}).get("sma20"))   if globals().get("h4_ind") else None
    h4_s200 = _last_float(globals().get("h4_ind", {}).get("sma200"))  if globals().get("h4_ind") else None
    h4_rsi  = _last_float(globals().get("h4_ind", {}).get("rsi14"))   if globals().get("h4_ind") else None

    # 導入
    bits = [_intro_from_impressions(d1_imp, h4_imp)]

    # ベース足
    tf_base_effective = globals().get("tf_base_effective", "日足")  # "日足" / "4時間足"
    if tf_base_effective == "4時間足":
        mv = _mv_rel_text(h4_s20, h4_s200)
        rs = _rsi_label(h4_rsi)
        if mv: bits.append(mv + "。")
        if rs: bits.append(rs + "。")
        mv_d1 = _mv_rel_text(d1_s20, d1_s200)
        if mv_d1:
            trend = "上向き基調" if (h4_s20 is not None and h4_s200 is not None and h4_s20 > h4_s200) else "下向き基調"
            bits.append(f"日足は{trend}の確認を意識。")
    else:
        mv = _mv_rel_text(d1_s20, d1_s200)
        rs = _rsi_label(d1_rsi)
        if mv: bits.append(mv + "。")
        if rs: bits.append(rs + "。")
        mv_h4 = _mv_rel_text(h4_s20, h4_s200)
        if mv_h4: bits.append("4時間足の動意も併せて確認したい。")

    # ---------- ブレークポイントを本文②へ反映 ----------
    try:
        ss = st.session_state if 'st' in globals() else {}

        # 全角->半角などを吸収して数値化
        def _to_float(x):
            if x is None: return None
            s = str(x).strip()
            if not s: return None
            # 全角数字・記号を半角へ
            z2h = str.maketrans(
                "０１２３４５６７８９．，－＋",
                "0123456789.,-+"
            )
            s = s.translate(z2h).replace(",", "")
            try:
                v = float(s)
            except Exception:
                return None
            # 0.00 は「未入力」と解釈
            if abs(v) == 0.0:
                return None
            return v

        def _decimals_for_ticker(tkr: str) -> int:
            t = (tkr or "").upper()
            if "JPY" in t:  # USDJPY=X, EURJPY=X など
                return 2
            return 4

        # 代表ティッカー（小数桁判定用）
        tkr = ss.get("ticker_1d") or ss.get("ticker_4h") or ss.get("ticker") or ss.get("symbol") or ""

        # 明示キー優先で拾う → 見つからなければ fuzzy で拾う
        def _pick_exact(keys):
            for k in keys:
                if k in ss and ss.get(k) not in (None, "", []):
                    return ss.get(k)
            return None

        def _pick_fuzzy(include_tokens, exclude_tokens=None):
            inc = [tok.lower() for tok in include_tokens]
            exc = [tok.lower() for tok in (exclude_tokens or [])]
            for k in ss.keys():
                kl = str(k).lower()
                if all(tok in kl for tok in inc) and not any(tok in kl for tok in exc):
                    v = ss.get(k)
                    if v not in (None, "", []):
                        return v
            return None

        # 共通
        raw_u_common = _pick_exact(["bp_upper", "breakpoint_upper", "bpUp", "ui_bp_upper"]) \
                       or _pick_fuzzy(["bp", "upper"])
        raw_l_common = _pick_exact(["bp_lower", "breakpoint_lower", "bpDown", "ui_bp_lower"]) \
                       or _pick_fuzzy(["bp", "lower"])

        # 時間軸別（D1/H4）
        raw_u_d1 = _pick_exact(["d1_bp_upper", "bp_d1_upper", "ui_d1_bp_upper"]) \
                   or _pick_fuzzy(["d1", "bp", "upper"])
        raw_l_d1 = _pick_exact(["d1_bp_lower", "bp_d1_lower", "ui_d1_bp_lower"]) \
                   or _pick_fuzzy(["d1", "bp", "lower"])

        raw_u_h4 = _pick_exact(["h4_bp_upper", "bp_h4_upper", "ui_h4_bp_upper"]) \
                   or _pick_fuzzy(["h4", "bp", "upper"])
        raw_l_h4 = _pick_exact(["h4_bp_lower", "bp_h4_lower", "ui_h4_bp_lower"]) \
                   or _pick_fuzzy(["h4", "bp", "lower"])

        # 文字列→数値（0 扱いは None）
        u_common = _to_float(raw_u_common)
        l_common = _to_float(raw_l_common)
        u_d1     = _to_float(raw_u_d1)
        l_d1     = _to_float(raw_l_d1)
        u_h4     = _to_float(raw_u_h4)
        l_h4     = _to_float(raw_l_h4)

        # どの時間軸の値を本文に使うか
        axis_pref = ss.get("bp_axis_for_text") or ss.get("bp_axis") or ss.get("bp_tf_for_text") or ss.get("breakpoint_axis_for_text") or "自動（基準に合わせる）"

        # 自動：本文ベース足に合わせて D1/H4 を優先し、無ければ共通
        def _choose_pair():
            if axis_pref == "日足のみ":
                return (u_d1, l_d1, "（日足）")
            if axis_pref == "4時間足のみ":
                return (u_h4, l_h4, "（4時間足）")
            # 自動
            base = tf_base_effective
            if base == "日足":
                uu, ll = (u_d1, l_d1)
                tfp = "（日足）"
            else:
                uu, ll = (u_h4, l_h4)
                tfp = "（4時間足）"
            # なければ共通へフォールバック
            if uu is None and ll is None:
                return (u_common, l_common, "")
            return (uu, ll, tfp)

        u_val, l_val, tf_phrase = _choose_pair()

        # 共通にも何も無ければ、本文へは書かない
        if (u_val is not None) or (l_val is not None):
            dec = _decimals_for_ticker(tkr)
            fmt_u = (f"{u_val:.{dec}f}" if u_val is not None else None)
            fmt_l = (f"{l_val:.{dec}f}" if l_val is not None else None)

            import random
            around = random.choice(["付近", "前後", "どころ"])
            if fmt_u and fmt_l:
                bits.append(f"上値は{fmt_u}{around}、下値は{fmt_l}{around}{tf_phrase}。")
            elif fmt_u:
                bits.append(f"上値は{fmt_u}{around}{tf_phrase}。")
            elif fmt_l:
                bits.append(f"下値は{fmt_l}{around}{tf_phrase}。")

    except Exception:
        # いかなる例外も本文生成を止めない
        pass

    # 結び
    bits.append("過度な一方向は決めつけにくく、まずは反応を確かめたい。")

    text = "".join(bits).replace("。。", "。").strip()
    return text





para2_seed_v21 = _compose_para2_v21()
default_para2_seed = para2_seed_v21

# === ここまで（常時2枚表示 & ベース足選択 & 自動選択 & H4ベース時にD1所見を必ず挿入） ===


st.markdown("### ステップ1：参照PDFの確認")
missing = []
for i, p in enumerate(CFG.get("pdf_paths", []), start=1):
    exists = os.path.exists(p)
    st.write(f"{i}. {p}  →  {'✅ 見つかりました' if exists else '❌ 見つかりません'}")
    if exists:
        # ★ ここを 'rb' に（小文字）★
        with open(p, "rb") as f:
            st.download_button(
                f"PDFをダウンロード {i}",
                data=f.read(),
                file_name=os.path.basename(p),
                mime="application/pdf",
                key=f"pdf_dl_{i}"  # 重複防止のため固有キー
            )
    else:
        missing.append(p)
if missing:
    st.warning("上の ❌ のPDFが見つかりません。config.yaml のパスを修正して保存し、左上の『再実行』を押してください。")



# ===== PDF→テキスト抽出 & ハッシュ確認 =====
import hashlib
try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

def _pdf_sha12(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except Exception:
        return "NA"

def _extract_pdf_text(paths: list[str], page_max: int = 20, chars_max: int = 6000) -> str:
    """
    与えたPDF群（先頭から）を順に開き、最大 page_max ページまでからテキスト抽出。
    合計 chars_max 文字で打ち切り。pypdfが無い/失敗時は空文字。
    """
    if PdfReader is None:
        return ""
    buff = []
    try:
        for p in paths:
            if not os.path.exists(p): 
                continue
            reader = PdfReader(p)
            n = min(len(reader.pages), page_max)
            for i in range(n):
                txt = reader.pages[i].extract_text() or ""
                buff.append(txt)
                if sum(len(x) for x in buff) >= chars_max:
                    break
            if buff:
                break
    except Exception:
        return ""
    out = "".join(buff).strip()
    # ノイズ除去の軽い整形
    out = out.replace("\u3000", " ")
    return out[:chars_max].strip()

# 目的：配布用PDF（正典）のテキストから体裁ルール要点を抽出し、data/rules_digest.txt を自動生成・更新する“任意の補助機能”（本線が無くても動作可）
# ====== 補足（任意）：PDF→ルール要約の自動抽出（正典をAIに必ず読ませる） ======
st.subheader("補足（任意）：PDF→ルール要約の自動抽出（正典をAIに必ず読ませる）")


pdf_list = [str(p) for p in CFG.get("pdf_paths", [])]
if not pdf_list:
    st.info("config.yaml の pdf_paths が未設定です。Step1でPDFが確認できるようにしてください。")

cols = st.columns(2)
with cols[0]:
    if pdf_list:
        hashes = [f"{os.path.basename(p)} : #{_pdf_sha12(p)}" for p in pdf_list if os.path.exists(p)]
        st.caption("PDFバージョン（SHA-256の先頭12桁）")
        for h in hashes:
            st.code(h, language="text")

with cols[1]:
    if st.button("PDFから rules_digest.txt を作成/更新", key="pdf_to_digest"):
        if PdfReader is None:
            st.error("pypdf が必要です。ターミナルで `pip install pypdf` を実行してください。")
        else:
            raw = _extract_pdf_text(pdf_list, page_max=30, chars_max=8000)

            if not raw:
                st.warning("PDFテキストが抽出できませんでした。PDFが画像のみの可能性があります。")
                digest = ""
            else:
                # なるべく「見出し・箇条書き」らしい行を優先。なければ先頭から切り出し。
                lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
                bullets = [ln for ln in lines if ln.startswith(("-", "・", "●", "■")) or ("：" in ln or ":" in ln)]
                digest = "\n".join(bullets) if len(bullets) >= 5 else "\n".join(lines[:120])
                digest = digest[:2000].strip()

            # 保存
            outp = Path("data") / "rules_digest.txt"
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_text(digest, encoding="utf-8")

            # アプリ内の RULES_DIGEST を更新して即反映
            try:
                RULES_DIGEST = _read_rules_digest()
            except Exception:
                pass

            st.success(f"rules_digest.txt を更新しました（{len(digest)}字）。")

            # ★ここが“画面プレビュー”です。成功メッセージの直後に出ます。★
            if digest:
                st.caption("要約プレビュー（先頭400字）")
                preview = digest[:400]
                if len(digest) > 400:
                    preview += " …"
                st.code(preview, language="markdown")
            else:
                st.caption("要約プレビュー：抽出できませんでした（rules_digest.txt は空です）")


st.markdown("---")

# ====== ステップ2：イベント候補の確認（表示だけ／FxON固定） ======
st.markdown("### ステップ2：イベント候補の確認")
st.caption("候補の選択と重要度付けは『ステップ4：指標候補（TopN + チェックで本文③へ）』で行います。ここでは読み込み状況のみ表示します。")

# Step4 で設定した結果を読む（なければ“取得予定”を案内）
src = st.session_state.get("events_source")
rng = st.session_state.get("events_range") or {}
cnt = st.session_state.get("events_rows")

if src:
    # 表示名を少しだけ整形（"fxon" → "FxON API（読み込みOK）"、CSVパスは見やすく）
    label = str(src).strip()
    if label.lower() in {"fxon", "fxon api", "fxon_api"}:
        label = "FxON API（読み込みOK）"
    if label.lower().endswith(".csv"):
        label = f"CSV（{label}）"

    # レンジ/件数があれば併記（JST yyyy-mm-dd→yyyy-mm-dd / N件）
    r_from = rng.get("from") or rng.get("d1") or rng.get("start")
    r_to   = rng.get("to")   or rng.get("d2") or rng.get("end")
    tail = f"（JST {r_from}→{r_to} / {cnt}件）" if (r_from and r_to and cnt is not None) else ""
    st.success(f"候補ソース：{label}{tail}")
else:
    # ここは“予定”の告知だけ（実際の取得は Step4）
    st.success("候補ソース：FxON API（JST / 今日〜+2日の3日レンジを取得予定）")

st.markdown("---")





# ====== タイトル・回収の補助（ステップ3の直前に置く） ======

def _get_api_key() -> str | None:
    """
    OPENAI_API_KEY を次の優先順で取得するユーティリティ（将来変更に強い設計）。
    1) st.secrets["OPENAI_API_KEY"]
    2) st.secrets["general"]["OPENAI_API_KEY"]
    3) 環境変数 OPENAI_API_KEY
    見つからなければ None を返す。
    ※ 秘密鍵の保管場所が変わっても、この関数だけ差し替えれば全体が対応可。
    """
    # 1) / 2) Streamlit secrets（ローカルimportで安全化）
    try:
        import streamlit as st
        if "OPENAI_API_KEY" in st.secrets:
            return st.secrets["OPENAI_API_KEY"]
        if "general" in st.secrets and "OPENAI_API_KEY" in st.secrets["general"]:
            return st.secrets["general"]["OPENAI_API_KEY"]
    except Exception:
        # st.secrets が使えない環境（ローカル等）はスキップ
        pass
    # 3) 環境変数
    import os
    return os.environ.get("OPENAI_API_KEY")


# 目的：段落②が短い時に“安全な汎用文”をYAMLから読み込み、結びの直前に自動で追記できるようにするためのローダー群
# ====== 段落② 安全文（外部YAML）ローダーと拡張ロジック ======

try:
    import yaml
except Exception:
    yaml = None  # 未インストールでもアプリは落とさない


_P2_SAFE_CACHE = None

def _load_para2_boiler_yaml(path: str | Path = "data/para2_boilerplate.yaml") -> dict:
    """YAMLを読み込み（無ければ空）。'generic'と'per_pair'を返す。"""
    global _P2_SAFE_CACHE
    if _P2_SAFE_CACHE is not None:
        return _P2_SAFE_CACHE

    # ↓↓↓ ここを追加 ↓↓↓
    if yaml is None:
        _P2_SAFE_CACHE = {"generic": [], "per_pair": {}}
        return _P2_SAFE_CACHE
    # ↑↑↑ ここまで追加 ↑↑↑

    p = Path(path)
    if not p.exists():
        _P2_SAFE_CACHE = {"generic": [], "per_pair": {}}
        return _P2_SAFE_CACHE
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        gen = data.get("generic", []) or []
        per = data.get("per_pair", {}) or {}
        _P2_SAFE_CACHE = {"generic": list(gen), "per_pair": dict(per)}
    except Exception:
        _P2_SAFE_CACHE = {"generic": [], "per_pair": {}}
    return _P2_SAFE_CACHE
def _load_para2_boiler(path: str | Path = "data/para2_boilerplate.yaml") -> dict:
    """互換ラッパー：既存コードの呼び名を維持しつつ、実体は YAML ローダーへ委譲。"""
    return _load_para2_boiler_yaml(path)

def _split_off_closer(text: str, closers: list[str]) -> tuple[str, str]:
    """末尾が許可済みの結びなら切り出す。（本文, 結び）を返す。"""
    t = (text or "").rstrip()
    for c in closers:
        if t.endswith(c):
            body = t[: -len(c)].rstrip()
            return body, c
    return t, ""

def _extend_para2_if_short(para2: str, pair: str, min_chars: int, closers: list[str]) -> str:
    """
    文字数が下限未満なら、安全文を“結びの直前”に足して満たす。
    結び（例：『値動きには警戒したい。』）は必ず文末に残す。
    """
    body, closer = _split_off_closer(para2, closers)
    base = body.strip()
    if not base.endswith("。"):
        base += "。"

    # 既に足りていればそのまま戻す
    if len(base.replace("\n", "")) >= min_chars:
        return (base + (" " + closer if closer else "")).strip()

    rules = _load_para2_boiler()
    pool = list(rules.get("generic", []))
    pool += list(rules.get("per_pair", {}).get(pair, []))

    used = set()
    out = base
    i = 0
    while len(out.replace("\n", "")) < min_chars and i < len(pool):
        sent = pool[i].strip()
        i += 1
        if not sent or sent in used:
            continue
        used.add(sent)
        if not out.endswith("。"):
            out += "。"
        out += " " + sent
    # 万一まだ足りなければ最後に短い安全文を一つ
    if len(out.replace("\n", "")) < min_chars:
        out += " 市場の振れに留意したい。"

    if closer:
        if not out.endswith("。"):
            out += "。"
        out += " " + closer
    return out.strip()
# ====== ここまで ======


def choose_title_tail(para1: str, para2: str) -> str:
    """タイトルの語尾：AIに候補から1つ選ばせ、失敗時はローカル乱択。"""
    system = (
        "あなたは金融レポートの校正者です。断定を避けた語尾を選びます。"
        "必ず、与えられた候補の中から、レポート全体の文脈に最も自然なものを『語尾だけ』で返します。"
        "候補に無い語は出さないでください。句点は不要です。"
        + (f"\n【遵守ルール（抜粋）】\n{RULES_DIGEST}\n" if RULES_DIGEST else "")
    )
    user = (
        "候補: " + " / ".join(ALLOWED_TITLE_TAILS) + "\n\n"
        "文脈（段落①②の下書き）:\n" + para1 + "\n---\n" + para2 + "\n\n"
        "出力は語尾（例：警戒か）のみ。"
    )
    picked = _llm_pick_from_list(system, user)
    return picked if picked in ALLOWED_TITLE_TAILS else random.choice(ALLOWED_TITLE_TAILS)


def choose_para2_closer(para1: str, para2: str) -> str:
    """段落②の結び：AIに候補から1文選ばせ、失敗時はローカル乱択。"""
    system = (
        "あなたは金融レポートの校正者です。断定を避けた結びの一文を選びます。"
        "必ず、与えられた候補の中から、文脈に最も自然なものを『一文そのまま』で返します。"
        "候補に無い文は作らないでください。"
        + (f"\n【遵守ルール（抜粋）】\n{RULES_DIGEST}\n" if RULES_DIGEST else "")
    )
    user = (
        "候補: " + " / ".join(ALLOWED_PARA2_CLOSERS) + "\n\n"
        "文脈（段落①②の下書き）:\n" + para1 + "\n---\n" + para2 + "\n\n"
        "出力は候補の一文のみ。"
    )
    picked = _llm_pick_from_list(system, user)
    return picked if picked in ALLOWED_PARA2_CLOSERS else random.choice(ALLOWED_PARA2_CLOSERS)


# ---- タイトル初期値（助詞を自動補正）
def _default_title_for(pair: str, tail: str) -> str:
    tail = (tail or "").strip()
    if tail == "見極めたい":  # 「の方向感“を”見極めたい」に統一
        return f"{pair}の方向感を見極めたい"
    return f"{pair}の方向感に{tail}"

# ---- 本文③のタイトル回収（一文）
def build_title_recall(title: str) -> str:
    if _build_title_recall_from_mod:
        try:
            return _build_title_recall_from_mod(title)
        except Exception:
            pass
    t = (title or "").strip()
    tail_map = {
        "注視か": "注視したい。",
        "警戒か": "警戒したい。",
        "静観か": "静観したい。",
        "要注意か": "要注意としたい。",
    }
    for q, fin in tail_map.items():
        if t.endswith(q):
            stem = t[: -len(q)].rstrip()
            if fin.endswith("したい。") and not stem.endswith("に"):
                stem += "に"
            if not stem.endswith("。") and fin.endswith("。"):
                return stem + fin
            return stem + fin
    if t.endswith("見極めたい"):
        t = t.replace("の方向感に見極めたい", "の方向感を見極めたい")
        if not t.endswith("。"):
            t += "。"
        return t
    if not t.endswith("。"):
        t += "。"
    return t

# ===== 1) yfinanceベースのレジーム判定ユーティリティ =====
import math
from dataclasses import dataclass
import numpy as np

# yfinance は別途: pip install yfinance
try:
    import yfinance as yf
except Exception:
    yf = None

# ペア名 -> 取得候補ティッカー（上から順に試行）
_PAIR_TICKERS = {
    "ドル円": ["USDJPY=X"],
    "ユーロドル": ["EURUSD=X"],
    "ユーロ円": ["EURJPY=X"],
    "ポンドドル": ["GBPUSD=X"],
    "ポンド円": ["GBPJPY=X"],
    "豪ドル米ドル": ["AUDUSD=X"],
    "NZドル米ドル": ["NZDUSD=X"],
    "金/米ドル": ["XAUUSD=X", "GC=F"],        # 失敗時は金先物
    "ビットコイン/米ドル": ["BTC-USD"],
}

@dataclass
class LiveMetrics:
    ticker: str
    close: float
    ema200: float
    ema200_slope: float
    sma20: float
    sma20_slope: float
    bb_width_pct: float
    adx: float
    atr_pct: float
    last_ts: str

def _pair_to_ticker(pair: str) -> list[str]:
    return _PAIR_TICKERS.get(pair, [])

def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    return pd.Series(arr).ewm(span=span, adjust=False).mean().to_numpy()

def _sma(arr: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(arr).rolling(n).mean().to_numpy()

def _atr_adx(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series]:
    """Wilder方式で ATR と ADX を計算（n=14が定番）。"""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    # True Range
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    # +DM, -DM
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    # Wilderの平滑化（EMAのalpha=1/nと同義）
    atr = tr.ewm(alpha=1/n, adjust=False).mean()

    plus_di = 100 * (plus_dm.ewm(alpha=1/n, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/n, adjust=False).mean() / atr)
    dx = ( (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) ) * 100
    adx = dx.ewm(alpha=1/n, adjust=False).mean()

    return atr, adx

def _bb_width_pct(close: pd.Series, n: int = 20) -> pd.Series:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    upper = mid + 2*sd
    lower = mid - 2*sd
    width = (upper - lower) / mid  # 中心線に対する幅
    return width

def _fetch_1h_metrics(pair: str, days: int = 30) -> LiveMetrics | None:
    if yf is None:
        return None
    tickers = _pair_to_ticker(pair)
    for tk in tickers:
        try:
            df = yf.Ticker(tk).history(period=f"{days}d", interval="60m", auto_adjust=False)
            if df is None or df.empty or {"Open","High","Low","Close"} - set(df.columns):
                continue
            df = df.dropna().copy()
            if df.empty:
                continue

            c = df["Close"].to_numpy()
            ema200 = _ema(c, 200)
            sma20 = _sma(c, 20)
            bbp = _bb_width_pct(df["Close"], 20)

            # 直近値
            last = df.index[-1]
            last_close = float(c[-1])
            last_ema200 = float(ema200[-1]) if not math.isnan(ema200[-1]) else last_close
            last_sma20 = float(sma20[-1]) if not math.isnan(sma20[-1]) else last_close
            last_bbp = float(bbp.iloc[-1]) if not math.isnan(bbp.iloc[-1]) else 0.0

            # 傾き：直近20本の回帰ではなく、単純差分の割合で素直に（過剰最適化を避ける）
            ema200_slope = float((ema200[-1] - ema200[-20]) / max(1e-9, ema200[-20])) if len(ema200) >= 20 and not math.isnan(ema200[-20]) else 0.0
            sma20_slope = float((sma20[-1] - sma20[-10]) / max(1e-9, sma20[-10])) if len(sma20) >= 10 and not math.isnan(sma20[-10]) else 0.0

            atr, adx = _atr_adx(df, n=14)
            last_atr = float(atr.iloc[-1] / last_close) if atr.iloc[-1] and last_close else 0.0
            last_adx = float(adx.iloc[-1]) if adx.iloc[-1] else 0.0

            return LiveMetrics(
                ticker=tk,
                close=last_close,
                ema200=last_ema200,
                ema200_slope=ema200_slope,
                sma20=last_sma20,
                sma20_slope=sma20_slope,
                bb_width_pct=last_bbp,
                adx=last_adx,
                atr_pct=last_atr,
                last_ts=str(last.tz_localize(None)) if hasattr(last, "tz_localize") else str(last),
            )
        except Exception:
            continue
    return None

# ==== 段落①用：市場データユーティリティ（重複定義しない） ====
from datetime import datetime, timezone, timedelta

# JST文字列（as of 用）
if "_jst_now_str" not in globals():
    def _jst_now_str() -> str:
        try:
            JST = timezone(timedelta(hours=9))
            return datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
        except Exception:
            # タイムゾーンが使えない環境でも落ちない
            return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

# 終値ベース：直近(t0)と一つ前(t-1)の終値を返す（週末・休場は period で吸収）
if "_yf_last_two_daily" not in globals():
    def _yf_last_two_daily(ticker: str):
        """
        戻り値: (prev_close, last_close)
        取得失敗時は None
        """
        try:
            import yfinance as yf
        except Exception:
            return None
        try:
            df = yf.download(ticker, period="15d", interval="1d", auto_adjust=False, progress=False)
            if df is None or df.empty or "Close" not in df:
                return None
            close = df["Close"].dropna()
            if len(close) < 2:
                return None
            prev_close = float(close.iloc[-2])
            last_close = float(close.iloc[-1])
            return (prev_close, last_close)
        except Exception:
            return None

# ％変化（小数→％表示用）
if "_pct" not in globals():
    def _pct(prev: float, last: float) -> float:
        try:
            return (float(last) / float(prev) - 1.0) * 100.0
        except Exception:
            return 0.0

# ％のフォーマット（±X.X%）
if "_fmt_pct" not in globals():
    def _fmt_pct(p: float) -> str:
        try:
            return f"{float(p):+,.1f}%"
        except Exception:
            return "+0.0%"

# bpのフォーマット（^TNX用：差×10がbp）
if "_fmt_bp" not in globals():
    def _fmt_bp(prev: float, last: float) -> int:
        try:
            return int(round((float(last) - float(prev)) * 10))
        except Exception:
            return 0






def _classify_regime(m: LiveMetrics, cfg: dict) -> str:
    """
    戻り値：'trend_up' / 'trend_down' / 'range'
    ルールは保守的（嘘をつかない）。どれにも強く当てはまらない時は 'range' に寄せる。
    """
    th = ((cfg or {}).get("live") or {}).get("thresholds") or {}
    ADX_TREND = float(th.get("adx_trend", 22))
    BW_RANGE_MAX = float(th.get("bb_range_max", 0.012))
    SLOPE_EMA200_MIN = float(th.get("slope_ema200_min", 0.0))
    SLOPE_SMA20_ABS_MAX = float(th.get("slope_sma20_abs_max", 0.0006))

    is_up = (m.adx >= ADX_TREND) and (m.close >= m.ema200) and (m.ema200_slope > SLOPE_EMA200_MIN)
    is_down = (m.adx >= ADX_TREND) and (m.close <= m.ema200) and (m.ema200_slope < -SLOPE_EMA200_MIN)

    if is_up:
        return "trend_up"
    if is_down:
        return "trend_down"

    # レンジ判定：BB幅が狭く、短期SMAの傾きが小さい
    if (m.bb_width_pct <= BW_RANGE_MAX) and (abs(m.sma20_slope) <= SLOPE_SMA20_ABS_MAX):
        return "range"

    # どちらとも言い切れない時は、断定を避けてレンジ寄りの表現へ
    return "range"

def _seed_para2_from_metrics(pair: str, m: LiveMetrics, regime: str) -> str:
    mk = _market_word_for(pair)
    if regime == "trend_up":
        return (
            f"{mk}は、{pair}は1時間足ではADXが{m.adx:.1f}、終値がEMA200上に位置し、"
            f"EMA200の傾きも上向きで上昇トレンドが意識されやすい。"
            "4時間足では20SMAやBB±2σ付近の押し目・戻り目が手掛かりとなり、"
            "日足では200SMA/EMAと20SMAの並びが上向きバイアスを示しやすい。"
        )
    if regime == "trend_down":
        return (
            f"{mk}は、{pair}は1時間足ではADXが{m.adx:.1f}、終値がEMA200下に位置し、"
            f"EMA200の傾きも下向きで下降トレンドが意識されやすい。"
            "4時間足では20SMAやBB±2σ付近の戻り売りが意識されやすく、"
            "日足では200SMA/EMAと20SMAの位置関係が下向きバイアスの目安となる。"
        )
    # range
    return (
        f"{mk}は、{pair}は1時間足でADXが{m.adx:.1f}と低めで、BB幅{m.bb_width_pct*100:.2f}%も小さく、"
        "短期20SMAの傾きも限定的でレンジ推移が意識されやすい。"
        "4時間足ではBB±2σと20SMAの帯域が目先のレンジ境界となりやすく、"
        "日足では200SMA/EMAと20SMAの位置関係を確認したい。"
    )

def _para2_from_live(pair: str, cfg: dict) -> tuple[str | None, dict]:
    """成功時は本文シードと診断情報を返す。失敗時は (None, diag)。"""
    if not ((cfg.get("live") or {}).get("enabled", True)):
        return None, {"enabled": False}

    days = (cfg.get("live") or {}).get("yf_days", 30)
    m = _fetch_1h_metrics(pair, days=days)
    if not m:
        return None, {"enabled": True, "reason": "no_data"}

    regime = _classify_regime(m, cfg)
    seed = _seed_para2_from_metrics(pair, m, regime)

    # ②はここでは「結び」を付けない（後段のAI/フォールバックで付く）
    return seed, {
        "enabled": True,
        "ticker": m.ticker,
        "last_ts": m.last_ts,
        "regime": regime,
        "adx": round(m.adx, 1),
        "bb_width_pct": round(m.bb_width_pct * 100, 2),
        "ema200_slope": round(m.ema200_slope, 4),
        "sma20_slope": round(m.sma20_slope, 4),
        "atr_pct": round(m.atr_pct * 100, 2),
    }
# ===== ここまで =====

# ===== データ取得ユーティリティ（API → CSV → ダミー の順で候補を作る） =====
# メモ：JST はファイル内に複数定義あり（互換のため残置）。
# 最終整理で CANON_JST へ一本化予定。
import requests
from datetime import datetime, timedelta, timezone
JST = timezone(timedelta(hours=9))

def _get_secret(name: str) -> str | None:
    # st.secrets優先 → 環境変数
    try:
        v = st.secrets.get(name)
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get(name)


# ===== FxON専用：地域略称 / 時刻整形 / 取得→本文（段落③）・本日のポイント生成 =====
from datetime import date as _date_cls, datetime as _dt
import re

def _region_code_to_jp_prefix(code: str) -> str:
    """地域コード→日本語接頭辞（米・日・欧・英・豪・NZ・中・南ア・独・仏・伊・加・西・スイス）。なければそのままコード。"""
    code = (code or "").upper()
    m = {
        "US": "米", "JP": "日", "EU": "欧", "UK": "英", "AU": "豪", "NZ": "NZ",
        "CN": "中", "ZA": "南ア", "DE": "独", "FR": "仏", "IT": "伊", "CA": "加",
        "ES": "西", "CH": "スイス",
    }
    return m.get(code, code)

def _fmt_hhmm_any(val) -> tuple[str, int]:
    """
    値を 'H:MM' に正規化。並べ替え用に total_minutes も返す（不明は大きな数）。
    受け入れ：'21:30' / '09:05' / ISO文字列 / datetime / dict{'hour','minute'} など。
    """
    if val is None:
        return "--:--", 10**9

    # 文字列 'HH:MM'
    if isinstance(val, str):
        s = val.strip()
        m = re.match(r"^(\d{1,2}):(\d{2})$", s)
        if m:
            h, mm = int(m.group(1)), int(m.group(2))
            return f"{h}:{mm:02d}", h * 60 + mm
        # ISO日時を一応救済
        try:
            dt = _dt.fromisoformat(s.replace("Z", "+00:00"))
            h, mm = dt.hour, dt.minute
            return f"{h}:{mm:02d}", h * 60 + mm
        except Exception:
            return "--:--", 10**9

    # datetime
    if isinstance(val, _dt):
        h, mm = val.hour, val.minute
        return f"{h}:{mm:02d}", h * 60 + mm

    # dict など
    try:
        h, mm = int(val.get("hour", 0)), int(val.get("minute", 0))
        return f"{h}:{mm:02d}", h * 60 + mm
    except Exception:
        return "--:--", 10**9

def _fxon_call_list(d1: str, d2: str) -> list[dict]:
    """
    既存の FxON リストAPI関数を呼ぶための薄いラッパ。
    環境により関数名が異なる可能性を吸収する。
    """
    # 既存のどれかが定義されている前提（プロジェクトに合わせて優先順）
    for fn in ("_fxon_fetch_list", "fxon_fetch_list", "fetch_fxon_events"):
        f = globals().get(fn)
        if callable(f):
            return f(d1, d2)
    # キャッシュを使っている場合の救済
    try:
        return list(st.session_state.get("_fxon_cached_rows", []) or [])
    except Exception:
        return []

def _events_from_fxon_for_date(target_date: _date_cls) -> list[dict]:
    """
    FxON API から target_date のイベントを取り出し、
    {time, minute_sort, name, region, category, score} に正規化した行の配列を返す。
    """
    d1 = d2 = target_date.strftime("%Y-%m-%d")
    raw = _fxon_call_list(d1, d2)

    out: list[dict] = []
    for it in (raw or []):
        # 代表的なキー名のゆらぎを吸収
        # 日付一致チェック（ある場合のみ）
        day = (it.get("date") or it.get("日付") or "").strip()
        if day and day[:10] != d1:
            continue

        name   = (it.get("name")   or it.get("指標") or it.get("title") or "").strip()
        region = (it.get("region") or it.get("地域") or it.get("country_code") or it.get("country") or "").strip()
        score  = it.get("score")   or it.get("スコア") or 0

        # 時刻
        hhmm, minute_sort = _fmt_hhmm_any(it.get("time") or it.get("時刻") or it.get("datetime") or it.get("dt"))
        if not name:
            continue

        out.append({
            "time": hhmm,
            "minute_sort": minute_sort,
            "name": name,
            "region": region,
            "category": (it.get("category") or it.get("カテゴリ") or "").strip(),
            "score": int(score) if str(score).isdigit() else 0,
        })

    # 時刻順（不明 '--:--' は最後）
    out.sort(key=lambda r: r["minute_sort"])
    return out

def compose_calendar_line_from_fxon(target_date: _date_cls) -> str:
    """
    本文③の1行テキストを FxON のみで生成：
    例）'本日の指標は、21:30 に米・◯◯、23:00 に米・◯◯。'
    """
    rows = _events_from_fxon_for_date(target_date)
    if not rows:
        return ""

    lines = [f'{r["time"]} に{_region_code_to_jp_prefix(r["region"])}・{r["name"]}' for r in rows]
    return "本日の指標は、" + "、".join(lines) + "。"

def refresh_calendar_from_fxon(target_date: _date_cls, top_n: int = 2) -> None:
    """
    - st.session_state['calendar_line'] を FxONのみで更新
    - st.session_state['points_tags_v2'] に“本日のポイント”用の2件を格納
      （スコア降順→時刻昇順で抽出、スコアが無い場合は時刻順）
    """
    rows = _events_from_fxon_for_date(target_date)
    st.session_state["calendar_line"] = compose_calendar_line_from_fxon(target_date)

    if not rows:
        st.session_state["points_tags_v2"] = []
        return

    ranked = sorted(rows, key=lambda r: (-int(r.get("score", 0)), r["minute_sort"]))
    points = [f'{r["time"]} に{_region_code_to_jp_prefix(r["region"])}・{r["name"]}'
            for r in ranked[:max(0, top_n)]]
    st.session_state["points_tags_v2"] = points






# ===== ここまで（関数は必ず1つだけ） =====

# ---------- yfinance ベースの TA 計算（頑丈版：列名を正規化 → 4Hは1Hからリサンプリング） ----------
import pandas as pd, numpy as np, yfinance as yf

def ta_block(symbol: str = "GBPJPY=X", days: int = 90):
    # 共通：OHLCVの列をフラット＆標準化する（FXの欠損に強い版）
    def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()

        d = df.copy()

        # 1) 列が MultiIndex なら末尾レベル（"Open" など）だけにする
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(-1)

        # 2) 列名の表記ゆれを統一（"open"→"Open" など）
        d = d.rename(columns={str(c): str(c).strip().title() for c in d.columns})

        # 3) Close が無ければ Adj Close を Close として使う
        if "Close" not in d.columns and "Adj Close" in d.columns:
            d["Close"] = d["Adj Close"]

        # 4) ここでまだ Close が無ければ安全に空DFを返す（以降で KeyError を防ぐ）
        if "Close" not in d.columns:
            return pd.DataFrame()

        # 5) OHLC が欠けていれば Close で補完（FXで起きがち）
        for c in ("Open", "High", "Low"):
            if c not in d.columns:
                d[c] = d["Close"]

        # 6) Volume が無ければ 0 を入れる（FXでは無いことが多い）
        if "Volume" not in d.columns:
            d["Volume"] = 0.0

        # 7) DatetimeIndex を保証
        if not isinstance(d.index, pd.DatetimeIndex):
            d.index = pd.to_datetime(d.index, errors="coerce")

        # 8) 数値化＆Close 欠損除去
        for c in ["Open", "High", "Low", "Close", "Volume"]:
            if c in d.columns:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        d = d.dropna(subset=["Close"]).sort_index()

        # 9) 必要列（存在するものだけ）で返す
        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in d.columns]
        return d[cols]



        # 1) 1時間足を取得 → すぐ正規化
    df_h1_raw = yf.download(symbol, period=f"{days}d", interval="1h", progress=False)

    # 正規化（MultiIndex解除・列名統一・Adj Close→Close 等）
    df_h1 = _normalize_ohlcv(df_h1_raw)

    # 2) 4時間足は 1時間足からリサンプリング
    if not df_h1.empty:
        # df_h1 に「ある列だけ」で集計辞書を組む（Volume が無いFXにも対応）
        agg = {}
        if "Open" in df_h1.columns:  agg["Open"]  = "first"
        if "High" in df_h1.columns:  agg["High"]  = "max"
        if "Low"  in df_h1.columns:  agg["Low"]   = "min"
        if "Close" in df_h1.columns: agg["Close"] = "last"
        if "Volume" in df_h1.columns: agg["Volume"] = "sum"

        if agg:  # 念のため
            df_h4 = df_h1.resample("4H").agg(agg).dropna()
        else:
            df_h4 = pd.DataFrame()
    else:
        df_h4 = pd.DataFrame()

    # 3) 日足（取得→正規化）
    df_d_raw = yf.download(symbol, period=f"{max(days,200)}d", interval="1d", progress=False)
    df_d = _normalize_ohlcv(df_d_raw)


    # 4) 指標（SMA/EMA/BB/ADX）を付与
    def add_ta(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        d = df.copy()
        c, h, l = d["Close"], d["High"], d["Low"]

        # SMA/EMA
        sma20  = c.rolling(20, min_periods=20).mean()
        ema200 = c.ewm(span=200, adjust=False).mean()
        d["SMA20"]  = sma20
        d["EMA200"] = ema200

        # ボリンジャーバンド(20, 2σ)  ※Seriesで計算して単列代入
        std20 = c.rolling(20, min_periods=20).std(ddof=0)
        d["BB_up"] = sma20 + 2.0 * std20
        d["BB_dn"] = sma20 - 2.0 * std20

        # 簡易 ADX(14)
        up_move   = h.diff()
        down_move = -l.diff()
        plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        tr1 = (h - l)
        tr2 = (h - c.shift()).abs()
        tr3 = (l - c.shift()).abs()
        tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.ewm(alpha=1/14, adjust=False).mean().replace(0, np.nan)
        pdi = 100 * (plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr)
        mdi = 100 * (minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr)
        dx  = ((pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)) * 100
        d["ADX"] = dx.ewm(alpha=1/14, adjust=False).mean()

        return d

    return add_ta(df_h1), add_ta(df_h4), add_ta(df_d)
# ---------- 置き換えここまで ----------


# ====== ステップ3：本文の下書き（編集可） ======

# ==== 段落①（市況サマリー）自動生成ヘルパー ====
from datetime import datetime, timezone, timedelta

def _jst_now_str():
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime("%Y-%m-%d %H:%M")

def _yf_last_two_daily(ticker: str):
    try:
        import yfinance as yf
        df = yf.download(ticker, period="10d", interval="1d", auto_adjust=False, progress=False)
        if df is None or df.empty or "Close" not in df:
            return None
        close = df["Close"].dropna()
        if len(close) < 2:
            return None
        return float(close.iloc[-2]), float(close.iloc[-1])
    except Exception:
        return None

def _pct_change(prev: float, last: float) -> float:
    if prev == 0 or prev is None or last is None:
        return 0.0
    return (last / prev - 1.0) * 100.0

def _fmt_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.1f}%"

# ==== 段落① 本体：市場スナップショットから1文を生成 ====
def _build_para1_from_market() -> str:
    """
    直近の有効終値(t0) と その一つ前(t-1) で比較し、段落①の1文を組み立てる。
    - 株: ^DJI / ^GSPC / ^IXIC（3指数すべて％を明示）
    - 金利: ^TNX（差×10=bp）→ ±3bp で三分岐
    - 商品: CL=F / NG=F / GC=F のうち “動いた順に上位2つ”
    - 文末に as of JST を付ける
    """

    # 既存ユーティリティが無い場合の軽いフォールバック
    _yf = globals().get("_yf_last_two_daily")
    if not callable(_yf):
        return ""

    _pct_fn = globals().get("_pct")
    if not callable(_pct_fn):
        def _pct_fn(a, b):
            try:
                return (b / a - 1.0) * 100.0
            except Exception:
                return 0.0

    _fmt_pct_fn = globals().get("_fmt_pct")
    if not callable(_fmt_pct_fn):
        def _fmt_pct_fn(p):
            return f"{p:+.1f}%"

    # 1) 株価指数（ダウ / S&P500 / ナスダック）
    dji = _yf("^DJI")
    spx = _yf("^GSPC")
    nas = _yf("^IXIC")

    # ％変化
    dji_pct = _pct_fn(*dji) if dji else None
    spx_pct = _pct_fn(*spx) if spx else None
    nas_pct = _pct_fn(*nas) if nas else None

    # 文頭（営業日想定）判定用カウント（±0.1%未満は FLAT で除外）
    ups = downs = 0
    for p in (dji_pct, spx_pct, nas_pct):
        if p is None:
            continue
        if abs(p) < 0.1:
            continue
        ups += (p > 0)
        downs += (p < 0)

    if ups == 3:
        head = "米国市場は、主要3指数がそろって上昇となった。"
    elif downs == 3:
        head = "米国市場は、主要3指数がそろって下落となった。"
    elif ups == 2 and downs == 1:
        head = "米国市場は、主要3指数のうち2指数が上昇・1指数が下落となった。"
    elif ups == 1 and downs == 2:
        head = "米国市場は、主要3指数のうち1指数が上昇・2指数が下落となった。"
    else:
        head = "米国市場は、指標ごとに強弱が分かれた。"

    # 株3指数はすべて％を明示
    parts_idx = []
    if dji_pct is not None:
        parts_idx.append(f"ダウは{_fmt_pct_fn(dji_pct)}")
    if spx_pct is not None:
        parts_idx.append(f"S&P500は{_fmt_pct_fn(spx_pct)}")
    if nas_pct is not None:
        parts_idx.append(f"ナスダックは{_fmt_pct_fn(nas_pct)}")
    part_idx = ("、".join(parts_idx) + "。") if parts_idx else ""

    # 2) 米10年金利（^TNX → bp）
    part_tnx = ""
    delta_bp = None
    tnx = _yf("^TNX")
    if tnx:
        try:
            delta_bp = (tnx[1] - tnx[0]) * 10.0
            if delta_bp > 3:
                part_tnx = f"米10年金利は+{int(round(delta_bp))}bpと上昇。"
            elif delta_bp < -3:
                part_tnx = f"米10年金利は{int(round(delta_bp))}bpと低下。"
            else:
                part_tnx = "米10年金利は概ね横ばい。"
        except Exception:
            part_tnx = "米10年金利は概ね横ばい。"

    # 3) 地合いの小結（株×金利）
    if ups == 3 and (delta_bp is not None) and (delta_bp < -3):
        mood = "株高・金利安の流れが意識されやすい。"
    elif downs == 3 and (delta_bp is not None) and (delta_bp > 3):
        mood = "株安・金利高のムードが意識されやすい。"
    else:
        mood = "方向性は限定的。"

    # 4) 商品：動いた順に上位2つ（WTI / 天然ガス / 金）
    movers = []
    wti = _yf("CL=F")
    ng = _yf("NG=F")
    gold = _yf("GC=F")

    if wti:
        movers.append(("原油WTI", _pct_fn(*wti), wti[1]))
    if ng:
        movers.append(("天然ガス", _pct_fn(*ng), None))
    if gold:
        movers.append(("金", _pct_fn(*gold), gold[1]))

    movers = [(n, p, last) for (n, p, last) in movers if p is not None]
    movers.sort(key=lambda x: abs(x[1]), reverse=True)

    part_com = ""
    if movers:
        chunks = []
        for name, p, last in movers[:2]:
            if name == "原油WTI" and (last is not None):
                chunks.append(f"{name}は{last:.1f}ドル付近（{_fmt_pct_fn(p)}）")
            else:
                chunks.append(f"{name}は{_fmt_pct_fn(p)}")
        part_com = " " + "、".join(chunks) + "。"

    # 5) 本日のポイント（Step5の2件があれば軽く言及）
    part_pts = ""
    try:
        pts = list(st.session_state.get("points_tags_v2", []) or [])[:2]
        if len(pts) == 2:
            part_pts = f"本日は{pts[0]}と{pts[1]}が控えており、短期の振れに留意したい。"
    except Exception:
        part_pts = ""

    # 6) as of（JST）
    asof_fn = globals().get("_jst_now_str")
    if callable(asof_fn):
        asof = asof_fn()
    else:
        from datetime import datetime, timezone, timedelta
        jst = timezone(timedelta(hours=9))
        asof = datetime.now(jst).strftime("%Y-%m-%d %H:%M JST")

    # 連結（句点の重複を避けて安全に）
    text = " ".join([head, part_idx, part_tnx, mood]).strip()
    if text and not text.endswith("。"):
        text += "。"
    text += part_com
    text = text.replace("。。", "。").strip()
    if part_pts:
        if not text.endswith("。"):
            text += "。"
        text += " " + part_pts
    text += f"（as of {asof}）"
    return text


from datetime import date as _date_cls
refresh_calendar_from_fxon(_date_cls.today())

st.markdown("### ステップ3：本文の下書き（編集可）")


# ---- 文章クリーン（安全フォールバック）-----------------------------
if "_clean_text_jp_safe" not in globals():
    import re
    def _clean_text_jp_safe(text: str) -> str:
        s = str(text or "")
        # 全角スペース→半角、タブ/連続空白の圧縮
        s = s.replace("\u3000", " ")
        s = re.sub(r"[ \t]+", " ", s)
        # 句読点の前の余分な空白を除去
        s = re.sub(r"\s+([、。])", r"\1", s)
        s = s.strip()
        # 末尾が句点で終わらない場合は句点を付ける（断定にならない範囲）
        if s and not s.endswith(("。", "！", "？")):
            s += "。"
        return s
# -------------------------------------------------------------------


import re, unicodedata

# ---- 設定の最小文字数（CFGが無くても動く）----
try:
    _tg = (CFG.get("text_guards") or {})
    P1_MIN = int(_tg.get("p1_min_chars", 220))
    P2_MIN = int(_tg.get("p2_min_chars", 180))
except Exception:
    P1_MIN, P2_MIN = 220, 180

# ---- ヘルパ ----

def _pair_to_symbol(p):
    m = {
        "ポンド円": "GBPJPY=X", "ドル円": "JPY=X", "ユーロ円": "EURJPY=X",
        "豪ドル円": "AUDJPY=X", "NZドル円": "NZDJPY=X",
    }
    return m.get(p or "", "GBPJPY=X")

def _default_para2_for_safe(p):
    # _default_para2_for が無ければ簡易テンプレ
    if "default_para2_for" in globals():
        return default_para2_for(p)
    if "_default_para2_for" in globals():
        try:
            return _default_para2_for(p)
        except Exception:
            pass
    pair_name = p or "ポンド円"
    return (f"為替市場では、{pair_name}は短期のテクニカルが意識されやすい。"
            "1時間足の移動平均やボリンジャーバンドの反応を確認しつつ、"
            "4時間足・日足の200SMA/EMA近辺では押し戻りが鈍りやすい。")

def _mentions_points(s, items):
    if not s or not items:
        return True
    body = str(s).replace(" ", "")
    for it in items:
        key = (str(it).split("に", 1)[-1] or "").replace(" ", "")
        if key and key in body:
            return True
    return False

def _pad_para2_base(para2, min_chars):
    """段落②が短いとき、中立で安全な補助文を締めの直前に挿入して必ず min_chars 以上にする。"""
    try:
        s = _clean_text_jp_safe(str(para2 or "").strip())
    except Exception:
        s = str(para2 or "").strip()

    if s and not s.endswith("。"):
        s += "。"

    def L(x: str) -> int:
        return len(str(x).replace("\n", ""))

    if L(s) >= min_chars:
        return s

    # 補助文（非断定・中立／締め文ではない）
    fillers = [
        "移動平均線周辺の反応を確かめつつ様子を見たい。",
        "節目水準の手前では反応がぶれやすい。",
        "指標通過後の方向性を見極めたい。",
        "短期は値動きの粗さに留意したい。",
    ]

    # 決定論的に開始位置をずらす（同じ日×同じペアでは同じ順）
    try:
        pair = str(st.session_state.get("pair", "") or "")
        asof = str(st.session_state.get("asof_date", "")) or __import__("datetime").date.today().isoformat()
        import hashlib
        start_idx = int(hashlib.md5(f"{pair}|{asof}|pad".encode("utf-8")).hexdigest(), 16) % len(fillers)
    except Exception:
        start_idx = 0

    used = set()
    i = 0
    # 最大2文まで追加して必ず180字以上に到達させる
    while L(s) < min_chars and i < len(fillers) * 2:
        cand = fillers[(start_idx + i) % len(fillers)]
        i += 1
        if cand in s or cand in used:
            continue
        # 締め文の手前に入れるのが理想だが、ここでは文末に自然に追加（締めは別段で整形される）
        if s and not s.endswith("。"):
            s += "。"
        s += cand
        used.add(cand)

    try:
        s = _clean_text_jp_safe(s)
    except Exception:
        pass
    return s


# ---- Step5で選んだポイント（最大2件）----
points_items = list(st.session_state.get("points_tags_v2", []) or [])[:2]

# ===== 段落① =====
# 段落①の初期文を“実データで自動生成”。失敗/空なら安全テンプレにフォールバック
try:
    default_para1 = _build_para1_from_market()
except Exception:
    default_para1 = ""
# --- NEW: 段落①の as-of を本文から取り外し、別保管（UI用） ---
import re
m = re.search(r"（as of ([^)]+)）$", str(default_para1))
if m:
    st.session_state["p1_asof_jst"] = m.group(1)      # 例: "2025-08-21 01:14"
    default_para1 = str(default_para1)[:m.start()].rstrip()
else:
    st.session_state["p1_asof_jst"] = None
# --- ここまで ---

# 万一、取得に失敗して空なら簡易テンプレを使用（空のテキストエリアを防止）
if not default_para1:
    default_para1 = (
        "米国市場は、指標ごとに強弱が分かれた。米10年金利は概ね横ばい。"
        "方向性は限定的。"
    )

# === Step5: 祝日/休場の自動注記を段落①の本文先頭に1行だけ合成 ===
# ここでは Step2 で保存した st.session_state["intro_overlay_text"] を使います。
# 合成先の変数名が default_para1 でない場合は、下の2行の変数名だけ合わせてください。
_overlay = (st.session_state.get("intro_overlay_text") or "").strip()
if _overlay:
    # 句点で終わっていなければ付ける（日本語の自然さを担保）
    if not _overlay.endswith("。"):
        _overlay += "。"
    # 既存の段落①本文の先頭に重ねる（1行だけ）
    default_para1 = (_overlay + default_para1.lstrip())
# === Step5 ここまで ===

# ---- Step5で選んだポイント（最大2件）----
points_items = list(st.session_state.get("points_tags_v2", []) or [])[:2]


# === Step6: 段落①の最低文字数ガード（220字未満ならやさしく補完） ===
_MIN_P1 = 220

def _pad_para1(text: str, target: int = _MIN_P1) -> str:
    """事実を追加せず、一般的で非断定の一文を重ねて規定字数に到達させる。"""
    base = (text or "").strip()
    if len(base) >= target:
        return base
    # 句点でいったん整える
    if base and not base.endswith("。"):
        base += "。"
    # 機械臭を抑えた汎用の追記候補（事実を断定しない・売買示唆なし）
    fillers = [
        "指標ごとに強弱が分かれるなか、過度な一方向は決めつけにくい。",
        "短期的にはヘッドラインや時刻要因による振れに留意したい。",
        "エネルギーや貴金属の動きも含めて全体の方向感は当面限定的となりやすい。",
    ]
    i = 0
    while len(base) < target and i < len(fillers):
        base += fillers[i]
        if not base.endswith("。"):
            base += "。"
        i += 1
    # まだ足りなければ、極めて穏当な一文で埋める
    while len(base) < target:
        base += "続く値動きの反応を確かめたい。"
    return base


default_para1 = _pad_para1(default_para1, _MIN_P1)
# === Step6 ここまで ===
# === Step8: 「株・金利・原油」の三点まとめを自動生成し、冒頭1文を差し替え ===
import re as _re  # 既にimport済みでもOK

def _pick_sign(val_str: str | None) -> int | None:
    """'+1'上昇 / '-1'下落 / '0'横ばい / None=判定不可"""
    if val_str is None:
        return None
    try:
        v = float(val_str)
        return 1 if v > 0 else (-1 if v < 0 else 0)
    except Exception:
        return None

def _label_from_sign(maj: int, pos_word: str, neg_word: str, flat_word: str) -> str:
    if maj > 0:
        return pos_word
    if maj < 0:
        return neg_word
    return flat_word

def _majority_label(signs: list[int | None], tie_label: str) -> int:
    """+1/-1/0 の多数決（Noneは無視）。同数・全Noneなら0（横ばい）"""
    s = [x for x in signs if x is not None]
    if not s:
        return 0
    pos = sum(1 for x in s if x > 0)
    neg = sum(1 for x in s if x < 0)
    if pos > neg:
        return 1
    if neg > pos:
        return -1
    return 0

def _build_three_points_line(p1_text: str) -> str:
    """段落①本文（既存の列挙）から、冒頭の要約1文を作る。"""
    text = p1_text or ""

    # 1) 株価（ダウ／S&P500／ナスダック）の±%を拾う
    m_dj  = _re.search(r"ダウは([+\-]?\d+(?:\.\d+)?)%", text)
    m_sp  = _re.search(r"S&P500は([+\-]?\d+(?:\.\d+)?)%", text)
    m_ndx = _re.search(r"ナスダックは([+\-]?\d+(?:\.\d+)?)%", text)
    s_stock = [
        _pick_sign(m_dj.group(1) if m_dj else None),
        _pick_sign(m_sp.group(1) if m_sp else None),
        _pick_sign(m_ndx.group(1) if m_ndx else None),
    ]
    # 全て上昇/下落のときは「主要3指数がそろって…」
    lead = ""
    if all(x == 1 for x in s_stock if x is not None) and any(x is not None for x in s_stock):
        lead = "主要3指数がそろって上昇となり、"
    elif all(x == -1 for x in s_stock if x is not None) and any(x is not None for x in s_stock):
        lead = "主要3指数がそろって下落となり、"
    elif any(x is not None for x in s_stock):
        lead = "主要3指数はまちまちとなり、"

    stock_maj = _majority_label(s_stock, tie_label="横ばい")
    stock_label = _label_from_sign(stock_maj, "株高", "株安", "株価横ばい")

    # 2) 金利（テキストから語で判定：上昇/低下/横ばい）
    rate_label = "金利横ばい"
    if _re.search(r"金利.*?(上昇|高)", text):
        rate_label = "金利高"
    elif _re.search(r"金利.*?(低下|下落|安)", text):
        rate_label = "金利安"
    elif _re.search(r"金利.*?横ばい", text):
        rate_label = "金利横ばい"

    # 3) 原油WTI（±%を拾う→高/安/横ばい）
    m_wti = _re.search(r"原油WTIは.*?([+\-]?\d+(?:\.\d+)?)%", text)
    wti_sign = _pick_sign(m_wti.group(1) if m_wti else None)
    wti_label = _label_from_sign(
        0 if wti_sign is None else wti_sign, "原油高", "原油安", "原油横ばい"
    )

    # 文の組み立て（サンプル文体）
    line = f"米国市場は、{lead}{stock_label}・{rate_label}・{wti_label}の流れ。"
    return line

def _inject_three_points_line(p1_text: str) -> str:
    """本文の先頭1文を、三点まとめに差し替え or 先頭に付加"""
    base = (p1_text or "").strip()
    summary = _build_three_points_line(base)

    # 先頭が「米国市場は」で始まるなら、最初の句点までを置き換え
    if base.startswith("米国市場は"):
        idx = base.find("。")
        if idx != -1:
            return summary + base[idx+1:]
        return summary
    # それ以外なら先頭に付ける
    return summary + base

default_para1 = _inject_three_points_line(default_para1)
# === Step8 ここまで ===

# === Step7: 日本語の句読点後の半角スペースを自動で詰める（見た目の自然さ向上） ===
import re as _re  # 既に上で import 済みでも問題ありません
def _jp_tighten_spaces(s: str) -> str:
    if not s:
        return s
    s = _re.sub(r'([。、「」])\s+', r'\1', s)  # 句読点の直後の半角スペースを除去
    s = _re.sub(r'\s{2,}', ' ', s)            # 連続スペースは1つに
    return s
default_para1 = _jp_tighten_spaces(default_para1)
# === Step7 ここまで ===
# === Step9: 段落①の重複フレーズを1回に揃える（機械臭の低減） ===
def _dedupe_p1_lines(s: str) -> str:
    if not s:
        return s
    # 繰り返しがちで意味が重複する注意文をターゲットにする（事実文には触れない）
    phrases = [
        "短期の振れに留意したい。",
        "過度な一方向は決めつけにくい。",
        "短期的にはヘッドラインや時刻要因による振れに留意したい。",
        "続く値動きの反応を確かめたい。",  # ← 追加
    ]
    # 連続スペースの正規化（安全策）
    s = s.replace("  ", " ")
    for p in phrases:
        first = s.find(p)
        if first == -1:
            continue
        # 先頭の1回は残し、それ以降の同一文は削除
        tail = s[first + len(p):].replace(p, "")
        s = s[:first + len(p)] + tail
    return s

default_para1 = _dedupe_p1_lines(default_para1)
# === Step9 ここまで ===

# === Step10: 商品フレーズの微整形（「商品は …」にまとめる） ===
import re as _re  # 既にimport済みならそのまま

def _format_commodities_line(s: str) -> str:
    if not s:
        return s
    text = s

    # 1) 「天然ガスはA%、金はB%。」 → 「商品は天然ガスがA%、金がB%。」
    #   - 末尾が句点で終わらない場合も考慮して安全に置換
    pattern_ng_gold = _re.compile(r"(天然ガスは([+\-]?\d+(?:\.\d+)?)%、金は([+\-]?\d+(?:\.\d+)?)%)(。)?")
    def _repl_ng_gold(m):
        a = m.group(2); b = m.group(3)
        return f"商品は天然ガスが{a}%、金が{b}%。"
    text = pattern_ng_gold.sub(_repl_ng_gold, text)

    # 2) 逆順「金はB%、天然ガスはA%。」にも対応
    pattern_gold_ng = _re.compile(r"(金は([+\-]?\d+(?:\.\d+)?)%、天然ガスは([+\-]?\d+(?:\.\d+)?)%)(。)?")
    def _repl_gold_ng(m):
        b = m.group(2); a = m.group(3)
        return f"商品は天然ガスが{a}%、金が{b}%。"
    text = pattern_gold_ng.sub(_repl_gold_ng, text)

    # 3) 不要な二重句点を防止
    text = _re.sub(r"。。", "。", text)
    return text

default_para1 = _format_commodities_line(default_para1)
# === Step10 ここまで ===
# === Step11: 冒頭の導入句「昨日は/先週末は」を自動で付与（祝日注記がある日はスキップ） ===
from datetime import datetime, timezone, timedelta

def _prepend_lead_phrase_to_p1(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text

    # すでに祝日/休場の自動注記を合成済みなら、その文頭を尊重して何もしない
    overlay = (st.session_state.get("intro_overlay_text") or "").strip()
    if overlay:
        return text

    now_jst = datetime.now(timezone(timedelta(hours=9)))
    lead = "先週末は" if now_jst.weekday() == 0 else "昨日は"  # 月曜のみ「先週末は」、それ以外は「昨日は」

    # 典型の文頭「米国市場は、」を「昨日は米国市場は、」に変換
    # すでに「昨日は」「先週末は」で始まっている場合は何もしない
    if text.startswith(("昨日は", "先週末は")):
        return text

    import re as _re
    pattern = _re.compile(r"^(米国市場は[、，])")
    new_text, n = pattern.subn(lead + r"\1", text, count=1)
    return new_text if n > 0 else (lead + text)

default_para1 = _prepend_lead_phrase_to_p1(default_para1)
# === Step11 ここまで ===
# === Step12: 導入句（二重化）を強制的に1文へ統一 ===
import re as _re  # 既にimport済みでもOK

def _collapse_double_lead(s: str) -> str:
    if not s:
        return s
    text = s

    # ケースA: 「米国市場は…。」の直後に「昨日は米国市場は…」or「先週末は米国市場は…」
    # -> 先頭の「米国市場は…。」を削除し、導入句つきだけを残す
    text = _re.sub(
        r'^米国市場は[、，][^。]*。(?:\s*)(?=(?:昨日は|先週末は)米国市場は[、，])',
        '',
        text
    )

    # ケースB: 逆に、冒頭が「昨日は米国市場は…」の直後にもう一度「米国市場は…」が続く
    # -> 2つ目の「米国市場は…」を削除
    text = _re.sub(
        r'^(?:昨日は|先週末は)米国市場は[、，][^。]*。(?:\s*)米国市場は[、，]',
        lambda m: m.group(0).split('。')[0] + '。',  # 1つ目の文だけ残す
        text
    )

    # 仕上げ：句読点直後スペースなどの体裁を再調整（既存関数があれば使う）
    try:
        text = _jp_tighten_spaces(text)
    except Exception:
        pass
    return text

default_para1 = _collapse_double_lead(default_para1)
# === Step12 ここまで ===
# === Step13: 商品の水準を簡潔に追記（WTI=XX.Xドル、金=X,XXXドル台） ===
import math
import yfinance as yf

@st.cache_data(ttl=1800)
def _yf_last_close(ticker: str) -> float | None:
    try:
        h = yf.Ticker(ticker).history(period="10d", interval="1d")
        if h is None or h.empty:
            return None
        close = h["Close"].dropna()
        return float(close.iloc[-1]) if not close.empty else None
    except Exception:
        return None

def _gold_band(v: float) -> str:
    # 100ドル刻みで「X,XXXドル台」表現
    band = int(math.floor(v / 100.0) * 100)
    return f"{band:,}ドル台"

def _append_commodity_levels(text: str) -> str:
    if not text:
        return text
    # すでに水準文が入っているなら何もしない
    if ("ドル台" in text) or ("WTIは" in text and "ドル" in text):
        return text

    # 取得（失敗時は None）
    wti = _yf_last_close("CL=F")   # 原油WTI先物
    gold = _yf_last_close("GC=F")  # 金先物（$/oz）

    if wti is None and gold is None:
        return text

    parts = []
    if wti is not None:
        parts.append(f"WTIは{wti:.1f}ドル")
    if gold is not None:
        parts.append(f"金は{_gold_band(gold)}")

    return text.rstrip() + " " + "、".join(parts) + "。"

default_para1 = _append_commodity_levels(default_para1)
# === Step13 ここまで ===


import re as _re  # 既にimport済みならそのまま

_FLAT_TH = 0.1  # %単位（必要なら設定に出せます）

def _apply_flat_label_to_text(s: str, th: float = _FLAT_TH) -> str:
    """±th%未満を「横ばい」に言い換える。該当しなければ原文のまま。"""
    if not s:
        return s
    def _rep(m):
        name, val = m.group(1), float(m.group(2))
        return f"{name}は横ばい" if abs(val) < th else m.group(0)
    # 株3指数
    s = _re.sub(r"(ダウ|S&P500|ナスダック)は([+\-]?\d+(?:\.\d+)?)%", _rep, s)
    # コモディティ（本文に%が出るケース）
    s = _re.sub(r"(原油WTI|天然ガス|金)は([+\-]?\d+(?:\.\d+)?)%", _rep, s)
    return s

def _sign_from_token(text: str, name: str) -> int | None:
    """+1/-1/0(None=不明)。横ばい語も検出。"""
    # 数値%優先
    m = _re.search(fr"{_re.escape(name)}は([+\-]?\d+(?:\.\d+)?)%", text)
    if m:
        v = float(m.group(1))
        if abs(v) < _FLAT_TH:
            return 0
        return 1 if v > 0 else -1
    # 「横ばい」語
    if _re.search(fr"{_re.escape(name)}は横ばい", text):
        return 0
    return None

def _majority_label(signs: list[int | None]) -> int:
    vals = [x for x in signs if x is not None]
    if not vals:
        return 0
    pos = sum(1 for x in vals if x > 0)
    neg = sum(1 for x in vals if x < 0)
    if pos > neg:
        return 1
    if neg > pos:
        return -1
    return 0

def _rebuild_three_points_flat(p1_text: str) -> str:
    """三点まとめをFLAT対応で作り直し、先頭1文に反映。"""
    import re as _re  # 念のためローカルimport（上でimport済みでもOK）

    base = (p1_text or "").strip()

    # ★自己参照回避：既に入っている要約文「（昨日は/先週末は）米国市場は、…の流れ。」を
    #   判定用テキストから一度だけ除去し、純粋に本文の記述だけで再判定する
    scan_base = _re.sub(r'^(?:昨日は|先週末は)?米国市場は[、，][^。]*。', '', base)

    # 1) 株（本文の実数/横ばい語から判定）
    s_stock = [
        _sign_from_token(scan_base, "ダウ"),
        _sign_from_token(scan_base, "S&P500"),
        _sign_from_token(scan_base, "ナスダック"),
    ]
    lead = ""
    if all(x == 1 for x in s_stock if x is not None) and any(x is not None for x in s_stock):
        lead = "主要3指数がそろって上昇となり、"
    elif all(x == -1 for x in s_stock if x is not None) and any(x is not None for x in s_stock):
        lead = "主要3指数がそろって下落となり、"
    elif any(x is not None for x in s_stock):
        lead = "主要3指数はまちまちとなり、"

    stock_maj = _majority_label(s_stock)
    stock_label = "株高" if stock_maj > 0 else ("株安" if stock_maj < 0 else "株価横ばい")

    # 2) 金利（まず本文の「米10年金利は…」系を優先して判定、なければ語ベース）
    rate_label = "金利横ばい"
    if _re.search(r"米?10年金利.*?(低下|下落|-?\d+\s?bp|−?\d+\s?bp)", scan_base):
        # 低下語や -Nbp があれば「金利安」
        rate_label = "金利安"
    elif _re.search(r"米?10年金利.*?(上昇|\+?\d+\s?bp|＋?\d+\s?bp)", scan_base):
        rate_label = "金利高"
    elif _re.search(r"米?10年金利.*?横ばい|概ね横ばい", scan_base):
        rate_label = "金利横ばい"
    else:
        # フォールバック（一般語）：高/安/横ばい を拾う
        if _re.search(r"金利.*?(上昇|高)", scan_base):
            rate_label = "金利高"
        elif _re.search(r"金利.*?(低下|下落|安)", scan_base):
            rate_label = "金利安"
        elif _re.search(r"金利.*?横ばい|概ね横ばい", scan_base):
            rate_label = "金利横ばい"

    # 3) 原油WTI（%または横ばい語）
    wti_sign = _sign_from_token(scan_base, "原油WTI")
    if wti_sign is None:
        wti_label = "原油横ばい"
    else:
        wti_label = "原油高" if wti_sign > 0 else ("原油安" if wti_sign < 0 else "原油横ばい")

    summary = f"米国市場は、{lead}{stock_label}・{rate_label}・{wti_label}の流れ。"

    # 置換ルール：
    # A) 先頭が「米国市場は」で始まる → 最初の句点まで差し替え
    if base.startswith("米国市場は"):
        idx = base.find("。")
        return (summary + base[idx+1:]) if idx != -1 else summary

    # B) 先頭が「昨日は/先週末は」+「米国市場は」で始まる → 導入句を保持したまま差し替え
    m = _re.match(r"^(昨日は|先週末は)(米国市場は[、，])", base)
    if m:
        prefix = m.group(1)  # 「昨日は」or「先週末は」
        core = summary.replace("米国市場は、", "", 1)  # summary のコア部分
        idx = base.find("。")  # 先頭文の終わり
        tail = base[idx+1:] if idx != -1 else ""
        return f"{prefix}米国市場は、{core}{tail}"

    # C) それ以外 → 先頭に要約を付加
    return summary + base



# 1) 本文の±0.1%未満を「横ばい」に
default_para1 = _apply_flat_label_to_text(default_para1, _FLAT_TH)
# 2) 先頭の三点まとめをFLAT対応で作り直す
default_para1 = _rebuild_three_points_flat(default_para1)
# 3) 仕上げに句読点スペースを再調整（既存の関数を再利用）
try:
    default_para1 = _jp_tighten_spaces(default_para1)
except Exception:
    pass
# === Step11 ここまで ===

# ★★★ ここに1行だけ追加 ★★★
default_para1 = _apply_flat_label_to_text(default_para1, _FLAT_TH)

# 段落①の入力欄（この行は既に入っていてOK）
para1_input = st.text_area("段落①（市況サマリー）", value=default_para1, height=160, key="p1")

# --- 段落① as-of の小さな表示（本文外） ---
_asof = st.session_state.get("p1_asof_jst")
if _asof:
    st.caption(f"※ このレポートの市場データは、日本時間{_asof} 時点の情報です。")
# --- ここまで ---



# ①にポイントの“完全一致”が混入していれば軽く除去
def _dedup_points_in_para(text, points):
    s = text or ""
    for p in points:
        p = (p or "").strip()
        if not p:
            continue
        s = s.replace(p, "")
    return s.replace("。。", "。").replace("  ", " ").strip()

para1 = _dedup_points_in_para(para1_input, points_items)

# ===== 段落②（ライブ生成＋テンプレ）=====
# 表示方針：ここはユーザーにも見せる軽い進捗表示（数値は本文に未使用）。
# もし運用で不要なら show_debug に連動させて非表示化する予定。

st.markdown("##### 📡 段落②のライブ生成（精密判定）")
use_live = st.checkbox(
    "市場データに基づいて②の初期文を自動生成（失敗時はテンプレにフォールバック）",
    value=True, key="use_live_v2"
)

pair_name = str(globals().get("pair", "") or "ポンド円")
live_diag = {}

if use_live:
    # (1) 既存のライブ生成（存在すれば）
    try:
        fn = globals().get("_para2_from_live")
        if callable(fn):
            seed, diag = fn(pair_name, CFG)
            live_diag = diag or {}
            if seed:
                default_para2_seed = seed
                last_ts = (live_diag or {}).get("last_ts")
                ticker  = (live_diag or {}).get("ticker")
                st.success(f"ライブ生成に成功：{ticker}（最終データ時刻: {last_ts}）")
            else:
                st.info("ライブ生成は未使用/失敗のため、安全なテンプレを使用します。")
    except Exception as e:
        st.warning(f"ライブ生成でエラー（テンプレへ）：{e}")

    # (2) yfinance ベースのテクニカル（ta_block）
    try:
        sym = _pair_to_symbol(pair_name)
        ta_fn = globals().get("ta_block")
        if callable(ta_fn):
            df_h1, df_h4, df_d = ta_fn(symbol=sym, days=90)
            if df_h1 is not None and not df_h1.empty:
                adx_h1 = float(df_h1["ADX"].iloc[-1])
                bb_w   = float((df_h1["BB_up"].iloc[-1] - df_h1["BB_dn"].iloc[-1]) / df_h1["Close"].iloc[-1])
                st.caption(f"TA診断: ADX(H1)={adx_h1:.1f}, BB幅={bb_w:.2%}")
                try:
                    last_ts = df_h1.index[-1]
                    if isinstance(live_diag, dict):
                        live_diag.update({
                            "ticker": sym,
                            "ADX": round(adx_h1, 1),
                            "BB幅%": round(bb_w * 100, 2),
                            "last_ts": f"{last_ts}",
                        })
                except Exception:
                    pass
    except Exception as e:


        st.warning(f"テクニカル計算でエラー: {e}")
# ▼ NEW: 下書きv2.1を入力欄に反映（未生成なら従来seedを維持）
# SOT: プレビュー確定文 > v21 を優先し、句点を1つに正規化してクリーン
default_para2_seed = _clean_text_jp_safe(str((globals().get("_para2_preview") or para2_seed_v21)).strip().rstrip("。") + "。")


# テキストエリア（ユーザー編集を受ける）
para2_raw = st.text_area("段落②（為替テクニカル）",
                         value=default_para2_seed, height=140, key=f"p2_{pair_name}")

# 用語整合（関数が存在する場合のみ）
try:
    regime_from = globals().get("_regime_from_diag")
    enforce_fn  = globals().get("_enforce_regime_language")
    if callable(regime_from) and callable(enforce_fn):
        current_regime = regime_from(live_diag)
        if current_regime:
            fixed, flags = enforce_fn(para2_raw, current_regime)
            if flags:
                st.info("用語を自動整合しました：" + " / ".join(flags))
            para2_raw = fixed
except Exception:
    pass
# === StepP2-1: 段落②の整形（重複フレーズ削除＋句読点スペース詰め） ===
def _dedupe_p2_lines(s: str) -> str:
    if not s:
        return s
    # 繰り返しやすい締め文は先頭の1回だけ残す
    phrases = [
        "過度な方向感は決めつけない構えとしたい。",
        "行方を注視したい。",
        "値動きには警戒したい。",
        "方向感を見極めたい。",
        "反応を確かめたい。",
    ]
    s = s.replace("  ", " ")
    for p in phrases:
        first = s.find(p)
        if first == -1:
            continue
        tail = s[first + len(p):].replace(p, "")
        s = s[:first + len(p)] + tail
    # 句読点直後スペースの除去（Step7で定義済み）
    try:
        s = _jp_tighten_spaces(s)
    except Exception:
        pass
    return s

# 段落②の最終整形
para2_raw = _dedupe_p2_lines(str(para2_raw or ""))
# === StepP2-1 ここまで ===
# === StepP2-1b: ブレークポイント注記の追加（任意） ===
try:
    # UI側で設定された値を利用（0やNoneは無視）
    bp_up_input = st.session_state.get("bp_up")
    bp_dn_input = st.session_state.get("bp_dn")
    apply_mode  = st.session_state.get("bp_axis") or "auto"

    # どちらか片側でも設定があれば試す
    if (bp_up_input or bp_dn_input):
        if callable(globals().get("_choose_breakpoints")) and callable(globals().get("_bp_phrase")):
        # ★ 引数なしで呼ぶ（戻り値は"付近"まで整形済みの文字列2つ）
            up_txt, dn_txt, _ = _choose_breakpoints()

        # ★ RSIやGC等の印象（なければ中立に）を渡す
        d1_imp = st.session_state.get("d1_imp", "横ばい")
        h4_imp = st.session_state.get("h4_imp", "横ばい")

        # ★ 日本語の定型文へ（片側のみでもOK）
        bp_line = _bp_phrase(up_txt, dn_txt, d1_imp, h4_imp)

        # ★ 既に同趣旨の文がないときだけ追記（重複防止）
        if bp_line and (bp_line not in para2_raw):
            para2_raw = (para2_raw.rstrip("。") + " " + bp_line).strip()
except Exception:
    pass
# === StepP2-1b ここまで ===

# 句点で終わるよう整形
para2 = str(para2_raw or "").strip()
if para2 and not para2.endswith("。"):
    para2 += "。"

# ===== タイトル（AI/ローカル）=====
try:
    title_tail_ai = choose_title_tail(para1, para2)
except Exception:
    title_tail_ai = "注視か"

def _default_title_for_safe(p, tail):
    if "_default_title_for" in globals():
        try:
            return _default_title_for(p, tail)
        except Exception:
            pass
    # tail が「注視か/警戒か/静観か/要注意か」に該当しない場合は “方向感を見極めたい”
    tail = str(tail or "")
    if tail.endswith(("注視か", "警戒か", "静観か", "要注意か")):
        return f"{p or 'ポンド円'}の方向感に{tail}"
    return f"{p or 'ポンド円'}の方向感を見極めたい"

default_title = _default_title_for_safe(pair_name, title_tail_ai)
title = st.text_input("タイトル（最後は回収します）", value=default_title, key=f"title_{pair_name}")


# --- 追加：①の最小文字数を安全に満たす（未定義なら定義）---
if "_pad_para1_base" not in globals():
    def _pad_para1_base(text: str, min_chars: int = 220) -> str:
        # まず軽く整形
        try:
            s = _clean_text_jp_safe(text)
        except Exception:
            s = str(text or "")

        def _len_no_nl(t: str) -> int:
            return len("".join(str(t).splitlines()))

        if _len_no_nl(s) >= min_chars:
            return s

        # 断定を避けた薄い補足文を順に追加（重複は避ける）
        fillers = [
            "指標ごとの強弱が混在し、方向性は限定的となりやすい。",
            "短期は過度な断定を避け、ヘッドラインの一報に左右されやすい。",
            "イベント前後は一時的な振れもあり、値動きの行き過ぎには注意したい。",
        ]
        for f in fillers:
            if _len_no_nl(s) >= min_chars:
                break
            if f not in s:
                if s and not s.endswith(("。", "！", "？")):
                    s += "。"
                s += " " + f

        # まだ足りなければ安全な定型文で埋める
        while _len_no_nl(s) < min_chars:
            s += " 市場は材料待ちとなりやすく、短期の振れに留意したい。"

        return s

# ===== ① 最低文字数を担保＋ポイント一言（重複防止） =====
import re

# ①の安全初期化（for_build > ui > "" の優先）
para1_build = (
    globals().get("para1_for_build")
    or globals().get("para1")
    or ""
)
para1_build = _clean_text_jp_safe(str(para1_build))

# 最低文字数を担保
para1_build = _pad_para1_base(para1_build, P1_MIN)

# Step5 の選択ポイント（最大2件）を取得
points_items = list(st.session_state.get("points_tags_v2", []) or [])[:2]

# ①内で既に触れているかを判定
def _mentions_points(s: str, items: list[str]) -> bool:
    if not s or not items:
        return True
    body = str(s).replace(" ", "")
    for it in items:
        key = (it.split("に", 1)[-1] or "").replace(" ", "")
        if key and key in body:
            return True
    return False

# 触れていなければ一言だけ自動挿入（似た文があれば除去してから）
if points_items and not _mentions_points(para1_build, points_items):
    hint = "本日は" + "と".join(points_items) + "が控えており、短期の振れに留意したい。"
    para1_build = re.sub(r"本日は[^。]*?留意したい。", "", str(para1_build)).strip()
    if para1_build and not para1_build.endswith("。"):
        para1_build += " "
    para1_build += hint

# 最後にもう一度クリーン
para1_build = _clean_text_jp_safe(para1_build)

# ★段落①：最終ガード（重複・体裁・用語ロック）
#   - 「米国市場は、主要3指数が…」の重複や句読点の重複、用語表記ゆれをここで吸収
para1_build = _final_polish_and_guard(para1_build, para="p1")

# ===== ② SOTを尊重：session_state["para2"] 優先で取得 → 句点正規化＋最低文字数 =====
_base_p2 = str(st.session_state.get("para2") or globals().get("para2") or "")
para2_build = _clean_text_jp_safe(_base_p2).strip().rstrip("。") + "。"
para2_build = _pad_para2_base(para2_build, P2_MIN)  # 結びの固定はStep6が担当

# ---- session_state へ格納（Step6 がこの2つを優先して使う）----
st.session_state["para1_for_build"] = para1_build
st.session_state["para2_for_build"] = para2_build
st.session_state["default_title"]   = default_title  # Step6の見出し用に共有



# ---- 文字数（“実効テキスト”基準で表示）----
len_p1 = len(str(para1_build).replace("\n", ""))
len_p2 = len(str(para2_build).replace("\n", ""))
st.caption(f"段落① 文字数: {len_p1} / 最低 {P1_MIN} 字（※for_build基準）")
st.caption(f"段落② 文字数: {len_p2} / 最低 {P2_MIN} 字（※for_build基準）")

st.markdown("---")



# ---- 内部診断（安全に表示）----
with st.expander("内部診断（出力本文には含めません）", expanded=False):
    if live_diag:
        st.write({
            "ticker": live_diag.get("ticker"),
            "regime": live_diag.get("regime"),
            # 可能なら両表記に対応
            "ADX": live_diag.get("ADX") if "ADX" in live_diag else live_diag.get("adx"),
            "BB幅%": live_diag.get("BB幅%") if "BB幅%" in live_diag else live_diag.get("bb_width_pct"),
            "EMA200傾き": live_diag.get("EMA200傾き") if "EMA200傾き" in live_diag else live_diag.get("ema200_slope"),
            "SMA20傾き": live_diag.get("SMA20傾き") if "SMA20傾き" in live_diag else live_diag.get("sma20_slope"),
            "ATR%": live_diag.get("ATR%") if "ATR%" in live_diag else live_diag.get("atr_pct"),
            "last_ts": live_diag.get("last_ts"),
        })
    else:
        st.caption("ライブ指標は未取得。テンプレを使用。")

# ===== FxON カレンダー取得（以降のステップに委譲）=====

def _fetch_events_fxon(target_date: date, cfg: dict) -> tuple[list[dict], dict]:
    """
    FxONの経済指標カレンダー（当日～window_days日）を取得し、
    Step4で使う [{時刻, 指標, 地域, カテゴリ}, ...] を返す。
    戻り値: (rows, diag)
    """
    import requests
    from datetime import datetime, timezone, timedelta

    JST = timezone(timedelta(hours=9))

    prov = (cfg.get("providers") or {}).get("fxon") or {}
    if not prov.get("enabled"):
        return [], {"enabled": False}

    list_url   = prov.get("list_url")   or "https://fxon.com/lib/fxscalendar/index.php"
    detail_url = prov.get("detail_url") or "https://fxon.com/lib/fxscalendar/get-event.php"
    timeout    = int(prov.get("timeout_sec", 15))
    tz_param   = int(prov.get("tz_param", -9))
    win_days   = int(prov.get("window_days", 1))
    use_detail = bool(prov.get("use_detail", True))

    # 取得対象（JST日付）
    allowed_dates = [(target_date + timedelta(days=i)) for i in range(max(1, win_days))]
    d1 = allowed_dates[0].strftime("%Y-%m-%d")
    d2 = allowed_dates[-1].strftime("%Y-%m-%d")

    params = [("lang","ja"), ("tz", str(tz_param)), ("from", d1), ("to", d2)]
    for v in ("LOW","MEDIUM","HIGH"):
        params.append(("volatilities[]", v))

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://fxon.com/jp/calendar/",
    }

    def _iso_or_epoch_to_utc(x):
        if x is None:
            return None
        # epoch (秒/ミリ秒)
        try:
            if isinstance(x,(int,float)) or (isinstance(x,str) and x.isdigit()):
                sec = float(x)
                if sec > 1e12:  # msなら秒に
                    sec /= 1000.0
                return datetime.fromtimestamp(sec, tz=timezone.utc)
        except Exception:
            pass
        # ISO
        if isinstance(x, str):
            s = x.strip().replace("Z","+00:00")
            try:
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                return None
        return None

    def _pick(d, *names):
        for n in names:
            if isinstance(d, dict) and n in d and d[n] not in (None, ""):
                return d[n]
        return None

    ok = False
    http_status = None
    rows, raw_items = [], []
    error_msg = None

    try:
        r = requests.get(list_url, params=params, headers=headers, timeout=timeout)
        http_status = r.status_code
        r.raise_for_status()
        data = r.json()
        items = []
        if isinstance(data, dict):
            for k in ("events","items","data","rows"):
                if k in data and isinstance(data[k], list):
                    items = data[k]
                    break
        elif isinstance(data, list):
            items = data
        raw_items = items or []
        ok = True

        # 必要に応じて詳細APIで補完
        def _fetch_detail(eid: str):
            if not eid:
                return None
            try:
                rr = requests.get(detail_url, params={"lang":"ja","eventId":eid},
                                  headers=headers, timeout=timeout)
                rr.raise_for_status()
                return rr.json()
            except Exception:
                return None

        for it in raw_items:
            det = _fetch_detail(_pick(it, "eventId","id")) if use_detail else None

            # 時刻（UTC→JST→H:MM）
            ts = _pick(it, "dateUtc","datetimeUtc","timeUtc","utc","timestamp","dateTime","datetime","date","time")
            dt_utc = _iso_or_epoch_to_utc(ts)
            hhmm = ""
            if dt_utc:
                j = dt_utc.astimezone(JST)
                hhmm = f"{j.hour}:{j.minute:02d}"

            # 指標名・地域・カテゴリ
            indicator = _pick(it, "name","title","eventName") or (_pick(det,"name","title") if det else "") or ""
            country   = _pick(it, "countryCode","country","region") or (_pick(det,"countryCode","country") if det else "") or ""
            region    = (country or "")[:3].upper()
            category  = None
            if det:
                cat = _pick(det, "category")
                if isinstance(cat, dict):
                    category = _pick(cat, "name","title")
                elif isinstance(cat, str):
                    category = cat
            if not category:
                category = _pick(it, "category") if isinstance(_pick(it, "category"), str) else ""

            rows.append({"時刻": hhmm, "指標": indicator, "地域": region, "カテゴリ": category})

    except Exception as e:
        error_msg = str(e)

    diag = {
        "enabled": True,
        "endpoint": list_url,
        "use_detail": use_detail,
        "d1": d1, "d2": d2,
        "http_status": http_status,
        "api_raw_count": len(raw_items),
        "returned_rows": len(rows),
        "error": error_msg,
    }
    return rows, diag


# ====== ステップ4：指標候補（FxON専用 / TopN + チェック） ======
st.markdown("### ステップ4：指標候補（TopN + チェックで本文③へ）")

# 目的：開発時だけ診断ログを表示（通常は非表示）
show_debug = st.checkbox("診断ログを表示する（開発向け）", value=False)


# --- 1) 候補取得：FxON API専用（今日〜2日後の3日レンジ）-----------------------
import requests, re
from datetime import date as _date_cls, datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

raw_candidates: list[dict] = []  # ←以降の処理はこの変数だけ参照

_FXON_LIST = "https://fxon.com/lib/fxscalendar/index.php"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en;q=0.8",
    "Referer": "https://fxon.com/jp/calendar/",
}
_JST = ZoneInfo("Asia/Tokyo") if ZoneInfo else timezone(timedelta(hours=9))

def _iso_or_epoch_to_dt_utc(x):
    """ISO/epoch(ms|s)混在→UTC datetime。失敗時None。"""
    if x is None:
        return None
    if isinstance(x, (int, float)) or (isinstance(x, str) and x.isdigit()):
        sec = float(x)
        if sec > 1e12:  # ms→s
            sec = sec / 1000.0
        try:
            return datetime.fromtimestamp(sec, timezone.utc)
        except Exception:
            return None
    if isinstance(x, str):
        s = x.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None

def _fxon_fetch_list(_from: str, _to: str, lang="ja", tz_for_param="9") -> tuple[list, int]:
    """FxONの一覧APIを呼び出し、イベント配列とHTTPステータスを返す。"""
    params = [
        ("lang", lang),
        ("tz", tz_for_param),       # JSTは '9'
        ("from", _from),
        ("to", _to),
        ("volatilities[]", "LOW"),
        ("volatilities[]", "MEDIUM"),
        ("volatilities[]", "HIGH"),
    ]
    r = requests.get(_FXON_LIST, params=params, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    events = None
    if isinstance(data, dict):
        for k in ("events", "items", "data", "rows"):
            if k in data and isinstance(data[k], list):
                events = data[k]; break
    elif isinstance(data, list):
        events = data
    return (events or []), r.status_code

def _pick(d: dict, *names, default=None):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default

def _canon_region(country: str) -> str:
    """既存の CANON_map_country_to_region があれば使用。無ければ2文字簡易。"""
    f = globals().get("CANON_map_country_to_region")
    if callable(f):
        try:
            return f(country or "")
        except Exception:
            pass
    c = (country or "").strip().upper()
    return c[:2] if len(c) >= 2 else ""

# --- FxON: 正規化 + 取得 + 並べ替え（日付→時刻） ---
import re
from datetime import date as _date_cls, datetime, timedelta, timezone

# 既に上で _JST が定義済みならそれを使う／無ければここで JPY TZ を用意
if "_JST" not in globals():
    try:
        from zoneinfo import ZoneInfo  # Py3.9+
        _JST = ZoneInfo("Asia/Tokyo")
    except Exception:
        _JST = timezone(timedelta(hours=9))

def _normalize_row(row: dict) -> dict:
    """FxON 1件 → {日付, 時刻, 指標, 地域, カテゴリ} に正規化。"""
    ts = _pick(row, "dateUtc", "datetimeUtc", "utc", "timestamp", "dateTime", "datetime", "date", "time")
    dt_utc = _iso_or_epoch_to_dt_utc(ts)

    hhmm = ""
    mmdd = ""
    if dt_utc:
        dt_jst = dt_utc.astimezone(_JST)
        hhmm = dt_jst.strftime("%H:%M")
        mmdd = dt_jst.strftime("%m/%d")

    # 欠落でも捨てない
    if not hhmm:
        hhmm = "--:--"
    if not mmdd:
        mmdd = "--/--"

    name = _pick(row, "name", "title", "eventName") or ""
    country = _pick(row, "countryCode", "country", "region") or ""
    cat = _pick(row, "category", "cat")
    if isinstance(cat, dict):
        category = _pick(cat, "name", "title") or ""
    else:
        category = str(cat or "")

    return {
        "日付": mmdd,              # JSTの MM/DD
        "時刻": hhmm,
        "指標": str(name),
        "地域": _canon_region(country),
        "カテゴリ": category,
    }

try:
    _today = _date_cls.today()
    _from = _today.isoformat()
    _to   = (_today + timedelta(days=2)).isoformat()  # 3日レンジ（今日〜+2日）

    items, http_status = _fxon_fetch_list(_from, _to, tz_for_param="9")
    rows = [_normalize_row(it) for it in items]

    # ★ 日付→時刻→指標 の順で安定ソート（欠損は末尾）
    def _sort_key(r: dict):
     d = str(r.get("日付") or "")
     t = str(r.get("時刻") or "")
     dkey = "99/99" if not re.match(r"^\d{2}/\d{2}$", d) else d
     tkey = "99:99" if not re.match(r"^\d{2}:\d{2}$", t) else t
     return (dkey, tkey, r.get("指標", ""))

     rows.sort(key=_sort_key)

    fxon_diag = {
        "endpoint": _FXON_LIST,
        "http_status": http_status,
        "returned_rows": len(rows),
        "range": {"from": _from, "to": _to, "tz": 9},
    }

    if not rows:
        st.error("FxON APIは呼び出せましたがイベントが0件でした。日付範囲や tz(=9) をご確認ください。")
        st.stop()  # ← フォールバックなしで停止

    # ここまで来たら FxON を唯一ソースに確定
    raw_candidates = rows
    st.session_state["events_source"] = "fxon"  # 後段でのラベル上書きを防止
    st.success("候補ソース：FxON API（読み込みOK）")

    # 診断の表示はトグルに追従
    if show_debug:
        with st.expander("接続診断（本文には出ません）", expanded=False):
            st.json(fxon_diag)

except Exception as e:
    st.error(f"FxON取得でエラー：{e}")
    st.stop()  # ← フォールバックなしで停止


# ==== 表記クリーニング（外部 YAML: data/cleaning_rules.yaml）====

import yaml
_CLEAN_RULES_CACHE = None

def _load_cleaning_rules_pairs() -> list[tuple[str, str]]:
    global _CLEAN_RULES_CACHE
    if _CLEAN_RULES_CACHE is not None:
        return _CLEAN_RULES_CACHE
    pairs = []
    p = Path("data") / "cleaning_rules.yaml"
    if p.exists():
        try:
            y = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            for item in (y.get("replace") or []):
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    pairs.append((str(item[0]), str(item[1])))
        except Exception:
            pass
    if not pairs:
        pairs = [
            ("　"," "),("  "," "),(" 、","、"),(" 。","。"),("、、","、"),("。。","。"),
            ("l可能性","の可能性"),
        ]
    _CLEAN_RULES_CACHE = pairs
    return pairs

def _clean_text_jp(s: str) -> str:
    if not isinstance(s, str):
        return s
    s = s.replace("\u3000"," ")
    pairs = _load_cleaning_rules_pairs()
    for _ in range(2):
        for a,b in pairs:
            s = s.replace(a,b)
    return s.strip()
# ==== ここまで ====


# --- 2) スコア付け & テーブル編集 ---

# 安全ガード：raw_candidates は list[dict] であること
if raw_candidates and not isinstance(raw_candidates[0], dict):
    st.error("内部型エラー：イベント候補の形式が想定外です（dictの配列ではありません）。")
    raw_candidates = []

# ①（任意）未登録の和訳キーを拾う
import re
unknown_keys = set()
for rec in (raw_candidates or []):
    t = str(rec.get("指標", "")).strip()
    # 「米・/英・…」の接頭を仮にはがして辞書ヒットを見る
    t = re.sub(r'^(?:米|日|欧|英|豪|NZ|中国|南ア)・\s*', '', t)
    if _ja_indicator_name(t, "") == t:
        unknown_keys.add(t)

if unknown_keys:
    with st.expander("⚠ 未登録の和訳キー（辞書に追記してください）", expanded=False):
        st.caption("下を data/indicator_alias_ja.yaml にコピペして訳語を埋めてください。")
        st.code("\n".join([f'"{k}": ""' for k in sorted(unknown_keys)]), language="yaml")

# ② 候補データを日本語表示に整形（ここで一度だけ）
for rec in (raw_candidates or []):
    ind_ja = _ja_indicator_name(rec.get("指標", ""), rec.get("地域", ""))
    rec["指標"]    = ind_ja
    rec["カテゴリ"] = _ja_category_name(rec.get("カテゴリ", ""), indicator=ind_ja)

# ③ DataFrame 化（必須列の穴埋め）
df = pd.DataFrame(raw_candidates or [])
# 必ず必要な列を先に用意（空でも落ちないように）
for col in ["日付", "時刻", "指標", "地域", "カテゴリ"]:
    if col not in df.columns:
        df[col] = ""

# ④ スコア付け
region_w = {"JP": 3, "US": 3, "EU": 2, "UK": 2, "AU": 2, "NZ": 2, "CN": 2}
cat_w    = {"雇用": 5, "金利": 4, "インフレ": 4, "PMI": 3, "住宅": 3, "信頼感": 2}

def _parse_hm(t):
    try:
        h, m = str(t).split(":");  return int(h), int(m)
    except Exception:
        return 99, 99

# スコア用の補助列（空でも安全に動く）
df["地域W"]      = df["地域"].map(region_w).fillna(1)
df["カテゴリW"]  = df["カテゴリ"].map(cat_w).fillna(1)
df["H"], df["M"] = zip(*df["時刻"].map(_parse_hm)) if len(df) else ([], [])
df["時間W"]      = ((df.get("H", pd.Series(dtype=int)).between(9,12)) |
                   (df.get("H", pd.Series(dtype=int)).between(21,24))).astype(int) if len(df) else 0
df["スコア"]      = df["地域W"] + df["カテゴリW"] + df["時間W"]

# ---------- ルール圧縮（日本人読者向けに円と米を厚めに） ----------
def reduce_events_for_body(df_in: pd.DataFrame, max_items: int = 14) -> pd.DataFrame:
    d = df_in.copy()

    # 1) 類似束ね（同時刻×地域×カテゴリ）
    base_cols = [c for c in ["時刻", "地域", "カテゴリ"] if c in d.columns]
    if base_cols and not d.empty:
        d = d.sort_values(["スコア", "H", "M"], ascending=[False, True, True])
        d = d.drop_duplicates(subset=base_cols + ["指標"], keep="first")
        d = d.drop_duplicates(subset=base_cols, keep="first")

    # 2) 日本人向けブースト（JP/USを厚めに）
    if not d.empty:
        jp_boost = (d["地域"] == "JP").astype(int) * 2
        us_boost = (d["地域"] == "US").astype(int) * 1
        d["スコア+"] = d["スコア"].fillna(0) + jp_boost + us_boost
    else:
        d["スコア+"] = d.get("スコア", pd.Series(dtype=float))

    # 3) 一般講演は減点（トップ級は維持）
    if not d.empty:
        speech_mask = d["指標"].astype(str).str.contains(r"講演|発言|会見|総裁|議長|speech", regex=True)
        high_rank   = d["指標"].astype(str).str.contains(r"(FRB|パウエル|日銀|植田|BOJ|FOMC|ECB|ラガルド)", regex=True)
        d["スコア+"] = d["スコア+"] - (speech_mask & ~high_rank).astype(int)

    # 4) 最終ソート＆切出し
    if not d.empty:
        d = d.sort_values(["スコア+", "H", "M", "指標"], ascending=[False, True, True, True]).head(max_items).reset_index(drop=True)

    return d
# ---- ここから：UIに渡すブロック（reduce_events_for_body の直後に置く）----
import pandas as pd
import unicodedata, re

# 0) 候補の圧縮（安全：df が無い/空でも落ちない）
try:
    df_for_body = reduce_events_for_body(df, max_items=14)
except Exception:
    df_for_body = pd.DataFrame()

# 1) 空でも安全な表示用DFを用意
_cols = ["日付", "時刻", "指標", "地域", "カテゴリ", "スコア"]
if df_for_body is None or df_for_body.empty:
    df_display = pd.DataFrame(columns=_cols)
else:
    # 欠け列があっても落ちないように補完
    df_display = df_for_body.copy()
    for c in _cols:
        if c not in df_display.columns:
            df_display[c] = ""
    df_display = df_display[_cols]
# ★ 表示順を 日付→時刻→スコア に固定（欠損や "--:--" は末尾へ）
import re

def _to_minutes(s: str) -> int:
    try:
        h, m = map(int, str(s).split(":"))
        return h * 60 + m
    except Exception:
        return 10**9  # 時刻不明は最後へ

def _to_mmdd_order(s: str) -> int:
    try:
        mm, dd = map(int, str(s).split("/"))
        return mm * 100 + dd
    except Exception:
        return 10**9  # 日付不明は最後へ

df_display = (
    df_display
    .assign(
        __d=df_display.get("日付", "").map(_to_mmdd_order) if "日付" in df_display.columns else 10**9,
        __t=df_display.get("時刻", "").map(_to_minutes)     if "時刻" in df_display.columns else 10**9,
    )
    .sort_values(by=["__d", "__t", "スコア"], ascending=[True, True, False], kind="stable")
    .drop(columns=["__d", "__t"])
)

# 2) 採用フラグ列（空でも列だけは作る）
df_display["採用"] = True

# 3) 編集テーブル（key はこの画面内でユニーク）
edited_df = st.data_editor(
    df_display,
    num_rows="fixed",
    use_container_width=True,
    hide_index=True,
    key="events_editor_v2",
)

# --- ⑤ 本文③用の1行（重複吸収＆時刻順：安全・自立版） ---
import re, unicodedata
import pandas as pd

def _normalize_time_str(s: str) -> str:
    try:
        h, m = str(s).split(":")
        return f"{int(h)}:{int(m):02d}"
    except Exception:
        return str(s)

def _norm_for_dedup_label(s: str) -> str:
    t = unicodedata.normalize("NFKC", str(s or "")).strip()
    # 地域接頭（米・日・英・…）は重複判定では無視
    t = re.sub(r'^(?:米|日|英|欧|豪|NZ|中国|南ア)・\s*', '', t)
    # ミシガン指数の表記ゆれ（ロイター/大学/消費/速報の差）を吸収
    t = re.sub(r'(?:ロイター・)?ミシガン(?:大学)?消費(?:者)?信頼感指数', 'ミシガン大学消費者信頼感指数', t)
    # スペース除去
    t = re.sub(r'\s+', '', t)
    return t

# === ステップ5：本日のポイント（2件選択） ===
st.markdown("### ステップ5：本日のポイント（2件選択）")
st.caption("ここで選んだ2件は、①に一言・③の1行の材料として使われます。")
st.divider()

# ===== Step5 一式（並べ替え/重複除去 + calendar_line 確定 + 本日のポイントUI）=====
import pandas as pd, re, unicodedata

# 画面内でユニークなキー名
WNS = "step5_main"
CHRONO_KEY = f"{WNS}_chronosort"
DEDUP_KEY  = f"{WNS}_dedup"

# --- ヘルパー ---
def _normalize_time_str(s: str) -> str:
    try:
        h, m = str(s).split(":")
        return f"{int(h)}:{int(m):02d}"
    except Exception:
        return str(s)

def _norm_for_dedup_label(s: str) -> str:
    t = unicodedata.normalize("NFKC", str(s or "")).strip()
    # 地域接頭は重複判定で無視
    t = re.sub(r'^(?:米|日|英|欧|豪|NZ|中国|南ア)・\s*', '', t)
    # ミシガン指数の表記ゆれ吸収
    t = re.sub(r'(?:ロイター・)?ミシガン(?:大学)?消費(?:者)?信頼感指数', 'ミシガン大学消費者信頼感指数', t)
    # スペース除去
    t = re.sub(r'\s+', '', t)
    return t

# --- オプションUI（キーは必ずユニークに1回だけ） ---
chronosort = st.checkbox(
    "本文③は時刻順で並べる",
    value=st.session_state.get(CHRONO_KEY, True),
    key=CHRONO_KEY,
)
dedup = st.checkbox(
    "重複を自動削除（時刻×指標）",
    value=st.session_state.get(DEDUP_KEY, True),
    key=DEDUP_KEY,
)

# --- selected を安全に作る（この1回だけ） ---
if "selected" not in locals() or not isinstance(globals().get("selected"), pd.DataFrame):
    if "edited_df" in locals() and isinstance(edited_df, pd.DataFrame) and not edited_df.empty:
        selected = edited_df[edited_df["採用"] == True].copy() if "採用" in edited_df.columns else edited_df.copy()
    else:
        selected = pd.DataFrame(columns=["時刻","指標","地域","カテゴリ","スコア","採用"])

# --- ③の1行 calendar_line を確定（重複吸収 → 時刻順 → 最終ユニーク化） ---
if selected.empty:
    calendar_line = ""
else:
    df_out = selected.copy()
    df_out["時刻_norm"] = df_out["時刻"].astype(str).map(_normalize_time_str)
    df_out["指標_norm"] = df_out["指標"].astype(str).map(_norm_for_dedup_label)

    if dedup:
        df_out = df_out.drop_duplicates(subset=["時刻_norm","指標_norm"], keep="first")

    if chronosort:
        hm = df_out["時刻_norm"].str.split(":")
        h  = pd.to_numeric(hm.str[0], errors="coerce").fillna(99).astype(int)
        m  = pd.to_numeric(hm.str[1], errors="coerce").fillna(99).astype(int)
        df_out = df_out.assign(h=h, m=m).sort_values(["h","m","指標_norm"]).drop(columns=["h","m"])

    # 連結直前でも順序維持ユニーク（最終保険）
    unique_pairs = list(dict.fromkeys(zip(df_out["時刻_norm"], df_out["指標_norm"])))
    calendar_line = "、".join(f"{t}に{idx}" for t, idx in unique_pairs)

# --- ③の唯一ソースとしてセッションに保存 ---
st.session_state["calendar_line"] = calendar_line

# --- 本日のポイント UI（この multiselect を1つだけ残す）---
point_candidates = list(
    df_display["時刻"].astype(str) + "に" + df_display["指標"].astype(str)
) if not df_display.empty else []
default_points = point_candidates[:2]

# 既存 state を尊重しつつ options に無い値は除外
existing = st.session_state.get("points_tags_v2", [])
existing = existing if isinstance(existing, list) else []
safe_default = [x for x in existing if x in point_candidates][:2] or default_points

chosen_points = st.multiselect(
    "本文冒頭の『本日のポイント』に載せる2件を選んでください（2件必須）",
    options=point_candidates,
    default=safe_default,
    max_selections=2,
    key="points_pick_main",   # ← ここ以外に multiselect を作らない
)

# セッション保存（ウィジェットkeyとは別に持つと、後で安全に上書きできる）
st.session_state["points_tags_v2"] = list(chosen_points)[:2] if chosen_points else safe_default

if point_candidates and len(st.session_state["points_tags_v2"]) < 2:
    st.info("『本日のポイント』は2件選んでください（プレビューは作れますが、空文になる可能性があります）。")
# ===== Step5 一式 ここまで =====

# --- 段落①に『本日のポイント』を一言だけ自動挿入（重複防止・任意） ---
import re
import streamlit as st
from datetime import datetime
import json, unicodedata

# 本日のポイント（最大2件）
pts = [x for x in (st.session_state.get("points_tags_v2") or []) if x][:2]

# para1_for_build の安全な初期化
p1 = (para1_for_build if "para1_for_build" in globals()
      else (para1 if "para1" in globals() else ""))

def _already_mentions(body: str, items: list[str]) -> bool:
    if not body or not items:
        return True
    b = re.sub(r"\s+", "", body)
    for it in items:
        # "8:50に日・GDP..." → "日・GDP..." のように先頭の時刻を除去して判定
        key = (it.split("に", 1)[-1] or "")
        if key and key in b:
            return True
    return False

if pts and not _already_mentions(p1, pts):
    line = "本日は" + (pts[0] + ("と" + pts[1] if len(pts) > 1 else "")) + "が控えており、短期の振れに留意したい。"
    # 似た文があれば除去して1本だけ差し込む
    p1 = re.sub(r"本日は[^。]*?留意したい。", "", p1).strip()
    if p1 and not p1.endswith("。"):
        p1 += "。"
    para1_for_build = (p1 + " " + line).strip()
else:
    para1_for_build = p1
# ---- 段落③：指標列の「時間 に 国・指標」体裁を統一 ----
def _normalize_calendar_line(line: str) -> str:
    import re, unicodedata
    s = unicodedata.normalize("NFKC", str(line or "")).strip()
    if not s:
        return ""

    # 例： "... 8:50に企業向けサービス価格指数(前年比)、10:30にRBA議事録、..."
    # 「HH:MM に 〜」の並びを全部拾う
    items = re.findall(r"([0-2]?\d:[0-5]\d)\s*に\s*([^、。]+)", s)
    if not items:
        return s  # 予期せぬ形式は素通し

    def has_prefix(name: str) -> bool:
        return re.match(r"^(米|日|英|欧|独|仏|豪|NZ|加|南ア|スイス|中)\s*・", name) is not None

    def add_prefix(name: str) -> str:
        if has_prefix(name):
            return name
        # 代表的なキーワード→国略称
        mapping = [
            (r"FOMC|レッドブック|連銀|MBA|S&P|ケース・シラー|JOLTS|中古住宅|新築住宅|API|財務省|T-Note|耐久財|ISM|コンファレンスボード|PCE|パウエル|ジェファーソン|ウォラー|ベージュブック|ダラス連銀|シカゴ連銀|NY連銀", "米"),
            (r"RBA|Westpac|豪州|ブロック", "豪"),
            (r"ANZ|NZ|ニュージーランド", "NZ"),
            (r"ECB|ユーロ圏|ユーロ", "欧"),
            (r"独|IFO|ZEW|ナーゲル|ドイツ", "独"),
            (r"仏|フランス", "仏"),
            (r"英|BOE|CBI|RICS|ネーションワイド|ハリファックス|ベイリー|グリーン", "英"),
            (r"スイス|SNB|シュレーゲル", "スイス"),
            (r"加|BOC|マックレム|カナダ", "加"),
            (r"中国|最優遇貸出金利|ローンプライムレート|LPR", "中"),
            (r"南ア|南アフリカ", "南ア"),
            (r"日|東京都区部|日銀|国内企業物価|機械受注|企業向けサービス価格指数|景気動向|鉱工業|家計|対外/対内証券|マネーストック|全国消費者物価|景気ウォッチャー", "日"),
        ]
        for pat, c in mapping:
            if re.search(pat, name):
                return f"{c}・{name}"
        return name  # 不明はそのまま

    normalized = []
    for t, body in items:
        body = add_prefix(body.strip())
        # 体裁：『時間␣に国・指標』
        normalized.append(f"{t} に{body}")

    # 区切りは読点。末尾の句点はここでは付けない（外側で付く想定）
    return "、".join(normalized)


# =========================
# ステップ6：プレビュー（公開用体裁 + 自動チェック + 保存）
# =========================
import streamlit as st
st.markdown("### ステップ6：プレビュー（公開用体裁 + 自動チェック）")

# ---- 再読込コントロール ----
c1, c2, c3 = st.columns([1, 1, 3])
with c1:
    if st.button("クリーニング再読込", key="reload_clean_rules_main"):
        try: globals()["_CLEAN_RULES_CACHE"] = None
        except Exception: pass
        try: st.rerun()
        except Exception: st.experimental_rerun()
with c2:
    if st.button("安全文再読込", key="reload_para2_boiler_main"):
        try: globals()["_P2_SAFE_CACHE"] = None
        except Exception: pass
        try: st.rerun()
        except Exception: st.experimental_rerun()
with c3:
    if st.button("ルール要約 再読込", key="reload_rules_digest_main"):
        try:
            RULES_DIGEST = _read_rules_digest()
            st.success("rules_digest.txt を再読込しました。")
        except Exception as e:
            st.warning(f"再読込でエラー：{e}")

# ========== ここから：③は「FXONデータ直参照」で確定生成（パターン一切なし） ==========
import re, unicodedata, json, sys, subprocess
from pathlib import Path
from datetime import datetime

def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s or ""))

def _clean_text_jp_safe(s: str) -> str:
    t = _nfkc(s or "").strip()
    t = re.sub(r"[　 ]+", " ", t)
    t = re.sub(r"([。])\1+", r"\1", t)
    return t

def _extract_hhmm(v) -> str:
    s = str(v or "").strip()
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if m: return f"{int(m.group(1))}:{m.group(2)}"
    m = re.search(r"T(\d{2}):(\d{2})", s)
    if m: return f"{int(m.group(1))}:{m.group(2)}"
    m = re.search(r"\b(\d{2})(\d{2})\b", s)  # 2130 → 21:30
    if m: return f"{int(m.group(1))}:{m.group(2)}"
    return ""

def _strip_country_prefix(title: str) -> str:
    # 既に「米・」「英・」などが先頭に付いていたら除去（付け直すため）
    return re.sub(r"^\s*(米|英|日|欧|独|仏|豪|NZ|加|南ア|スイス|中国)\s*[・･]\s*", "", str(title or "").strip())

# かっこ周りの余白を整形
def _tidy_label(name: str) -> str:
    s = _nfkc(name or "")
    s = re.sub(r"\s+([)）])", r"\1", s)
    s = re.sub(r"([（(])\s+", r"\1", s)
    return s.strip()

# --- FXONのイベントテーブル取得（DataFrame or list[dict] のどれでも） ---
def _events_df_like():
    for k in ["selected", "edited_df", "df_display"]:
        df = globals().get(k)
        if df is not None:
            return df
    for k in ["events_df","fxon_events_df","econ_events_df","events_table"]:
        df = st.session_state.get(k)
        if df is not None:
            return df
    for k in ["events","fxon_events","econ_events"]:
        arr = st.session_state.get(k)
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            return arr
    return None

def _pick(row, keys, default=""):
    for k in keys:
        if isinstance(row, dict) and k in row:
            return row.get(k, default)
        try:
            return row[k]
        except Exception:
            continue
    return default

# --- 地域列 → 和略称（推測なし） ---
_JA_NAME_TO_ABBR = {
    "米": "米","米国": "米","アメリカ": "米",
    "英": "英","英国": "英","イギリス": "英",
    "日": "日","日本": "日",
    "欧": "欧","ユーロ圏": "欧","欧州": "欧","ユーロエリア":"欧",
    "独": "独","ドイツ": "独",
    "仏": "仏","フランス": "仏",
    "伊": "伊","イタリア": "伊",
    "西": "西","スペイン": "西",
    "スイス":"スイス",
    "加":"加","カナダ":"加",
    "豪":"豪","豪州":"豪","オーストラリア":"豪",
    "NZ":"NZ","ニュージーランド":"NZ",
    "中国":"中国","中":"中国",
    "南ア":"南ア","南アフリカ":"南ア",
}
_EN_NAME_TO_ISO2 = {
    "UNITED STATES":"US","USA":"US","UNITED KINGDOM":"GB","UK":"GB","JAPAN":"JP",
    "EUROZONE":"EU","EURO AREA":"EU","EUROPEAN UNION":"EU",
    "GERMANY":"DE","FRANCE":"FR","ITALY":"IT","SPAIN":"ES",
    "SWITZERLAND":"CH","CANADA":"CA","AUSTRALIA":"AU","NEW ZEALAND":"NZ","CHINA":"CN",
    "SOUTH AFRICA":"ZA",
}
_ISO_TO_ABBR = {
    "US":"米","GB":"英","JP":"日","EU":"欧",
    "DE":"独","FR":"仏","IT":"伊","ES":"西",
    "CH":"スイス","CA":"加","AU":"豪","NZ":"NZ","CN":"中国","ZA":"南ア",
    "USA":"米","GBR":"英","JPN":"日","DEU":"独","FRA":"仏","ITA":"伊","ESP":"西",
    "CHE":"スイス","AUS":"豪","NZL":"NZ","CHN":"中国","ZAF":"南ア","CAN":"加",
}
def _abbr_from_region_value(v: str) -> str:
    s = _nfkc(v).strip()
    if not s: return ""
    if s in _JA_NAME_TO_ABBR:
        return _JA_NAME_TO_ABBR[s]
    core = re.sub(r"\s*\(.*?\)\s*", "", s).upper()
    if core in _EN_NAME_TO_ISO2:
        iso2 = _EN_NAME_TO_ISO2[core]
        return _ISO_TO_ABBR.get(iso2, "")
    return _ISO_TO_ABBR.get(core, "")

def _abbr_from_row(ev: dict) -> str:
    cand_keys = (
        "地域","国","国コード",
        "region","region_code","regionCode",
        "country","country_name_ja","country_ja",
        "country_code","countryCode",
        "ccy","iso2","iso3","currency"
    )
    for k in cand_keys:
        if k in ev and ev[k]:
            ab = _abbr_from_region_value(ev[k])
            if ab: return ab
    return ""

def _normalize_time_str(s: str) -> str:
    try:
        h, m = str(s).split(":")
        return f"{int(h)}:{int(m):02d}"
    except Exception:
        return str(s)

# --- ③：FXON → 「HH:MM に略称・指標」列挙を直接生成 ---
def _build_calendar_from_fxon() -> str:
    src = _events_df_like()
    if src is None:
        return ""
    if hasattr(src, "to_dict"):
        try:
            records = src.to_dict(orient="records")
        except Exception:
            records = []
    else:
        records = list(src)

    rows = []
    for r in records:
        t_raw = _pick(r, ["時刻","time","local_time","datetime","start_at","start"])
        title = _pick(r, ["指標","indicator","title","name"], "")
        abbr  = _abbr_from_row(r)
        hhmm  = _extract_hhmm(t_raw)
        if not title or not hhmm:
            continue
        ttl = _tidy_label(_strip_country_prefix(title))
        disp = f"{_normalize_time_str(hhmm)} に{(abbr + '・') if abbr else ''}{ttl}"  # “に”の後は詰める
        norm_key = (hhmm, _nfkc(ttl))
        rows.append((hhmm, norm_key, disp))

    rows.sort(key=lambda x: (int(x[0].split(':')[0]), int(x[0].split(':')[1])))
    seen = set()
    items = []
    for _, key, disp in rows:
        if key in seen:
            continue
        seen.add(key)
        items.append(disp)

    out = "、".join(items)
    out = re.sub(r'([0-2]?\d:[0-5]\d)\s*に\s*', r'\1 に', out)  # 「 に」の間隔を統一
    return out

# ③の唯一ソースを確定
cal_line = _build_calendar_from_fxon()
if not cal_line:
    cal_line = str(st.session_state.get("calendar_line", "") or "")
cal_line = _clean_text_jp_safe(cal_line)
if cal_line:
    st.session_state["calendar_line"] = cal_line

# ========== ここまで：③生成 ==========

# --- 本文①/②（for_build を優先：既存ロジック踏襲） ---
p1 = para1_for_build if "para1_for_build" in globals() else (para1 if "para1" in globals() else "")

p2_source = (
    st.session_state.get("p2_ui_preview_text")
    or globals().get("_para2_preview")
)
if not p2_source:
    try:
        p2_source = _compose_para2_preview_mix()
    except Exception:
        try:
            p2_source = _compose_para2_preview_from_ui()
        except Exception:
            pair = str(st.session_state.get("pair", "") or "")
            d1   = st.session_state.get("d1_imp", "横ばい")
            h4   = st.session_state.get("h4_imp", "横ばい")
            p2_source = f"為替市場は、{pair}は日足は{d1}、4時間足は{h4}。"

p2 = _clean_text_jp_safe(str(p2_source).strip().rstrip("。") + "。")
st.session_state["para2"] = p2
st.session_state["para2_for_build"] = p2

if "_final_polish_and_guard" in globals() and callable(globals().get("_final_polish_and_guard")):
    p1_out = _final_polish_and_guard(st.session_state.get("para1_for_build", ""), para="p1")
    p2_out = _final_polish_and_guard(st.session_state.get("para2_for_build", ""), para="p2")
else:
    p1_out = _clean_text_jp_safe(st.session_state.get("para1_for_build", ""))
    p2_out = _clean_text_jp_safe(st.session_state.get("para2_for_build", ""))
p1, p2 = p1_out, p2_out
st.session_state["p1_ui_preview_text"] = p1
st.session_state["p2_ui_preview_text"] = p2

# =========================
# AI補正：タイトル & タイトル回収（手入力ニュース + RSS候補）
# =========================
import sys, subprocess, re, json
from pathlib import Path
from datetime import datetime

# --- 既存ユーティリティが無い環境でも落ちないよう最小フォールバック ---
try:
    _nfkc
except NameError:
    import unicodedata
    def _nfkc(s: str) -> str:
        return unicodedata.normalize("NFKC", str(s or ""))

try:
    _clean_text_jp_safe
except NameError:
    def _clean_text_jp_safe(s: str) -> str:
        t = _nfkc(s or "").strip()
        t = re.sub(r"[　 ]+", " ", t)
        t = re.sub(r"([。])\1+", r"\1", t)
        return t

# --- AI使用サイン用（LLM/RSSの利用状況と概算トークン） ---
def _ai_flags():
    if "ai_flags" not in st.session_state:
        st.session_state["ai_flags"] = {"llm_used": False, "rss_used": False, "tokens_est": 0, "last_error": ""}
    return st.session_state["ai_flags"]

def _est_tokens(s: str) -> int:
    # 超ざっくり：日本語は約3文字=1tokenくらいの目安
    s = str(s or "")
    return max(0, round(len(s) / 3))

def _call_llm_with_flags(prompt: str) -> str:
    """llm_complete の呼び出しをラップして使用サインと概算トークンを記録"""
    af = _ai_flags()
    if "llm_complete" in globals() and callable(globals().get("llm_complete")):
        try:
            out = llm_complete(prompt)
            if isinstance(out, str) and out.strip():
                af["llm_used"] = True
                af["tokens_est"] += _est_tokens(prompt) + _est_tokens(out)
                return out
        except Exception as e:
            af["last_error"] = repr(e)
            return ""
    return ""

# --- ニュース見出しのクレンジング（媒体名/日付/URL/括弧） ---
def _clean_news_title_for_prompt(t: str) -> str:
    s = _nfkc(t or "")
    s = re.sub(r"^\d+[).:\-]\s*", "", s)                               # 先頭の番号
    s = re.sub(r"\s*\d{4}[./]\d{1,2}[./]\d{1,2}\s*$", "", s)           # 末尾の日付
    s = re.sub(r"\s*[–—\-‐\-]\s*[^・、，,。]+$", "", s)                 # " - 媒体"
    s = re.sub(r"（[^）]*）", "", s); s = re.sub(r"\([^)]*\)", "", s)   # 括弧
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s

# --- 回収文に混入する括弧情報（媒体/日付/URL）を強制除去 ---
_PAT_MEDIA = r"(?:外為どっとコム|ロイター|Reuters|Bloomberg|ブルームバーグ|日経|Nikkei|共同|Kyodo|時事|Jiji|朝日|毎日|読売|CNBC|Yahoo|ヤフー|みんかぶ|MINKABU)"
_PAT_DATE  = r"(?:\d{4}[./]\d{1,2}[./]\d{1,2})"
_PAT_URL   = r"(?:https?://\S+)"
def _strip_media_brackets(s: str) -> str:
    r = _clean_text_jp_safe(s or "")
    r = re.sub(rf"（[^）]*?(?:{_PAT_MEDIA}|{_PAT_DATE}|{_PAT_URL})[^）]*?）", "", r)
    r = re.sub(rf"\([^)]*?(?:{_PAT_MEDIA}|{_PAT_DATE}|{_PAT_URL})[^)]*?\)", "", r)
    r = re.sub(r"\s{2,}", " ", r).strip()
    return r

# --- RSSユーティリティ ---
def _ensure_feedparser():
    try:
        import feedparser  # noqa
        return True
    except Exception:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "feedparser", "-q"])
            import feedparser  # noqa
            return True
        except Exception:
            return False

def _google_news_rss_search(query: str, lang="ja", gl="JP", ceid="JP:ja", limit=8):
    import urllib.parse, feedparser
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={gl}&ceid={ceid}"
    feed = feedparser.parse(url)
    out = []
    for e in (feed.entries or [])[:limit]:
        title = str(getattr(e, "title", "") or "")
        link  = str(getattr(e, "link", "")  or "")
        publ  = str(getattr(e, "published", "") or getattr(e, "updated", "") or "")
        src   = ""
        try: src = str(e.source.title)
        except Exception: pass
        out.append({"title": title, "url": link, "source": src, "published": publ})
    return out

def _rank_news(items: list[dict], max_items=10):
    def _norm_title(t: str) -> str:
        t = _nfkc(t or "").strip()
        t = re.sub(r"\s+", " ", t)
        t = re.sub(r"[【\[][^】\]]+[】\]]", "", t)
        return t
    WEIGHTS = {
        # US
        "FRB":8,"FOMC":8,"パウエル":7,"米雇用統計":9,"NFP":9,"CPI":7,"PCE":7,"ISM":6,"JOLTS":5,"米金利":7,
        # EU
        "ECB":8,"ラガルド":6,"ユーロ圏":7,"HICP":6,"PMI":5,"ドイツ":5,"IFO":5,"ZEW":5,
        # JP
        "日銀":9,"植田":6,"YCC":6,"長期金利":5,"消費者物価":5,"為替介入":7,"マイナス金利":6,
        # Global
        "原油":5,"WTI":5,"OPEC":5,"中東":6,"地政学":6,"停戦":6,"戦闘":6,"停電":5,"地震":5,
        "為替":3,"外為":3
    }
    def score(title: str) -> int:
        s = 0
        for k, w in WEIGHTS.items():
            if k in title: s += w
        return s
    seen, ranked = set(), []
    for it in items:
        t = _norm_title(it.get("title",""))
        if not t or t in seen: continue
        seen.add(t)
        ranked.append({**it, "title": t, "_score": score(t)})
    ranked.sort(key=lambda x: x["_score"], reverse=True)
    return ranked[:max_items]

def _fetch_fx_related_news(max_items=10):
    ok = _ensure_feedparser()
    if not ok:
        st.warning("feedparser のインストールに失敗しました。ニュース取得はスキップします。")
        return []
    pair = str(globals().get("pair","") or "")
    pair_q = []
    if "ドル" in pair or "USD" in pair.upper(): pair_q += ["ドル 為替", "米金利 為替"]
    if "円"   in pair or "JPY" in pair.upper(): pair_q += ["日銀 為替", "為替介入"]
    if "ユーロ" in pair or "EUR" in pair.upper(): pair_q += ["ECB 為替", "ユーロ 為替"]
    if "ポンド" in pair or "GBP" in pair.upper(): pair_q += ["BOE 為替", "ポンド 為替"]
    if "フラン" in pair or "CHF" in pair.upper(): pair_q += ["SNB 為替", "スイス 金利"]

    core_queries = [
        "FRB FOMC 為替","米雇用統計 NFP","米 CPI インフレ","米 PCE 物価","ISM 指数 為替",
        "ECB 金利 ユーロ","ユーロ圏 HICP 物価","ドイツ IFO 景況","ZEW 景況感",
        "日銀 金利 為替","為替介入 日本","日本 CPI 物価","長期金利 日本",
        "原油先物 相場","中東 地政学 為替","米大統領選 為替",
    ]
    queries = core_queries + pair_q
    all_items = []
    for q in queries:
        try:
            all_items += _google_news_rss_search(q, limit=8)
        except Exception:
            pass
    items = _rank_news(all_items, max_items=max_items)
    if items: _ai_flags()["rss_used"] = True
    return items

# --- タイトル＆回収（AI/フォールバック） ---
def _ai_title_and_recall(preview_text: str, manual_news_list: list[str], picked_news_list: list[dict],
                         base_title_tail: str = "", pair_name: str = ""):
    news_lines = [x.strip() for x in manual_news_list if str(x).strip()]
    news_lines += [_clean_news_title_for_prompt(d["title"]) for d in picked_news_list if d.get("title")]

    prompt = (
        "次の素材を踏まえ、FXレポートの『自然で簡潔なタイトル（18〜28字程度）』を1つと、"
        "『タイトル回収の一文（50〜90字程度）』を1つ作ってください。"
        "断定は避け、助言はしない。句読点は和文のまま。"
        "ニュースの媒体名や日付、URLは本文に書かない。括弧で挿入しない。\n\n"
        f"【主役ペア】{pair_name}\n"
        f"【タイトル語尾候補】{base_title_tail}\n"
        "【本日の重要ニュース（人入力+AI拾い）】\n- " + "\n- ".join(news_lines[:10]) + "\n\n"
        "【Pre-AI本文プレビュー】\n" + _clean_text_jp_safe(preview_text)[:1200]
        + "\n\n--- 出力フォーマット ---\n"
          "Title: <タイトル>\n"
          "Recall: <タイトル回収の一文>\n"
    )
    out = _call_llm_with_flags(prompt)

    if isinstance(out, str) and "Title:" in out and "Recall:" in out:
        m1 = re.search(r"Title:\s*(.+)", out)
        m2 = re.search(r"Recall:\s*(.+)", out)
        title  = _clean_text_jp_safe(m1.group(1)) if m1 else ""
        recall = _strip_media_brackets(_clean_text_jp_safe(m2.group(1)) if m2 else "")
        title = title.strip("。"); recall = recall.rstrip("。")
        if title: return title, recall

    # フォールバック
    base = pair_name or str(globals().get("pair", "") or "為替")
    tail = base_title_tail or str(st.session_state.get("title_tail") or "見極めたい")
    tip = f"（{news_lines[0]}）" if news_lines else ""
    title = _clean_text_jp_safe(f"{base}の方向感を{tail}".replace("に見極めたい", "見極めたい")).strip("。")
    recall = _strip_media_brackets(
        _clean_text_jp_safe(f"{base}は材料が交錯しやすい局面{('で' + tip) if tip else 'で'}、ヘッドライン次第の振れに留意したい").rstrip("。")
    )
    return title, recall

def _selected_news_strings() -> tuple[list[str], list[dict]]:
    man = st.session_state.get("manual_news_lines", "")
    manual_list = [x.strip() for x in str(man or "").splitlines() if x.strip()]
    sel_idx = st.session_state.get("ai_news_selected_idx", [])
    cand = st.session_state.get("ai_news_candidates", [])
    picked = [cand[i] for i in sel_idx if isinstance(i, int) and i < len(cand)]
    return manual_list, picked

with st.container():
    st.markdown("#### AI補正：タイトル/回収 と 重要ニュース（任意）")
    st.caption("手入力ニュースを優先。AI候補は参考（RSSのみ使用／外部ブラウズ不要）。")

    col1, col2 = st.columns([3, 2])
    with col1:
        manual_news = st.text_area(
            "本日の重要ニュース（手入力・1行1件）",
            value=st.session_state.get("manual_news_lines",""),
            placeholder="例）米大統領選TV討論会\n中東地政学リスク再燃\n大規模停電 など",
            key="manual_news_lines", height=96,
        )
    with col2:
        st.write(""); st.write("")
        fetch_now = st.button("AIニュース（RSS）を取得/更新", key="btn_fetch_ai_news")
        n_items = st.number_input("AI候補：最大件数", 3, 20, 10, step=1, key="ai_news_max_items")

    if fetch_now:
        st.session_state["ai_news_candidates"] = _fetch_fx_related_news(int(n_items))

    cand = st.session_state.get("ai_news_candidates", [])
    if cand:
        st.caption("AI候補ニュース（複数選択可）")
        view = [f"{i+1}. {_clean_news_title_for_prompt(c['title'])}（{c.get('source','')}）" for i, c in enumerate(cand)]
        st.multiselect(
            "採用するAI候補ニュースを選択",
            options=list(range(len(view))),
            format_func=lambda i: view[i],
            default=st.session_state.get("ai_news_selected_idx", [])[:3],
            key="ai_news_selected_idx"
        )
    else:
        st.info("『AIニュース（RSS）を取得/更新』を押すと候補が表示されます。")

    colA, colB, colC = st.columns([1,1,2])
    with colA:
        gen_title = st.button("AIでタイトル案を生成", key="btn_ai_title")
    with colB:
        apply_title = st.button("↑ このタイトルを適用", key="btn_apply_title")
    with colC:
        period_only = st.checkbox("③は『回収文のみ句点』にする", value=st.session_state.get("recall_period_only", False), key="recall_period_only")

    if gen_title:
        preview_all = "\n".join([str(globals().get("p1","")), str(globals().get("p2","")), str(st.session_state.get("calendar_line",""))])
        manual_list, picked = _selected_news_strings()
        pair_name = str(globals().get("pair",""))
        base_tail = str(st.session_state.get("title_tail") or "")
        t, r = _ai_title_and_recall(preview_all, manual_list, picked, base_tail, pair_name)
        st.session_state["ai_title_draft"] = t
        st.session_state["ai_recall_draft"] = r

    ai_t = st.text_input("AI提案タイトル（編集可）", value=st.session_state.get("ai_title_draft", ""), key="ai_title_draft_text")
    ai_r = st.text_area("AI提案：タイトル回収（編集可・句点なし推奨）", value=st.session_state.get("ai_recall_draft", ""), height=66, key="ai_recall_draft_text")

    if apply_title:
        new_t = _clean_text_jp_safe(st.session_state.get("ai_title_draft_text","")).strip("。")
        if new_t:
            globals()["title"] = new_t
            st.session_state["title"] = new_t
        new_r_raw = _clean_text_jp_safe(st.session_state.get("ai_recall_draft_text","")).rstrip("。")
        st.session_state["ai_title_recall_final"] = _strip_media_brackets(new_r_raw)
        st.success("AIタイトル＆回収を適用しました。下のプレビューを更新してください。")

    # --- ここでAI使用サインを表示 ---
    _af = _ai_flags()
    badge = f"🔎 AI使用状況｜LLM: {'✅' if _af['llm_used'] else '—'} / RSS: {'✅' if _af['rss_used'] else '—'} / 概算Tokens: ~{_af['tokens_est']}"
    st.info(badge)
    if _af.get("last_error"):
        st.warning(f"LLM呼び出しエラー: {_af['last_error']}")

# --- タイトル最終確定（AI適用を反映） ---
ttl_display = (str(globals().get("title", "")).strip() or str(globals().get("default_title", "")).strip())
if not ttl_display:
    base = str(st.session_state.get("pair", "") or "ポンド円")
    tail = (st.session_state.get("title_tail") if hasattr(st, "session_state") else None) or "注視か"
    ttl_display = f"{base}の方向感に{tail}"
ttl_display = _clean_text_jp_safe(ttl_display)

# --- 本日のポイント（FXONデータから略式付で再構成：2件） ---
def _build_points_from_fxon() -> list[str]:
    src = _events_df_like()
    if src is None: return []
    if hasattr(src, "to_dict"):
        try: records = src.to_dict(orient="records")
        except Exception: records = []
    else:
        records = list(src)

    pts = []
    for r in records:
        t_raw = _pick(r, ["時刻","time","local_time","datetime","start_at","start"])
        title = _pick(r, ["指標","indicator","title","name"], "")
        abbr  = _abbr_from_row(r)
        hhmm  = _extract_hhmm(t_raw)
        if not title or not hhmm: continue
        ttl = _tidy_label(_strip_country_prefix(title))
        pts.append((hhmm, f"{_normalize_time_str(hhmm)} に{(abbr + '・') if abbr else ''}{ttl}"))
    pts.sort(key=lambda x: (int(x[0].split(':')[0]), int(x[0].split(':')[1])))
    out = [p[1] for p in pts[:2]]
    out = [re.sub(r'([0-2]?\d:[0-5]\d)\s*に\s*', r'\1 に', x) for x in out]
    return out

_pts_fx = _build_points_from_fxon()
if _pts_fx and not st.session_state.get("points_tags_v2"):
    st.session_state["points_tags_v2"] = _pts_fx

def _norm_point_line(s: str) -> str:
    return re.sub(r'^\s*([0-9]{1,2}:[0-9]{2})\s*に\s*', r'\1 に', _nfkc(s))
points = list(st.session_state.get("points_tags_v2", []) or [])[:2]
points = [_norm_point_line(p) for p in points]
point1 = points[0] if len(points) > 0 else ""
point2 = points[1] if len(points) > 1 else ""

# --- ①にポイント未言及なら一言だけ挿入（既存） ---
def _mentions_points(s: str, items: list[str]) -> bool:
    if not s or not items: return True
    body = str(s).replace(" ", "")
    for it in items:
        key = (it.split("に", 1)[-1] or "").replace(" ", "")
        if key and key in body: return True
    return False

_pts_short = [re.sub(r'(^|[^\d])([0-9]{1,2}:[0-9]{2})\s*に', r'\1\2 に', x) for x in points if x]
if _pts_short and not _mentions_points(globals().get("p1", ""), points):
    p1 = re.sub(r"本日は[^。]*?留意したい。", "", str(globals().get("p1", ""))).strip()
    hint = "本日は" + "と".join(_pts_short) + "が控えており、短期の振れに留意したい。"
    if p1 and not p1.endswith("。"): p1 += " "
    p1 += hint
else:
    p1 = str(globals().get("p1", "") or "")

# --- ② 最低文字数＋末尾整合（既存ロジック温存） ---
def _allowed_closers() -> list[str]:
    if "ALLOWED_PARA2_CLOSERS" in globals() and ALLOWED_PARA2_CLOSERS:
        return list(ALLOWED_PARA2_CLOSERS)
    return ["行方を注視したい。","値動きには警戒したい。","当面は静観としたい。","一段の変動に要注意としたい。","方向感を見極めたい。"]

def _ends_with_closer(s: str) -> bool:
    return any(str(s).endswith(c) for c in _allowed_closers())

def pad_para2(para2: str, min_chars: int = 180) -> str:
    base = str(para2 or "").strip()
    if len(base) >= min_chars: return base
    addon = "短期は20SMAやボリンジャーバンド周辺の反応を確かめつつ、過度な方向感は決めつけない構えとしたい。"
    if addon not in base:
        if base and not base.endswith("。"): base += " "
        base += addon
    return base

p2 = str(globals().get("p2", "") or "")
p2 = pad_para2(p2, min_chars=180)
tail = (st.session_state.get("title_tail") or "").strip()
closer_map = {
    "注視か": "行方を注視したい。", "警戒か": "値動きには警戒したい。", "静観か": "当面は静観としたい。",
    "要注意か": "一段の変動に要注意としたい。", "見極めたい": "方向感を見極めたい。",
}
desired = closer_map.get(tail, "方向感を見極めたい。")
p2 = re.sub(r"(行方を注視したい。|値動きには警戒したい。|当面は静観としたい。|一段の変動に要注意としたい。|方向感を見極めたい。)$", "", p2).rstrip("。") + "。" + desired
p2 = (p2 or "").strip().rstrip("。") + "。"

para1_clean = _clean_text_jp_safe(str(p1).strip())
para2_clean = _clean_text_jp_safe(str(p2).strip())

# --- 段落②のAI補正（任意）：日足 → 4時間足の順に自然文で整形 ---
def _ai_refine_para2_d1_h4(p2_text: str, pair_name: str) -> str:
    prompt = (
        "次の段落②の文章を、必ず『日足の内容を先に、次に4時間足の内容』の順に並べ替え、"
        "重複する主語は省きつつ自然な日本語で整えてください。"
        "・断定は避ける・助言はしない・句点で終える・専門語は維持（RSI/ボリンジャーバンド/SMA/EMA など）。\n\n"
        f"【通貨ペア】{pair_name}\n【段落②】\n{_clean_text_jp_safe(p2_text)}"
    )
    out = _call_llm_with_flags(prompt)
    if isinstance(out, str) and out.strip():
        return _clean_text_jp_safe(out).rstrip("。") + "。"
    # フォールバック（簡易）
    s = _clean_text_jp_safe(p2_text)
    s = re.sub(r"為替市場は、\s*", "", s)
    sents = [x for x in re.split(r"[。]+", s) if x.strip()]
    d1 = [x for x in sents if "日足" in x]
    h4 = [x for x in sents if "4時間足" in x or "４時間足" in x]
    other = [x for x in sents if x not in d1 + h4]
    out = "。".join(d1 + h4 + other).strip()
    return (out + "。") if out else p2_text

use_llm_refine = bool(globals().get("use_llm_refine", False))
para2_final = para2_clean
if use_llm_refine and len(para2_final) < 180:
    try:
        out_tmp = _call_llm_with_flags(
            "次の日本語テキストを、断定を避ける筆致のまま180〜220字程度に自然に補筆してください。"
            "ボリンジャーバンドやSMA/EMAの語彙は維持し、売買助言はしないこと。末尾は句点。\n\n【テキスト】\n" + para2_final
        )
        if isinstance(out_tmp, str) and len(out_tmp.strip()) >= 180:
            para2_final = out_tmp.strip()
    except Exception:
        pass

# 並べ替え（任意ボタン）
with st.expander("AI補正（段落②を『日足→4時間足』順に整える・任意）", expanded=False):
    if st.button("段落②を整形して適用", key="btn_refine_para2_d1h4"):
        pair_name = str(globals().get("pair",""))
        refined = _ai_refine_para2_d1_h4(para2_final, pair_name)
        st.session_state["para2_final"] = refined
        st.success("段落②を『日足→4時間足』順に整えました。下のプレビューを更新してください。")

# 最終クリンナップ
para2_final = _clean_text_jp_safe(st.session_state.get("para2_final", para2_final))
para2_final = pad_para2(para2_final, 180)
if not _ends_with_closer(para2_final):
    if not para2_final.endswith("。"): para2_final += "。"
    para2_final += "方向感を見極めたい。"

if "_final_polish_and_guard" in globals() and callable(globals().get("_final_polish_and_guard")):
    para1_final = _final_polish_and_guard(para1_clean, para="p1")
    para2_final = _final_polish_and_guard(para2_final, para="p2")
    para2_final = pad_para2(para2_final, 180)
else:
    para1_final = _clean_text_jp_safe(para1_clean)
    para2_final = _clean_text_jp_safe(para2_final)
st.session_state["para1_final"] = para1_final
st.session_state["para2_final"] = para2_final

# --- ③＋タイトル回収（同一行・4引数OK） ---
def _make_cal_plus_recall(cal_src: str, ttl: str, preview_text: str = None, manual_news: str = "") -> str:
    """
    ③の1行を構成：
      - 本日の指標（cal_src）
      - タイトル回収（手入力/AI補正の結果を優先）
      - 句点ルール：『回収文のみ句点』オプションに対応
    2引数/4引数どちらの呼び出しでもOK。
    """
    cs = _nfkc(cal_src or "").strip()
    cs = re.sub(r"[。．]+$", "", cs)
    period_only = bool(st.session_state.get("recall_period_only", False))
    if cs:
        cal_txt = f"本日の指標は、{cs}が発表予定となっている"
        cal_txt += (" " if period_only else "。")
    else:
        cal_txt = "本日の指標は、" if period_only else "本日の指標は、。"

    recall = str(st.session_state.get("ai_title_recall_final", "") or "").strip()

    if not recall and "llm_complete" in globals() and callable(globals().get("llm_complete")):
        prompt = (
            "以下の素材から、タイトル回収の一文（50〜90字程度）を1つ作成してください。"
            "断定は避け、助言はしない。末尾は句点。和文で。媒体名や日付、URLは書かない。\n\n"
            f"【タイトル】{_clean_text_jp_safe(ttl)}\n"
            f"【手入力ニュース】{_clean_text_jp_safe(manual_news or '')}\n"
            "【プレビュー本文】\n" + _clean_text_jp_safe((preview_text or "")[:1200])
        )
        out = _call_llm_with_flags(prompt)
        if isinstance(out, str) and out.strip():
            recall = out.strip().rstrip("。")

    if not recall:
        base = _clean_text_jp_safe(ttl or "")
        recall = base.replace("に注視か", "の行方を注視したい") \
                     .replace("に警戒か", "値動きには警戒したい") \
                     .replace("に静観か", "当面は静観としたい") \
                     .replace("見極めたい", "方向感を見極めたい")
        recall = recall.rstrip("。")

    recall = _strip_media_brackets(recall)
    out = cal_txt + _clean_text_jp_safe(recall).rstrip("。") + "。"
    out = re.sub(r"([。])\1+", r"\1", out)
    return out

cal_line_src = str(st.session_state.get("calendar_line", "") or "").strip()
_preview_for_recall = "\n".join([
    f"ポイント: {', '.join(x for x in (st.session_state.get('points_tags_v2') or []) if x)}",
    f"段落①: {para1_final}",
    f"段落②: {para2_final}",
])
cal_plus_recall = _make_cal_plus_recall(
    cal_line_src, ttl_display, _preview_for_recall, st.session_state.get("manual_news_lines","")
)

# --- レポート本文の組み立て（既存関数があれば使用） ---
report_final = ""
try:
    try:
        report_final = render_report(
            title=ttl_display,
            point1=point1, point2=point2,
            para1=para1_final, para2=para2_final,
            calendar_line=cal_line_src,
            title_recall=cal_plus_recall,
        )
    except TypeError:
        report_final = render_report(
            title=ttl_display,
            point1=point1, point2=point2,
            para1=para1_final, para2=para2_final,
            cal_line=cal_line_src,
            title_recall=cal_plus_recall,
        )
except Exception:
    lines = []
    if ttl_display: lines += [f"タイトル：{ttl_display}", ""]
    if points:      lines += ["本日のポイント", *points, ""]
    if para1_final: lines += [para1_final, ""]
    if para2_final: lines += [para2_final, ""]
    lines += [cal_plus_recall]
    report_final = "\n".join(lines).strip()

# --- ③重複除去＆末尾1本に統一 ---
def _compact_final_text(s: str, ttl: str) -> str:
    t = (s or "").strip()
    t = re.sub(r"本日の指標は、.*?(?=\n\n|$)", "", t, flags=re.S).strip()
    if ttl:
        try:
            solo = (build_title_recall(ttl) or ttl).strip()
        except Exception:
            solo = ttl
        solo = re.escape(_nfkc(solo).rstrip("。"))
        t = re.sub(rf"(?m)^\s*{solo}。?\s*$\n?", "", t)
    t = re.sub(r"(?:\n\s*){3,}", "\n\n", t)
    t = re.sub(r"([。])\1+", r"\1", t)
    return t.strip()

body = _compact_final_text(report_final, ttl_display)
if body and not body.endswith("\n\n"): body += "\n\n"
report_final = (body + cal_plus_recall).strip()

# --- 唯一ソースに固定 & プレビュー表示 ---
st.session_state["report_final_main"] = str(report_final or "").strip()
text_to_show = st.session_state["report_final_main"]

if "preview_report_base" not in st.session_state:
    st.session_state["preview_report_base"] = text_to_show
if st.session_state["preview_report_base"] != text_to_show:
    st.session_state["preview_report_base"] = text_to_show
    st.session_state["preview_report_main"] = text_to_show
_base_preview = st.session_state.get("preview_report_base", "")

# --- Pre-AIプレビュー ---
try:
    if "_final_polish_and_guard" in globals() and callable(globals().get("_final_polish_and_guard")):
        para1_pre = _final_polish_and_guard(globals().get("para1_clean", para1_final), para="p1")
    else:
        para1_pre = _clean_text_jp_safe(globals().get("para1_clean", para1_final))
    src_p2 = str(globals().get("para2_clean", para2_final))
    pre_p2 = pad_para2(src_p2, 180)
    if not _ends_with_closer(pre_p2):
        if not pre_p2.endswith("。"): pre_p2 += "。"
        pre_p2 += "方向感を見極めたい。"
    if "_final_polish_and_guard" in globals() and callable(globals().get("_final_polish_and_guard")):
        pre_p2 = _final_polish_and_guard(pre_p2, para="p2")
    else:
        pre_p2 = _clean_text_jp_safe(pre_p2)
    cal_line_pre = _make_cal_plus_recall(
        cal_line_src, ttl_display, _preview_for_recall, st.session_state.get("manual_news_lines","")
    )
    pre_lines = []
    if ttl_display: pre_lines += [f"タイトル：{ttl_display}", ""]
    if points:      pre_lines += ["本日のポイント", *points, ""]
    if para1_pre:   pre_lines += [para1_pre, ""]
    if pre_p2:      pre_lines += [pre_p2, ""]
    pre_lines += [cal_line_pre]
    pre_text = "\n".join(pre_lines).strip()
except Exception:
    pre_text = _base_preview

tab1, tab2 = st.tabs(["Pre-AI版プレビュー", "AI後プレビュー"])
with tab1:
    st.text_area("Pre-AI本文", value=pre_text, height=420, key="pre_ai_preview", disabled=True)
with tab2:
    st.text_area("プレビュー", value=_base_preview, height=420, key="preview_report_main")

if st.session_state.get("preview_report_main", "") != st.session_state.get("preview_report_base", ""):
    st.session_state["preview_report_main"] = st.session_state["preview_report_base"]
    try: st.rerun()
    except Exception: st.experimental_rerun()

# --- 体裁チェック ---
try:
    guards = CFG.get("text_guards", {}) or {}
except Exception:
    guards = {}
p1_min = int(guards.get("p1_min_chars", 220))
p2_min = int(guards.get("p2_min_chars", 180))

def _norm_for_check(s: str) -> str:
    if "_canon_normalize" in globals() and callable(globals().get("_canon_normalize")):
        try:
            return _canon_normalize(s)
        except Exception:
            pass
    return _nfkc(s)

p1_check = _norm_for_check(para1_final).replace("\n", "")
p2_check = _norm_for_check(para2_final).replace("\n", "")

viol = []
if len(p1_check) < p1_min: viol.append("段落①が規定文字数に未達")
if len(p2_check) < p2_min: viol.append("段落②が規定文字数に未達")
ban_words = ["買い","売り","ロング","ショート","損切り","推奨","おすすめ","必勝"]
if any(w in (para1_final + para2_final) for w in ban_words): viol.append("売買助言に該当し得る語が含まれる")

last_block = report_final.split("\n\n")[-1].strip() if report_final else ""
if not (last_block.startswith("本日の指標は、") and last_block.endswith("。")):
    viol.append("段落③の体裁が不正（本日の指標は、〜 で始まり句点で終える必要）")
# タイトル回収の一致チェックはAI回収により形が変わるためスキップ

if viol:
    st.error("体裁チェック NG：" + " / ".join(viol))
else:
    st.success("体裁チェック OK（①≥220字／②≥180字／③は1行で回収まで同一行・句点で終える）。")
st.session_state["__final_check_done"] = True

# --- 保存 ---
out_dir = Path("./out"); out_dir.mkdir(parents=True, exist_ok=True)
fname = f"m{datetime.now():%Y%m%d}.txt"
save_path = out_dir / fname
try:
    save_path.write_text(text_to_show, encoding="utf-8")
    st.success(f"保存しました：{fname}")
    st.download_button("このプレビューをダウンロード",
                       data=text_to_show.encode("utf-8"),
                       file_name=fname, mime="text/plain",
                       key="dl_report_unified")
except Exception as e:
    st.warning(f"保存時のエラー：{e}")

# --- 監査ログ ---
try:
    report_text = str(st.session_state.get("report_final_main", "")).strip()
    title_guess = ""
    if report_text:
        first_line = report_text.splitlines()[0]
        m = re.search(r"^タイトル：(.+)$", first_line)
        if m: title_guess = m.group(1).strip()
    if not title_guess:
        title_guess = str(globals().get("ttl_display", "")
                          or globals().get("title", "")
                          or globals().get("default_title", "")).strip()
    points_log = list(st.session_state.get("points_tags_v2", []) or [])[:2]
    cal_log = str(st.session_state.get("calendar_line", "") or "").strip()
    checks = st.session_state.get("checks_failed", [])
    live_diag = globals().get("live_diag", {});  te_diag = globals().get("te_diag", {})
    if not isinstance(live_diag, dict): live_diag = {}
    if not isinstance(te_diag, dict):   te_diag = {}
    log = {
        "ts": datetime.now().isoformat(),
        "pair": str(st.session_state.get("pair", "")),
        "title": title_guess,
        "points": points_log,
        "calendar_line": cal_log,
        "preview_len": len(report_text),
        "checks_failed": checks,
        "ai_flags": _ai_flags(),
        "live_diag": live_diag,
        "te_diag": te_diag,
    }
    log_dir = Path("outlog"); log_dir.mkdir(parents=True, exist_ok=True)
    log_name = f"log_{datetime.now():%Y%m%d_%H%M%S}.json"
    (log_dir / log_name).write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
except Exception as e:
    st.warning(f"監査ログの保存に失敗しました: {e}")
