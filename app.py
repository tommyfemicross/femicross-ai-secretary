import streamlit as st
import anthropic
import os
from datetime import datetime
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

# ── クライアント初期化 ─────────────────────────────────────
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
slack_client  = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))

SLACK_CHANNEL_PERSONAL = os.getenv("SLACK_CHANNEL_PERSONAL", "")
SLACK_CHANNEL_SHARED   = os.getenv("SLACK_CHANNEL_SHARED", "")


# ── ベースシステムプロンプト ───────────────────────────────
BASE_MORNING = """あなたはFemiCrossのAIエージェント「相田ミク」です。
仕事はじめのセッションを担当しています。

【キャラクター】
名前: 相田ミク
口調: フレンドリーで励ましてくれる同僚のような存在。敬語ベース。日本語で話す。

【最初の挨拶（必須）】
セッション開始時は必ず「おはようございます。フェミクロスAIエージェントの相田ミクです。」から始めること。

【役割】
ユーザーが今日取り組むタスクを会話で整理するお手伝いをする。

【進め方】
1. 挨拶の後「今日は何をしますか？」と聞く
2. 話してくれた内容を深掘りする
   - 「それはいつまでに必要ですか？」
   - 「誰かに確認が必要ですか？」
   - 「優先度はどのくらいですか？」
3. タスクが出そろったら一覧にまとめて確認を取る
4. 確認がとれたら下記フォーマットでタスクリストを出力する

【タスクリスト出力フォーマット】
---TASK_LIST---
【本日のタスク - MM/DD】
・タスク名（期限: XX、優先度: 高/中/低）
・タスク名
---END_TASK_LIST---"""

BASE_EVENING = """あなたはFemiCrossのAIエージェント「相田ミク」です。
仕事おわりのセッションを担当しています。

【キャラクター】
名前: 相田ミク
口調: ねぎらいの気持ちを込めた同僚のような存在。敬語ベース。日本語で話す。

【最初の挨拶（必須）】
セッション開始時は必ず「お疲れさまです。フェミクロスAIエージェントの相田ミクです。」から始めること。

【役割】
今日の振り返りと、明日の準備を会話でサポートする。

【進め方】
1. 挨拶の後「今日はどうでしたか？」と聞いて話を引き出す
2. 完了したタスクを称える
3. 未完了のタスクは「これはどうしますか？」と確認
   選択肢: 明日に持ち越す / 別の日に / キャンセル
4. 翌営業日のタスクをまとめたら下記フォーマットで出力する

【タスクリスト出力フォーマット】
---TASK_LIST---
【本日の振り返り - MM/DD】
✅ 完了: タスク名
⏭ 持ち越し: タスク名
❌ キャンセル: タスク名

【次の営業日のタスク】
・タスク名
---END_TASK_LIST---"""


# ── ヘルパー関数 ──────────────────────────────────────────
def build_system_prompt(base: str, customizations: list) -> str:
    """ベースプロンプトにユーザーの対話カスタマイズを追記する"""
    if not customizations:
        return base
    lines = "\n".join(f"- {c}" for c in customizations)
    return base + f"\n\n【対話スタイルの調整（最優先で守ること）】\n{lines}"


def is_evening_time() -> bool:
    return datetime.now().hour >= 16


def chat_with_claude(messages: list, session_type: str, customizations: list) -> str:
    base   = BASE_MORNING if session_type == "morning" else BASE_EVENING
    system = build_system_prompt(base, customizations)
    resp   = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=system,
        messages=messages,
    )
    return resp.content[0].text


def extract_task_list(text: str):
    if "---TASK_LIST---" in text and "---END_TASK_LIST---" in text:
        s = text.index("---TASK_LIST---") + len("---TASK_LIST---")
        e = text.index("---END_TASK_LIST---")
        return text[s:e].strip()
    return None


def strip_task_markers(text: str) -> str:
    if "---TASK_LIST---" in text:
        return text[: text.index("---TASK_LIST---")].strip()
    return text


def send_to_slack(message: str, channels: list) -> bool:
    ok = True
    for ch in channels:
        if not ch:
            continue
        try:
            slack_client.chat_postMessage(channel=ch, text=message, mrkdwn=True)
        except SlackApiError as e:
            st.error(f"Slack送信エラー ({ch}): {e.response['error']}")
            ok = False
    return ok


def apply_customization(text: str):
    """対話修正を適用して、セッション中なら相田ミクに通知する"""
    text = text.strip()
    if not text:
        return
    if text not in st.session_state.customizations:
        st.session_state.customizations.append(text)
    # セッション中なら相田ミクに変更を伝える
    if st.session_state.session_started:
        notify = (
            f"【対話スタイル変更のお知らせ】"
            f"ユーザーから次のリクエストがありました:「{text}」"
            f"今後はこのスタイルで対応してください。"
            f"変更を受け付けたことを一言だけ伝えてください。"
        )
        st.session_state.messages.append({"role": "user", "content": notify})
        reply = chat_with_claude(
            st.session_state.messages,
            st.session_state.session_type,
            st.session_state.customizations,
        )
        st.session_state.messages.append({"role": "assistant", "content": reply})


# ── ページ設定 ────────────────────────────────────────────
st.set_page_config(page_title="FemiCross AI秘書", page_icon="🌸", layout="centered")
st.markdown("""
<style>
.title    { font-size:28px; font-weight:bold; text-align:center; color:#e75480; margin-bottom:2px; }
.subtitle { text-align:center; color:#aaa; font-size:13px; margin-bottom:16px; }
.badge    { text-align:center; padding:6px 14px; border-radius:20px; font-size:13px;
            margin-bottom:16px; display:inline-block; width:100%; }
.morning  { background:#fff3e0; color:#e65100; }
.evening  { background:#e8eaf6; color:#3949ab; }
.ctag     { background:#f0fdf4; border:1px solid #86efac; border-radius:8px;
            padding:5px 10px; font-size:12px; display:inline-block; margin:2px; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="title">🌸 FemiCross AI秘書</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">相田ミク</div>', unsafe_allow_html=True)

# ── セッション状態の初期化 ────────────────────────────────
for key, val in [
    ("session_started",   False),
    ("session_type",      None),
    ("messages",          []),
    ("task_list",         None),
    ("slack_sent",        False),
    ("customizations",    []),      # 対話スタイル調整リスト
    ("show_custom_panel", False),   # 対話修正パネル表示フラグ
]:
    if key not in st.session_state:
        st.session_state[key] = val


# ── 対話修正パネル（共通コンポーネント）──────────────────
def render_custom_panel():
    if not st.session_state.show_custom_panel:
        return

    with st.container(border=True):
        st.markdown("### 🛠 AIの対話スタイルを調整する")

        # 現在の設定を表示
        if st.session_state.customizations:
            st.markdown("**現在の設定:**")
            for i, c in enumerate(st.session_state.customizations):
                col_t, col_d = st.columns([6, 1])
                with col_t:
                    st.markdown(f'<span class="ctag">✅ {c}</span>', unsafe_allow_html=True)
                with col_d:
                    if st.button("✕", key=f"del_{i}", help="この設定を削除"):
                        st.session_state.customizations.pop(i)
                        st.rerun()
            st.markdown("")

        # よく使う例ボタン
        st.markdown("**よく使う調整例（クリックで即適用）:**")
        examples = [
            "質問は一度に1つだけにして",
            "もっと短く簡潔に返答して",
            "もっと詳しく深掘りして",
            "フランクな口調で話して",
            "励ましの言葉を多めにして",
            "箇条書きで整理して見せて",
        ]
        cols = st.columns(3)
        for i, ex in enumerate(examples):
            with cols[i % 3]:
                if st.button(ex, key=f"ex_{i}", use_container_width=True):
                    apply_customization(ex)
                    st.session_state.show_custom_panel = False
                    st.rerun()

        st.markdown("**または自由に入力:**")
        custom_input = st.text_area(
            "リクエストを入力",
            placeholder="例: もっと明るいトーンで話して、タスクを整理するときは優先度を必ず確認して...",
            height=80,
            label_visibility="collapsed",
            key="custom_input_field",
        )
        col_ok, col_close = st.columns([3, 1])
        with col_ok:
            if st.button("✅ この設定を適用する", type="primary", use_container_width=True):
                apply_customization(custom_input)
                st.session_state.show_custom_panel = False
                st.rerun()
        with col_close:
            if st.button("閉じる", use_container_width=True):
                st.session_state.show_custom_panel = False
                st.rerun()


# ═══════════════════════════════════════════════════════════
# ① スタート画面（セッション未開始）
# ═══════════════════════════════════════════════════════════
if not st.session_state.session_started:
    now = datetime.now()
    st.markdown(
        f"<p style='text-align:center;color:gray;font-size:13px;'>"
        f"{now.strftime('%Y年%m月%d日 %H:%M')}</p>",
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("🌅　仕事はじめ", use_container_width=True, type="primary"):
            st.session_state.update(
                session_started=True, session_type="morning",
                messages=[], task_list=None, slack_sent=False, show_custom_panel=False,
            )
            st.rerun()
    with col2:
        if st.button("🌙　仕事おわり", use_container_width=True):
            st.session_state.update(
                session_started=True, session_type="evening",
                messages=[], task_list=None, slack_sent=False, show_custom_panel=False,
            )
            st.rerun()
    with col3:
        badge = " ✅" if st.session_state.customizations else ""
        if st.button(f"🛠　対話修正{badge}", use_container_width=True):
            st.session_state.show_custom_panel = not st.session_state.show_custom_panel
            st.rerun()

    # 現在の対話設定サマリー
    if st.session_state.customizations:
        st.markdown("**現在の対話設定:**")
        tags = "".join(f'<span class="ctag">✅ {c}</span>' for c in st.session_state.customizations)
        st.markdown(tags, unsafe_allow_html=True)
        st.markdown("")

    render_custom_panel()

    if is_evening_time():
        st.info("💡 16時を過ぎています。「仕事おわり」セッションがおすすめです。")


# ═══════════════════════════════════════════════════════════
# ② チャット画面（セッション中）
# ═══════════════════════════════════════════════════════════
else:
    stype = st.session_state.session_type
    label = "🌅 仕事はじめセッション" if stype == "morning" else "🌙 仕事おわりセッション"
    cls   = "morning" if stype == "morning" else "evening"
    st.markdown(f'<div class="badge {cls}">{label}</div>', unsafe_allow_html=True)

    # セッション中の対話修正ボタン
    badge = " ✅" if st.session_state.customizations else ""
    if st.button(f"🛠　対話修正{badge}", use_container_width=False):
        st.session_state.show_custom_panel = not st.session_state.show_custom_panel
        st.rerun()
    render_custom_panel()

    # ── 最初のメッセージを自動生成 ──────────────────────
    if len(st.session_state.messages) == 0:
        with st.spinner("相田ミクが準備中..."):
            if stype == "morning":
                trigger = "セッションを開始してください。必ず「おはようございます。フェミクロスAIエージェントの相田ミクです。」という挨拶から始めてください。"
            else:
                trigger = "セッションを開始してください。必ず「お疲れさまです。フェミクロスAIエージェントの相田ミクです。」という挨拶から始めてください。"
            st.session_state.messages.append({"role": "user", "content": trigger})
            reply = chat_with_claude(st.session_state.messages, stype, st.session_state.customizations)
            st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

    # ── チャット履歴表示 ─────────────────────────────────
    for i, msg in enumerate(st.session_state.messages):
        if i == 0:
            continue
        # 内部の対話修正通知は非表示
        if msg["role"] == "user" and "【対話スタイル変更のお知らせ】" in msg["content"]:
            continue
        if msg["role"] == "assistant":
            with st.chat_message("assistant", avatar="🌸"):
                st.markdown(strip_task_markers(msg["content"]))
                tl = extract_task_list(msg["content"])
                if tl:
                    st.session_state.task_list = tl
                    st.success("📋 タスクリストが作成されました！")
                    st.code(tl, language="")
        else:
            with st.chat_message("user", avatar="👤"):
                st.markdown(msg["content"])

    # ── Slack送信パネル ──────────────────────────────────
    if st.session_state.task_list and not st.session_state.slack_sent:
        st.markdown("---")
        st.markdown("**📤 Slackに送りますか？**")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("個人チャンネルへ", type="primary", use_container_width=True):
                if send_to_slack(st.session_state.task_list, [SLACK_CHANNEL_PERSONAL]):
                    st.success("✅ 個人チャンネルに送信しました！")
                    st.session_state.slack_sent = True
        with c2:
            if st.button("チームにも共有", use_container_width=True):
                if send_to_slack(st.session_state.task_list,
                                 [SLACK_CHANNEL_PERSONAL, SLACK_CHANNEL_SHARED]):
                    st.success("✅ 個人＋チームに送信しました！")
                    st.session_state.slack_sent = True
        with c3:
            if st.button("送らない", use_container_width=True):
                st.session_state.slack_sent = True
                st.rerun()

    # ── チャット入力 ────────────────────────────────────
    if prompt := st.chat_input("メッセージを入力してください..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.spinner("考え中..."):
            reply = chat_with_claude(st.session_state.messages, stype, st.session_state.customizations)
        st.session_state.messages.append({"role": "assistant", "content": reply})
        tl = extract_task_list(reply)
        if tl:
            st.session_state.task_list  = tl
            st.session_state.slack_sent = False
        st.rerun()

    # ── セッション終了ボタン ────────────────────────────
    st.markdown("---")
    if st.button("🔚　セッションを終了する", use_container_width=True):
        st.session_state.update(
            session_started=False, session_type=None,
            messages=[], task_list=None, slack_sent=False, show_custom_panel=False,
        )
        st.rerun()
