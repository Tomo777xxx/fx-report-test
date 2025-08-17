import os, base64, yaml
import streamlit as st

# 設定読み込み
with open("config.yaml", "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

st.set_page_config(page_title=CFG["app"]["title"], layout="centered")
st.title(CFG["app"]["title"])

st.markdown("### ステップ1：参照PDFの確認")
missing = []
for i, p in enumerate(CFG.get("pdf_paths", []), start=1):
    exists = os.path.exists(p)
    st.write(f"{i}. {p}  →  {'✅ 見つかりました' if exists else '❌ 見つかりません'}")
    if exists:
        with open(p, "rb") as f:
            data = f.read()
        st.download_button(
            f"PDFをダウンロード {i}",
            data=data,
            file_name=os.path.basename(p),
            mime="application/pdf"
        )
    else:
        missing.append(p)

if missing:
    st.warning("上の ❌ のPDFが見つかりません。パスを config.yaml で修正して保存し、画面左上の『再実行』を押してください。")

st.markdown("---")
st.markdown("### ステップ2：モックのレポート出力（体裁雛形）")

# ---- ここは「とりあえず動く」固定文。次の段階で実データを差し込みます ----
title = "200円手前で上値が重いポンド円の下値余地に警戒か"

point1 = "米・MBA住宅ローン申請指数"
point2 = "欧・消費者信頼感指数"

para1 = (
    "昨日は、米国市場で主要株価指数3銘柄のうち2銘柄が上昇となり、株高・金利安・原油横ばいの相場展開となった。"
    "原油WTIは65.5ドル付近にて停滞。一方の天然ガスは、前日から0.78%下落。3.29ドル台まで落ち込んだ。"
    "主要貴金属5銘柄はプラチナ以外が上昇となり、唯一下落したプラチナは、前日から1.01%低下。1,482ドル台で推移した。"
)

para2 = (
    "為替市場は、ポンドが米ドルに次いで弱含んだ。ポンド円は199.280から197.439まで下落。"
    "時間足ボリンジャーバンド+3σから-3σへと移行した。4時間足では20MAに上値をレジストされる形で下落しており、"
    "日足ではサポートされている20MAを一時的に割り込んでいる。本日の欧州時間にてポンドの軟化が継続するのか、強弱性に注意したい。"
)

calendar_items = [
    "9:30に豪・Westpac先行指数", "10:30に日・内田日銀副総裁の発言", "15:00に日・工作機械受注",
    "17:00に南ア・消費者物価指数", "20:00に米・MBA住宅ローン申請指数", "23:00に欧・消費者信頼感指数",
    "23:00に米・中古住宅販売件数", "23:30に米・週間原油在庫", "26:00に米・20年債入札"
]
calendar_line = "、".join(calendar_items)

# タイトル回収（言い切り）
title_recall = "200円手前で上値が重いポンド円の下値余地に注目したい。"

report = (
f"タイトル：{title}\n\n"
"本日のポイント\n"
f"{point1}\n"
f"{point2}\n\n"
f"{para1}\n\n"
f"{para2}\n\n"
f"本日の指標は、{calendar_line}が発表予定となっている。{title_recall}"
)

st.code(report, language="markdown")
st.success("体裁の雛形で出力できました。次のステップで『実データ連携』に進みます。")
