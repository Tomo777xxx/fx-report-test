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



# ===== app.py 冒頭〜Step1直前（安全化・重複解消 版） =====
import os
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
import json
import random
import pandas as pd
import streamlit as st
try:
    import yaml
except Exception:
    yaml = None  # 未インストールでもアプリは落とさない

import numpy as np
import yfinance as yf


# ---------- yfinance ベースの TA 計算（安全版：4Hは1Hからリサンプリング） ----------
import pandas as pd, numpy as np, yfinance as yf


# ---------- 置き換えここまで ----------

# === Canonical helpers (追加のみ／既存の呼び出しはまだ変更しない) ===
from datetime import timezone as _tz, timedelta as _td

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

# ここから画面のヘッダ部
st.set_page_config(page_title=CFG["app"]["title"], layout="centered")

# LLM稼働インジケーター（鍵の存在だけを表示）
_has_key = ("OPENAI_API_KEY" in st.secrets) or ("general" in st.secrets and "OPENAI_API_KEY" in st.secrets["general"])
st.caption(f"LLM: {'ON' if _has_key else 'OFF (OpenAIキー未設定)'}")

st.title(CFG["app"]["title"])

# ▼この生成で LLM を使えたか（USED / FALLBACK）を表示
badge = ("USED ✅" if st.session_state.get("llm_used") is True
         else "FALLBACK ⚠️" if st.session_state.get("llm_used") is False
         else "–")
st.caption(f"LLM call: {badge}")
if st.session_state.get("llm_error"):
    st.caption(f"LLM note: {st.session_state['llm_error']}")

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

# ====== サイドバー（主役ペア / NFPカウントダウン） ======

PAIRS = [
    "ドル円", "ユーロドル", "ユーロ円", "ポンドドル", "ポンド円",
    "豪ドル米ドル", "NZドル米ドル", "金/米ドル", "ビットコイン/米ドル"
]
# 目的：レポートの骨格となる「主役ペア」を人が選択し、タイトル語尾はホワイトリストから安全に選ぶ（LLM使用時も候補外は出さない）
with st.sidebar:
    st.subheader("主役ペア / タイトル語尾")
    pair = st.selectbox("主役ペア", PAIRS, index=4)  # 既定：ポンド円
    st.markdown("---")

    # 目的：次回NFPまでの案内（公式があれば公式／無ければ目安）
    st.subheader("NFPカウントダウン")
    _today = date.today()
    _nfp = next_nfp_official_or_rule(_today)   # ← 公式優先に切替済み
    _days_left = (_nfp - _today).days

    # 公式スケジュール内に一致があれば「公式」、無ければ「目安」
    _sched = _load_bls_empsit_schedule()
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
        if "レンジ" not in s and "持ち合い" not in s:
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
# ===== タイトル・結び・ルールダイジェスト（丸ごと置換用） =====
import os
import random
from pathlib import Path
import streamlit as st

# 1) ホワイトリスト（LLMが"候補外"を出しても採用しない安全設計）
ALLOWED_TITLE_TAILS = ["注視か", "警戒か", "静観か", "要注意か", "見極めたい"]

ALLOWED_PARA2_CLOSERS = [
    "行方を注視したい。", "値動きには警戒したい。", "当面は静観としたい。",
    "一段の変動に要注意としたい。", "方向感を見極めたい。"
]

# 2) ルール要約の読込（存在しなければ空文字）
def _read_rules_digest(path: str | Path = "data/rules_digest.txt") -> str:
    p = Path(path)
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    return text[:2000]

RULES_DIGEST = _read_rules_digest()

# 3) APIキー取得（既存の _get_api_key があればそれを使う）
try:
    _get_api_key  # 既存が定義済みならそれを使う
except NameError:
    def _get_api_key() -> str:
        # st.secrets 優先 → [general] → 環境変数
        if "OPENAI_API_KEY" in st.secrets:
            return st.secrets["OPENAI_API_KEY"]
        if "general" in st.secrets and "OPENAI_API_KEY" in st.secrets["general"]:
            return st.secrets["general"]["OPENAI_API_KEY"]
        return os.environ.get("OPENAI_API_KEY", "")

# 4) LLM呼び出し（今回の生成で使えたかを session_state に記録）
def _llm_pick_from_list(system_msg: str, user_msg: str) -> str | None:
    # この生成での状態を初期化
    st.session_state["llm_used"] = None
    st.session_state["llm_error"] = None

    # APIキー取得
    try:
        api_key = _get_api_key()
    except Exception as e:
        st.session_state["llm_used"] = False
        st.session_state["llm_error"] = f"key error: {e}"
        return None

    if not api_key:
        st.session_state["llm_used"] = False
        st.session_state["llm_error"] = "No OPENAI_API_KEY"
        return None

    try:
        # OpenAIクライアント
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        # ★モデルとパラメータ（gpt-5 と 4o 系で自動出し分け）
        MODEL_NAME = "gpt-5"  # 必要に応じて 'gpt-4o-mini' などに変更可
        is_gpt5 = str(MODEL_NAME).startswith("gpt-5")

        kwargs = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ],
            # gpt-5 は temperature のカスタム不可（デフォルトのみ）なので渡さない
            # 4o 系はこれまで通り設定してOK
            **({} if is_gpt5 else {"temperature": 0.2}),
        }

        # 出力トークン上限の指定もモデルで分岐
        if is_gpt5:
            kwargs["max_completion_tokens"] = 16
        else:
            kwargs["max_tokens"] = 16

        resp = client.chat.completions.create(**kwargs)


        # テキスト抽出
        text = (resp.choices[0].message.content or "").strip().replace("\n", "")

        # 見える化メタ
        st.session_state["llm_used"] = True
        try:
            st.session_state["llm_model"] = getattr(resp, "model", None) or MODEL_NAME
        except Exception:
            st.session_state["llm_model"] = MODEL_NAME
        try:
            u = getattr(resp, "usage", None)
            st.session_state["llm_tokens"] = (
                (getattr(u, "prompt_tokens", 0) or 0)
                + (getattr(u, "completion_tokens", 0) or 0)
            )
        except Exception:
            st.session_state["llm_tokens"] = None

        return text

    except Exception as e:
        st.session_state["llm_used"] = False
        st.session_state["llm_error"] = str(e)[:240]
        return None


# 5) タイトル語尾の選択（LLM→ホワイトリスト検証→不一致ならランダムにフォールバック）
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

# 6) 段落②の結びの選択（同様）
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

# ===== ここまでを既存ブロックと置換 =====


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

_ALIAS_CACHE = None

def _load_indicator_alias(path: str | Path = "data/indicator_alias_ja.yaml") -> dict:
    """YAMLのエイリアス辞書を読む。無ければ空でOK。"""
    global _ALIAS_CACHE
    if _ALIAS_CACHE is not None:
        return _ALIAS_CACHE
    p = Path(path)
    if not p.exists():
        _ALIAS_CACHE = {"exact": {}, "contains": {}}
        return _ALIAS_CACHE
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _ALIAS_CACHE = {
            "exact": data.get("exact", {}) or {},
            "contains": data.get("contains", {}) or {},
        }
    except Exception:
        _ALIAS_CACHE = {"exact": {}, "contains": {}}
    return _ALIAS_CACHE

def _region_symbol(code: str) -> str:
    """地域コード→接頭語（本文表記に合わせる）"""
    m = {"US":"米","JP":"日","EU":"欧","UK":"英","AU":"豪","NZ":"NZ","CN":"中国","ZA":"南ア"}
    c = (code or "").upper()
    return m.get(c, c)

def _ja_indicator_name(raw: str, region_code: str) -> str:
    """原文指標名を日本語に寄せる（辞書ヒット時のみ）。常に『接頭語・名称』で返す。"""
    name = (raw or "").strip()
    alias = _load_indicator_alias()
    # 完全一致の置換
    if name in alias["exact"]:
        name = alias["exact"][name]
    else:
        # 部分一致の置換（順番に最初だけ適用）
        for key, val in alias["contains"].items():
            if key in name:
                name = name.replace(key, val)
                break
    head = _region_symbol(region_code)
    # 既に「◯・」が付いていたら二重にならないよう簡単ガード
    if name.startswith(f"{head}・"):
        return name
    return f"{head}・{name}" if head else name
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
# 注意：_ja_indicator_name はこのファイル内で二重定義。
# いまは「この後ろの定義」が有効（上書き）になっています。
# 次の整理ステップで片方に統一します（呼び出し差し替え時に動作差分を確認予定）。
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


# ====== ステップ1：参照PDFの確認 ======


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

def _load_events_csv(path: Path) -> list[dict]:
    """CSV: 列は『時刻, 指標, 地域, カテゴリ』想定。存在しなければ [] を返す。"""
    try:
        df = pd.read_csv(path, encoding="utf-8")
        req = {"時刻","指標","地域","カテゴリ"}
        if not req.issubset(df.columns):
            st.warning(f"CSV列名が不足: 必要{req} / 実際{set(df.columns)}")
            return []
        df["時刻"] = df["時刻"].astype(str).str.strip()
        df["指標"] = df["指標"].astype(str).str.strip()
        df["地域"] = df["地域"].astype(str).str.strip()
        df["カテゴリ"] = df["カテゴリ"].astype(str).str.strip()
        return df[["時刻","指標","地域","カテゴリ"]].to_dict("records")
    except FileNotFoundError:
        return []
    except Exception as e:
        st.warning(f"CSV読込エラー: {e}")
        return []


# ===== TE API: シークレット取得 / JST / 国コード変換 / 本体 =====
import requests
from datetime import datetime as _dt, date as _date_cls, timezone as _tz, timedelta as _td

# JSTタイムゾーン
JST = _tz(_td(hours=9))

def _get_secret_value(name: str) -> str | None:
    """secrets → [general] → 環境変数 の順で安全に読む。"""
    try:
        if name in st.secrets:
            return st.secrets[name]
        if "general" in st.secrets and name in st.secrets["general"]:
            return st.secrets["general"][name]
    except Exception:
        pass
    import os
    return os.environ.get(name)

# 互換: もし既存コードに _get_secret(...) 呼び出しが残っていても動くようエイリアスを用意
def _get_secret(name: str) -> str | None:
    return _get_secret_value(name)



def _region_code_to_jp_prefix(code: str) -> str:
    """地域コード→日本語接頭辞（米・日・欧・英・豪・南ア など）。なければそのままコード。"""
    code = (code or "").upper()
    jp = {
        "US": "米",
        "JP": "日",
        "EU": "欧",
        "UK": "英",
        "AU": "豪",
        "NZ": "NZ",   # 慣例上そのまま表記でもOK
        "CN": "中",
        "ZA": "南ア",
        "DE": "独",
        "FR": "仏",
        "IT": "伊",
        "CA": "加",
        "ES": "西",
        "CH": "スイス",
    }
    return jp.get(code, code)

def _parse_te_datetime_to_jst_hhmm(date_str: str) -> str:
    """TEの日時文字列を JST の 'H:MM' へ。失敗時は空文字。"""
    if not date_str:
        return ""
    # 例: "2025-08-13T09:00:00Z" / "2025-08-13T09:00:00"
    try:
        ts = date_str.replace("Z", "+00:00")
        dt = _dt.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        j = dt.astimezone(JST)
        return f"{j.hour}:{j.minute:02d}"
    except Exception:
        pass
    # 例: "2025-08-13 09:00:00"
    try:
        dt = _dt.strptime(date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz.utc)
        j = dt.astimezone(JST)
        return f"{j.hour}:{j.minute:02d}"
    except Exception:
        return ""

# ---- TradingEconomics 当日イベント取得（診断つき）----
from datetime import datetime, date, timedelta, timezone
import requests

JST = timezone(timedelta(hours=9))

def _get_secret(name: str, default: str | None = None) -> str | None:
    # secrets.toml → 環境変数 → default の順で探す
    try:
        import streamlit as st
        v = st.secrets.get(name)
        if v:
            return v
    except Exception:
        pass
    import os
    return os.environ.get(name, default)


    c = (country or "").lower()
    if "united states" in c or c in {"us","u.s.","usa","u.s.a."}: return "US"
    if "japan" in c or c in {"jp","jpn"}: return "JP"
    if "euro" in c or c in {"eu","euro area","eurozone"}: return "EU"
    if "united kingdom" in c or c in {"uk","gb","gbr"}: return "UK"
    if "australia" in c or c in {"au","aus"}: return "AU"
    if "new zealand" in c or c in {"nz","nzl"}: return "NZ"
    if "china" in c or c in {"cn","chn"}: return "CN"
    if "south africa" in c or c in {"za","zaf"}: return "ZA"
    return (country or "")[:2].upper()

def _parse_te_datetime(s: str) -> datetime | None:
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None

from datetime import datetime, timezone, timedelta, date
import requests

JST = timezone(timedelta(hours=9))

def _get_secret(name: str) -> str | None:
    # 既にある同名の補助関数があればそれを使ってOK
    try:
        import os, streamlit as st
        return st.secrets.get(name) or os.environ.get(name)
    except Exception:
        import os
        return os.environ.get(name)


def _fetch_events_tradingeconomics(target_date: date, cfg: dict):
    """
    TradingEconomics カレンダーAPIから target_date のイベントを取得。
    時刻が取れない行も捨てずに '時刻' は '--:--' として返す。
    返り値: (rows, diag)
      - rows: [{"時刻","指標","地域","カテゴリ"}, ...]
      - diag: 診断情報（UIの expander に表示）
    """
    prov = (cfg.get("providers") or {}).get("tradingeconomics") or {}
    if not prov.get("enabled"):
        return [], {"enabled": False}

    endpoint = prov.get("endpoint") or "https://api.tradingeconomics.com/calendar"
    key_env  = prov.get("key_env")  or "TE_API_KEY"
    timeout  = prov.get("timeout_sec", 12)

    api_key = _get_secret(key_env)
    if not api_key:
        api_key = "guest:guest"  # 無料ゲストでまずは動かす

    # APIの日付はUTC基準ですが、まずは素直に target_date の1日を指定
    d1 = target_date.strftime("%Y-%m-%d")
    d2 = d1
    params = {"c": api_key, "d1": d1, "d2": d2, "format": "json"}

    url = endpoint
    http_status = None
    err_msg = None
    data = []
    try:
        r = requests.get(url, params=params, timeout=timeout)
        http_status = r.status_code
        r.raise_for_status()
        data = r.json() if r.content else []
    except Exception as e:
        err_msg = str(e)
        return [], {
            "enabled": True, "endpoint": endpoint, "key_env": key_env,
            "has_key": bool(api_key), "d1": d1, "d2": d2,
            "http_status": http_status, "error": err_msg, "api_raw_count": 0,
        }

    rows = []
    filtered_blank_time = 0
    for it in (data or []):
        # 代表的なフィールド名に対応
        indicator = (it.get("Event") or it.get("Indicator") or "").strip()
        country   = (it.get("Country") or "").strip()
        category  = (it.get("Category") or "").strip()
        date_str  = it.get("Date") or it.get("DateUTC") or it.get("DateISO") or ""

        # JSTの H:MM を生成（うまく読めなければ 空 → 後で '--:--' に置換）
        hhmm = ""
        if date_str:
            dt = None
            try:
                dt = datetime.fromisoformat(date_str.replace("Z","+00:00"))
            except Exception:
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except Exception:
                    dt = None
            if dt:
                j = dt.astimezone(JST)
                hhmm = f"{j.hour}:{j.minute:02d}"

        if not indicator:
            continue

        # ★ここがポイント：時刻が空でも捨てない（'--:--' にする）
        if not hhmm:
            filtered_blank_time += 1
            hhmm = "--:--"

        rows.append({
            "時刻": hhmm,
            "指標": indicator,
            "地域": CANON_map_country_to_region(country),
            "カテゴリ": category or "",
        })

    diag = {
        "enabled": True, "endpoint": endpoint, "key_env": key_env,
        "has_key": bool(api_key), "d1": d1, "d2": d2,
        "http_status": http_status, "error": err_msg,
        "api_raw_count": len(data or []),
        "returned_rows": len(rows),
        "blank_time_to_dash": filtered_blank_time,  # 空時刻を '--:--' にした件数
        "requested_url": r.url if 'r' in locals() else url,
    }
    return rows, diag





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
st.markdown("### ステップ3：本文の下書き（編集可）")

import re, unicodedata

# ---- 設定の最小文字数（CFGが無くても動く）----
try:
    _tg = (CFG.get("text_guards") or {})
    P1_MIN = int(_tg.get("p1_min_chars", 220))
    P2_MIN = int(_tg.get("p2_min_chars", 180))
except Exception:
    P1_MIN, P2_MIN = 220, 180

# ---- ヘルパ ----
def _clean_text_jp_safe(s):
    try:
        if "_clean_text_jp" in globals():
            return _clean_text_jp(s)
    except Exception:
        pass
    if not isinstance(s, str):  # 簡易版
        return s
    s = s.replace("\u3000", " ")
    for a, b in [("  ", " "), (" 、", "、"), (" 。", "。"), ("、、", "、"), ("。。", "。")]:
        s = s.replace(a, b)
    return s.strip()

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
    base = str(para2 or "").strip()
    if len(base) >= min_chars:
        return base
    addon = "短期は過度に方向感を決めつけず、節目やボリンジャーバンド周辺の反応を確かめつつ、値動きには警戒したい。"
    if base and not base.endswith("。"):
        base += " "
    base += addon
    return base

# ---- Step5で選んだポイント（最大2件）----
points_items = list(st.session_state.get("points_tags_v2", []) or [])[:2]

# ===== 段落① =====
default_para1 = (
    "昨日は、米国市場で主要株価指数3銘柄のうち2銘柄が上昇となり、株高・金利安・原油横ばいの相場展開となった。"
    "原油WTIは65.5ドル付近にて停滞。一方の天然ガスは、前日から0.78%下落。3.29ドル台まで落ち込んだ。"
    "主要貴金属5銘柄はプラチナ以外が上昇となり、唯一下落したプラチナは、前日から1.01%低下。1,482ドル台で推移した。"
    "米株はセクターごとに強弱が分かれ、指数全体の方向性は限定的だった。金利と商品市況は材料待ちのムードが続き、"
    "決定的な手掛かりは乏しかった。海外勢のフローは時間帯によってばらつきがあり、イベント前の見送り姿勢が意識された。"
)
para1_input = st.text_area("段落①（市況サマリー）", value=default_para1, height=160, key="p1")

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
default_para2_seed = _default_para2_for_safe(pair_name)
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

# ===== ①にポイント一言を自動挿入（重複防止）→ for_build を作る =====
para1_build = _clean_text_jp_safe(para1)
if points_items and not _mentions_points(para1_build, points_items):
    hint = "本日は" + "と".join(points_items) + "が控えており、短期の振れに留意したい。"
    para1_build = re.sub(r"本日は[^。]*?留意したい。", "", str(para1_build)).strip()
    if para1_build and not para1_build.endswith("。"):
        para1_build += " "
    para1_build += hint
para1_build = _clean_text_jp_safe(para1_build)

# ===== ②は“最終に近い形”へ軽くパディング → for_build を作る =====
para2_build = _clean_text_jp_safe(para2)
para2_build = _pad_para2_base(para2_build, P2_MIN)  # ここでは結びの固定はしない（Step6が担当）

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

# 任意：接続診断（本文には出ません）—TEのキー確認だけ（レガシー互換）
if show_debug:
    import os as _os  # 念のためローカル import
    with st.expander("接続診断（本文には出ません）", expanded=False):
        prov = (CFG.get("providers", {}) or {}).get("tradingeconomics", {}) or {}
        _name = prov.get("key_env", "TE_API_KEY")
        try:
            _has = bool(st.secrets.get(_name))  # secrets優先
        except Exception:
            _has = False
        if not _has:
            _has = bool(_os.environ.get(_name))
        st.json({"TE_API_KEYが読めたか": _has, "endpoint": prov.get("endpoint")})

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

pts = [x for x in (st.session_state.get("points_tags_v2") or []) if x][:2]

# para1_for_build の安全な初期化
p1 = (para1_for_build if "para1_for_build" in locals()
      else (para1 if "para1" in locals() else ""))

def _already_mentions(body: str, items: list[str]) -> bool:
    if not body or not items:
        return True
    b = re.sub(r"\s+", "", body)
    for it in items:
        key = (it.split("に", 1)[-1] or "")  # "8:50に日・GDP..." → "日・GDP..."
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


## =========================
# ====== ステップ6：プレビュー（公開用体裁 + 自動チェック + 保存） ======
## =========================
from datetime import datetime
import json, re, unicodedata

st.markdown("### ステップ6：プレビュー（公開用体裁 + 自動チェック）")

# ---- 再読込コントロール（キーはユニークに）----
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

# =========================
# ここから “参照の一本化” と プレビュー組み立て
# =========================

# ③は唯一のソース（セッションの1本）だけ参照する
cal_line = str(st.session_state.get("calendar_line", "") or "").strip()

# 本文①/②は “for_build” があればそれを優先（自動挿入した一文を反映）
p1 = para1_for_build if "para1_for_build" in locals() else (para1 if "para1" in locals() else "")
p2 = para2_for_build if "para2_for_build" in locals() else (para2 if "para2" in locals() else "")

# タイトルの最終確定（UI > default > 自動生成）
ttl_display = (str(globals().get("title", "")).strip()
               or str(globals().get("default_title", "")).strip())
if not ttl_display:
    base = str(globals().get("pair", "") or "ポンド円")
    tail = (st.session_state.get("title_tail")
            if "st" in globals() and hasattr(st, "session_state") else None) or "注視か"
    ttl_display = f"{base}の方向感に{tail}"
ttl_display = _clean_text_jp(ttl_display) if "_clean_text_jp" in globals() else ttl_display

# ---- 本日のポイント（Step5選択を2件まで）----
points = list(st.session_state.get("points_tags_v2", []) or [])[:2]
point1 = points[0] if len(points) > 0 else ""
point2 = points[1] if len(points) > 1 else ""

# ---- ①が『本日のポイント』に軽く触れていなければ自動で一言だけ挿入 ----
def _mentions_points(s: str, items: list[str]) -> bool:
    if not s or not items: return True
    body = str(s).replace(" ", "")
    for it in items:
        key = (it.split("に", 1)[-1] or "").replace(" ", "")
        if key and key in body: return True
    return False

def _strip_prefix_and_time(s: str) -> str:
    t = unicodedata.normalize("NFKC", str(s or ""))
    return re.sub(r'^\s*([0-9]{1,2}:[0-9]{2})に(?:米|日|英|欧|豪|NZ|中国|南ア)・\s*', r'\1に', t)

_pts_short = [_strip_prefix_and_time(x) for x in points if x]
if _pts_short and not _mentions_points(p1, points):
    # 既存の似た文を除去してから1行だけ入れる
    p1 = re.sub(r"本日は[^。]*?留意したい。", "", str(p1)).strip()
    hint = ("本日は" + "と".join(_pts_short) + "が控えており、短期の振れに留意したい。")
    if p1 and not p1.endswith("。"): p1 += " "
    p1 += hint

# ---- ② 最低文字数ガード＋安全な結び ----
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
    if base and not base.endswith("。"): base += " "
    base += addon
    return base

# クリーニング
para1_clean = _clean_text_jp(str(p1).strip()) if "_clean_text_jp" in globals() else str(p1).strip()
para2_clean = _clean_text_jp(str(p2).strip()) if "_clean_text_jp" in globals() else str(p2).strip()

# ②：LLM補筆は任意（OFF既定）→その後パディング
use_llm_refine = bool(globals().get("use_llm_refine", False))
def _try_llm_refine_para2(text: str, target_min: int = 180) -> str:
    try:
        if "llm_complete" in globals():
            prompt = (
                "次の日本語テキストを、断定を避けるトーンを維持しつつ、"
                f"{target_min}〜220字程度に自然に補筆してください。"
                "ボリンジャーバンドやSMA/EMAといった語彙は崩さず、"
                "売買助言はしないこと。末尾は句点「。」で終えること。\n\n"
                f"【テキスト】\n{text}"
            )
            out = llm_complete(prompt)
            if isinstance(out, str) and len(out.strip()) >= target_min:
                return out.strip()
    except Exception:
        pass
    return text

para2_final = para2_clean
if use_llm_refine and len(para2_final) < 180:
    para2_final = _try_llm_refine_para2(para2_final, 180)
para2_final = pad_para2(para2_final, 180)
if not _ends_with_closer(para2_final):
    if not para2_final.endswith("。"): para2_final += " "
    para2_final += "方向感を見極めたい。"

# ---- タイトル回収（非LLMで堅く合成。LLM要約があれば前置き）----
def _guess_pair_from_title(ttl: str) -> str:
    m = re.search(r"^(.+?)の方向感", ttl or "")
    return m.group(1) if m else ""

def _infer_bias(para2: str) -> str:
    s = para2 or ""
    if any(k in s for k in ["上昇トレンド","上昇バイアス","上向き"]): return "上昇バイアス"
    if any(k in s for k in ["下落トレンド","下落圧力","下向き","反落"]): return "下落圧力"
    if any(k in s for k in ["もみ合い","方向感は限定","見極めたい"]): return "方向感の見極め"
    return "方向感の見極め"

def _points_label_join(opts: list[str]) -> str:
    labs = []
    for x in opts[:2]:
        labs.append((x.split("に", 1)[1] if "に" in x else x))
    return "と".join([z for z in labs if z])

def _synth_closer_nonllm(title: str, pair: str, bias_phrase: str,
                         cal_line: str, pts_join: str) -> str:
    parts = []
    if cal_line: parts.append(f"本日の指標は、{cal_line}。")
    parts.append(f"特に{pts_join}の通過後は短期の振れに留意しつつ、" if pts_join
                 else "イベント通過後の振れに留意しつつ、")
    parts.append(f"{pair}の{bias_phrase}を確認したい。" if pair
                 else f"相場の{bias_phrase}を確認したい。")
    parts.append(title if title.endswith("。") else title + "。")
    return "".join(parts)

def _try_llm_skim_summary(p1: str, p2: str, cal: str) -> str:
    return ""  # スキム要約は当面オフ（デバッグ混入防止）
    """
    1文要約（任意）。llm_complete が使える時だけ要約を取りに行く。
    使えない時は必ず空文字を返す（プロンプトが本文に混入しないようにする）。
    """
    try:
        llm = globals().get("llm_complete", None)
        if callable(llm):
            prompt = (
                "次の①②③を踏まえて、日本語で60〜120字の要約を1文だけ返してください。"
                "断定は避け、売買助言は含めないこと。\n"
                f"① {p1}\n② {p2}\n③ {cal}"
            )
            out = llm(prompt)
            s = (out or "").strip()
            # 念のためのガード：プロンプトの断片が返ってきたら破棄
            bad_markers = ("以下の段落", "短く1文で", "[①]", "[②]", "[③]")
            if not s or any(k in s for k in bad_markers):
                return ""
            # 1行に整形して長すぎる場合は切り詰め
            return s.replace("\n", " ")[:120]
    except Exception:
        pass
    return ""


pair_hint = _guess_pair_from_title(ttl_display)
bias      = _infer_bias(para2_final)
pts_join  = _points_label_join(points)
skim      = _try_llm_skim_summary(para1_clean, para2_final, cal_line)
if any(k in skim for k in ("以下の段落", "短く1文で", "[①]", "[②]", "[③]")):
    skim = ""


core = _synth_closer_nonllm(ttl_display, pair_hint, bias, cal_line, pts_join)
title_recall = core
title_recall = title_recall.strip()

# ---- レポート本文の組み立て（render_report があれば使う・無くても動く）----
report_final = ""
try:
    try:
        report_final = render_report(
            title=ttl_display,
            point1=point1, point2=point2,
            para1=para1_clean, para2=para2_final,
            calendar_line=cal_line,     # ← ③はセッションの唯一ソース
            title_recall=title_recall,  # ← 最後は必ずこの総括で回収
        )
    except TypeError:
        report_final = render_report(
            title=ttl_display,
            point1=point1, point2=point2,
            para1=para1_clean, para2=para2_final,
            cal_line=cal_line,          # ← 旧引数名に対応
            title_recall=title_recall,
        )
except Exception as e:
    st.warning(f"render_report 未定義またはエラーのため簡易構成で出力します: {e}")
    lines = []
    if ttl_display: lines += [f"タイトル：{ttl_display}", ""]
    if points:      lines += ["本日のポイント", *points, ""]
    if para1_clean: lines += [para1_clean, ""]
    if para2_final: lines += [para2_final, ""]
    # ③は title_recall に包含されるため、ここでは別途 cal_line を足さない
    lines += [title_recall]
    report_final = "\n".join(lines)

# ---- 締めの一貫化（古い締めが付いていたら差し替えて必ず title_recall に）----
try:
    _text = (report_final or "").rstrip()
    _text = re.sub(
        r"(本日の指標は、.*?。)(?:.*?の方向感に(?:注視|警戒|静観|要注意)か。)?\s*$",
        "",
        _text,
        flags=re.S,
    )

    # ★追加：ここで末尾の改行・空白を一度すべて落とす（残りカスの改行を消す）
    _text = _text.rstrip()

    # ★変更：常に「\n\n」= 空行1つだけ入れてタイトル回収を付与
    if title_recall not in _text:
        _text += "\n\n" + title_recall

    report_final = _text.strip()
except Exception:
    pass


# ---- この時点の本文を “唯一ソース” としてセッションに固定 ----
st.session_state["report_final_main"] = str(report_final or "").strip()

# ---- 体裁チェック（任意：最低限）----
viol = []
try:
    g = CFG.get("text_guards", {}) or {}
except Exception:
    g = {}
if len(para1_clean.replace("\n", "")) < g.get("p1_min_chars", 220): viol.append("段落①が規定文字数に未達")
if len(para2_final.replace("\n", "")) < g.get("p2_min_chars", 180): viol.append("段落②が規定文字数に未達")
bad_words = ["買い", "売り", "ロング", "ショート", "損切り", "推奨", "おすすめ", "必勝"]
if any(w in (para1_clean + para2_final) for w in bad_words): viol.append("売買助言に該当し得る語が含まれる")

# ---- プレビュー（唯一ソースから）----
text_to_show = str(st.session_state.get("report_final_main", "") or "")

# メモ: text_area は改行/空行を絶対に保持する。一方 disabled=True だと灰色になるので、
# enabled のまま「編集しても即元に戻す」＝実質ReadOnlyにする。

# 1) 現在のプレビュー本文を「正」としてセッションに保存
if "preview_report_base" not in st.session_state:
    st.session_state["preview_report_base"] = text_to_show

# 2) 上流で本文が更新されたときは base と表示用キーを同期
if st.session_state["preview_report_base"] != text_to_show:
    st.session_state["preview_report_base"] = text_to_show
    st.session_state["preview_report_main"] = text_to_show  # 初期表示を差し替え

# 3) 白背景のまま表示（編集できる見た目だが、下のガードですぐ元に戻る）
st.text_area(
    "プレビュー",
    value=st.session_state.get("preview_report_base", ""),
    height=420,
    key="preview_report_main",
    help="コピー可。編集は保存されません（自動で元に戻ります）。",
)

# 4) もしユーザーが編集しても、即「正」に巻き戻す（＝実質 ReadOnly）
if st.session_state.get("preview_report_main", "") != st.session_state.get("preview_report_base", ""):
    st.session_state["preview_report_main"] = st.session_state["preview_report_base"]
    try:
        st.rerun()
    except Exception:
        st.experimental_rerun()

# ルール要約の先頭だけ確認（長いとUIが崩れるので400字まで）
try:
    digest = (RULES_DIGEST or "").strip() if "RULES_DIGEST" in globals() else ""
    if digest:
        st.caption("現在のルール要約（先頭400字）")
        st.code(digest[:400])
except Exception:
    pass

# 体裁OK/NG表示（この下は元のままでOK）


if viol:
    st.error("体裁チェック NG：" + " / ".join(viol))
else:
    st.success("体裁チェック OK（見出し直後の空行なし／段落③は1行／最後はタイトル回収）。")

# === 保存・ダウンロード（“プレビューと同一本文”を唯一ソースに統一） ===
out_dir = Path("./out"); out_dir.mkdir(parents=True, exist_ok=True)
fname = f"m{datetime.now():%Y%m%d}.txt"
save_path = out_dir / fname
try:
    save_path.write_text(text_to_show, encoding="utf-8")
    st.success(f"保存しました：{fname}")
    st.download_button(
        "このプレビューをダウンロード",
        data=text_to_show.encode("utf-8"),
        file_name=fname,
        mime="text/plain",
        key="dl_report_unified",
    )
except Exception as e:
    st.warning(f"保存時のエラー：{e}")


# ===== 監査ログ（再現性のため） =====
from datetime import datetime
import json, re

try:
    # プレビュー本文（唯一ソース）
    report_text = str(st.session_state.get("report_final_main", "")).strip()

    # タイトル推定（フォールバック付き）
    title_guess = ""
    if report_text:
        first_line = report_text.splitlines()[0]
        m = re.search(r"^タイトル：(.+)$", first_line)
        if m:
            title_guess = m.group(1).strip()
    if not title_guess:
        title_guess = str(
            globals().get("ttl_display", "")
            or globals().get("title", "")
            or globals().get("default_title", "")
        ).strip()

    # 監査項目
    points_log = list(st.session_state.get("points_tags_v2", []) or [])[:2]
    cal_line   = str(st.session_state.get("calendar_line", "") or "").strip()
    checks     = st.session_state.get("checks_failed", [])  # あれば採用、無ければ空のまま

    live_diag = globals().get("live_diag", {})
    te_diag   = globals().get("te_diag", {})
    if not isinstance(live_diag, dict): live_diag = {}
    if not isinstance(te_diag, dict):   te_diag   = {}

    log = {
        "ts": datetime.now().isoformat(),
        "pair": str(globals().get("pair", "")),
        "title": title_guess,
        "points": points_log,
        "calendar_line": cal_line,
        "preview_len": len(report_text),
        "checks_failed": checks,
        "live_diag": live_diag,
        "te_diag": te_diag,
    }

    # 保存
    log_dir = Path("outlog"); log_dir.mkdir(parents=True, exist_ok=True)
    log_name = f"log_{datetime.now():%Y%m%d_%H%M%S}.json"
    (log_dir / log_name).write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
except Exception as e:
    st.warning(f"監査ログの保存に失敗しました: {e}")

# ===== （任意）LLMで最終本文を再組版：既定オフ・手動実行のみ =====
import os, json, re
from datetime import datetime

def _get_secret_any(name: str) -> str | None:
    # secrets.toml > env の順で取得
    try:
        v = st.secrets.get(name)
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get(name)

# プレビュー本文（唯一ソース）
preview_text = str(st.session_state.get("report_final_main", "")).strip()

with st.expander("LLMで最終本文を再組版（任意／実験的）", expanded=False):
    st.caption("※ クリック時のみ実行。プレビューと保存内容の唯一ソース（report_final_main）を上書きします。")

    # OpenAI 設定（config が無くても env/secrets から拾えるようフォールバック）
    prov_openai = ((CFG.get("providers") or {}).get("openai") or {}) if "CFG" in globals() else {}
    OPENAI_KEY   = _get_secret_any(prov_openai.get("key_env", "OPENAI_API_KEY"))
    OPENAI_MODEL = prov_openai.get("model", "gpt-4o-mini")
    OPENAI_TIMEOUT = int(prov_openai.get("timeout_sec", 30))

    # 入力素材は “唯一ソース/直前の確定値” を使う（再計算しない）
    ttl = str(globals().get("ttl_display", "") or "").strip()
    if not ttl:
        # プレビュー本文の先頭から推定（フォールバック）
        if preview_text.startswith("タイトル："):
            first = preview_text.splitlines()[0].replace("タイトル：", "", 1).strip()
            ttl = first

    p1_src = str(globals().get("p1", "") or "").strip()
    p2_src = str(globals().get("p2", "") or "").strip()
    cal_line = str(st.session_state.get("calendar_line", "") or "").strip()
    points_v = list(st.session_state.get("points_tags_v2", []) or [])[:2]

    # 実行ボタン
    run_llm = st.button("LLMで再組版を実行", key="btn_llm_compose_manual")
    if run_llm:
        if not OPENAI_KEY:
            st.warning("OpenAI API キーが見つかりません（secrets/env）。再組版はスキップしました。")
        else:
            # 要求仕様を JSON で返してもらう
            SYSTEM_MSG = (
                "あなたは金融パブリッシャー向けの編集者です。日本語で、煽らず断定しない筆致で、"
                "以下の制約を必ず守って相場レポートを整形してください。"
                "・禁止：売買助言（買い/売り/ロング/ショート等の推奨）\n"
                "・段落構成：①市況サマリー（>=220字想定）②テクニカル（>=180字想定）③本日の指標（1行）＋タイトル回収（句点で終える）\n"
                "・語彙例：SMA/EMA、ボリンジャーバンド、±2σ/±3σ、200SMA/EMA\n"
                "・②の最後は『行方を注視したい。／値動きには警戒したい。／当面は静観としたい。／一段の変動に要注意としたい。／方向感を見極めたい。』のいずれかで締める"
            )
            payload = {
                "title_suggestion": ttl,
                "points": points_v,
                "calendar_line": cal_line,
                "para1_draft": p1_src,
                "para2_draft": p2_src,
                "require_json": True
            }

            def _compose_with_openai(p):
                import requests
                url = "https://api.openai.com/v1/chat/completions"
                headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
                body = {
                    "model": OPENAI_MODEL,
                    "temperature": 0.3,
                    "messages": [
                        {"role": "system", "content": SYSTEM_MSG},
                        {"role": "user", "content": "次の入力をJSONで整形して。必ずUTF-8日本語。keys: title, para1, para2, para3。\n" + json.dumps(p, ensure_ascii=False)}
                    ],
                    "response_format": {"type": "json_object"}
                }
                r = requests.post(url, headers=headers, json=body, timeout=OPENAI_TIMEOUT)
                r.raise_for_status()
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                obj = json.loads(content)
                for k in ["title", "para1", "para2", "para3"]:
                    if k not in obj or not str(obj[k]).strip():
                        raise ValueError(f"LLM出力に {k} が欠落")
                return obj, data.get("usage", {})

            try:
                obj, usage = _compose_with_openai(payload)
                new_title = str(obj["title"]).strip()
                new_p1    = str(obj["para1"]).strip()
                new_p2    = str(obj["para2"]).strip()
                new_p3    = str(obj["para3"]).strip()

                # 本文は “唯一ソース” 形式に合成し直す
                blocks = []
                blocks += [f"タイトル：{new_title}", ""]
                if points_v:
                    blocks += ["本日のポイント", *points_v, ""]
                blocks += [new_p1, "", new_p2, "", new_p3]
                new_text = "\n".join(blocks).strip()

                # 唯一ソースを上書き → 以降のプレビュー/保存はすべてこれを参照
                st.session_state["report_final_main"] = new_text
                st.session_state["compose_diag"] = {"provider": "openai", "model": OPENAI_MODEL, "usage": usage}
                st.success("LLMで再組版を実行し、プレビュー本文（唯一ソース）を更新しました。")

            except Exception as e:
                st.warning(f"LLM再組版に失敗：{e}")

# =========================
# 以降：体裁チェック / プレビュー表示 / 保存・ダウンロード / 履歴（唯一ソースを使用）
# =========================
from datetime import datetime
import json

# --- 唯一ソース（プレビュー本文） ---
report_text = str(st.session_state.get("report_final_main", "")).strip()

# compose_diag 既定（LLM再組版を実行していない場合でも安全）
compose_diag = st.session_state.get("compose_diag", {"provider": "none"})

# 最低文字数（未定義でも動くようフォールバック）
try:
    P1_MIN
except NameError:
    P1_MIN = 220
try:
    P2_MIN
except NameError:
    P2_MIN = 180

# ①②の実体（未定義でも落ちない多段フォールバック＋②は最小文字数を保証）

# 最低文字数（CFGが無ければ既定）
try:
    _tg = (CFG.get("text_guards") or {})
    P1_MIN = int(_tg.get("p1_min_chars", 220))
    P2_MIN = int(_tg.get("p2_min_chars", 180))
except Exception:
    P1_MIN, P2_MIN = 220, 180

# パディング関数（既存があればそれを使う）
if "pad_para2" in globals():
    _pad_func = pad_para2
else:
    def _pad_func(s: str, min_chars: int = 180) -> str:
        base = str(s or "").strip()
        if len(base.replace("\n", "")) >= min_chars:
            return base
        addon = "短期は過度に方向感を決めつけず、節目やボリンジャーバンド周辺の反応を確かめつつ、値動きには警戒したい。"
        if base and not base.endswith("。"):
            base += " "
        return (base + addon).strip()

# ①は for_build > clean > 生 の順で取得
p1_src = (
    globals().get("para1_for_build")
    or globals().get("para1_clean")
    or globals().get("para1")
    or globals().get("p1")
    or ""
)
p1_src = str(p1_src)

# ②は final > for_build > clean > 生 の順で取得し、必ず最小文字数までパディング
p2_src = (
    globals().get("para2_final")
    or globals().get("para2_for_build")
    or globals().get("para2_clean")
    or globals().get("para2")
    or globals().get("p2")
    or ""
)
p2_src = _pad_func(str(p2_src), min_chars=P2_MIN)

def _validate_char_min(p1s: str, p2s: str, p1min: int, p2min: int) -> list[str]:
    errs = []
    if len((p1s or "").replace("\n","")) < p1min:
        errs.append(f"段落①が規定文字数未満（現在{len(p1s)}字 / 最低{p1min}字）")
    if len((p2s or "").replace("\n","")) < p2min:
        errs.append(f"段落②が規定文字数未満（現在{len(p2s)}字 / 最低{p2min}字）")
    return errs

# ---- 体裁チェック ----


errors = []

# 既存のレイアウト検証器があればまず使う（無ければ無視）
try:
    errs = validate_layout(report_text)
    if errs:
        errors.extend(errs)
except Exception:
    pass

# 文字数ガード（①②）
try:
    errs = _validate_char_min(p1_src, p2_src, P1_MIN, P2_MIN)
    if errs:
        errors.extend(errs)
except Exception:
    pass

# 禁止語チェック（本文全体に対して）
forbidden = [" 推奨", "おすすめ", "必勝", "利益確定を", "エントリー推奨"]
if any(w in report_text for w in forbidden):
    errors.append("売買助言と見なされる文言が含まれています（語彙を緩めてください）")

# ---- 診断の表示（LLM使用状況など）----
with st.expander("組版診断（本文には出ません）", expanded=False):
    st.write(compose_diag)

# ---- プレビューのソースをそのまま表示 ----
st.code(report_text, language="markdown")

# ---- チェック結果 ----
if errors:
    st.error("体裁チェック NG：\n- " + "\n- ".join(errors))
    can_export = False
else:
    st.success("体裁チェック OK（見出し直後の空行なし／段落③は1行／最後はタイトル回収）。")
    can_export = True

# ---- 保存・ダウンロード（唯一ソースを使用）----
# 目的：公開用テキストを保存（命名規則 mYYYYMMDD.txt を推奨）。保存の成否を画面に明示し、再出力の再現性を確保
file_name_default = f"m{datetime.now():%Y%m%d}.txt"
file_name = st.text_input("保存ファイル名（例：m20250812.txt）", value=file_name_default, key="fname_llm_v1")

st.download_button(
    "この内容をダウンロード（.txt）",
    data=report_text,
    file_name=file_name,
    mime="text/plain",
    disabled=not can_export,
    key="dl_btn_llm_v1",
)

# ---- 任意：プロジェクト内保存＋履歴追記（安全版・全置換OK）----
if st.checkbox("プロジェクト内 data/out に保存して履歴へ記録", value=False, key="save_and_log_llm_v1", disabled=not can_export):
    try:
        # 1) 本文を data/out/ に保存（UTF-8 BOMでメモ帳互換）
        outdir = Path("data") / "out"
        outdir.mkdir(parents=True, exist_ok=True)
        outfile = outdir / file_name
        outfile.write_text(report_text, encoding="utf-8-sig")
        st.success(f"保存しました：{outfile}")

        # 2) 履歴メタを安全に収集（未定義や型ずれに強く）
        pair_safe = str(globals().get("pair") or "")
        ttl_log = str((globals().get("ttl_display") or globals().get("title") or "")).strip()

        # ポイント：必ず文字列化→空要素除去→先頭2件
        _raw_pts = st.session_state.get("points_tags_v2", []) or []
        points_v = [str(x).strip() for x in _raw_pts if str(x).strip()][:2]

        # 本日の指標：区切りを「、」に正規化→空要素除去
        cal_line = str(st.session_state.get("calendar_line", "") or "")
        cal_events = [s.strip() for s in cal_line.replace(",", "、").split("、") if s.strip()]

        # compose_diag（LLMの実行ログ）が無い環境でも安全
        _cd = globals().get("compose_diag", {}) or {}
        if isinstance(_cd, dict):
            provider = _cd.get("provider")
            llm_model = _cd.get("model")
        else:
            provider = None
            llm_model = None
        llm_used = (str(provider).lower() == "openai")

        # NFP 次回日付は存在時のみ記録
        nfp_obj = globals().get("_nfp")
        nfp_next = nfp_obj.strftime("%Y-%m-%d") if getattr(nfp_obj, "strftime", None) else None

        # ソースCSVのパス（あれば）
        src_csv = globals().get("use_csv_path")
        src_csv = str(src_csv) if src_csv else None

        # 3) JSONL へ1行追記（UTF-8 / 日本語可）
        hist_path = Path("data") / "history.jsonl"
        meta = {
            "id": str(uuid4()),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pair": pair_safe,
            "title": ttl_log,
            "points": points_v,
            "calendar_events": cal_events,
            "nfp_next": nfp_next,
            "source_csv": src_csv,
            "file_name": file_name,
            "llm_used": llm_used,
            "llm_model": llm_model,
        }
        with hist_path.open("a", encoding="utf-8") as hf:
            hf.write(json.dumps(meta, ensure_ascii=False) + "\n")
        st.caption(f"履歴に追記しました：{hist_path}")

    except Exception as e:
        st.error(f"保存/履歴の処理でエラー: {e}")







