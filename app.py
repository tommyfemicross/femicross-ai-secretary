import streamlit as st
import anthropic
import os
import json
import uuid
from datetime import datetime, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from pathlib import Path

# ── 音声入力ライブラリ（任意）────────────────────────────
try:
    from streamlit_js_eval import streamlit_js_eval
    VOICE_AVAILABLE = True
except ImportError:
    VOICE_AVAILABLE = False

load_dotenv()

# ── タイムゾーン（日本時間）──────────────────────────────
JST = ZoneInfo("Asia/Tokyo")

def now_jst() -> datetime:
    return datetime.now(JST)

def today_jst() -> str:
    return str(date.today())   # サーバーがどこにあっても now_jst() から導出
    # ※ Streamlit Cloud は UTC なので date.today() ではなく JST から取る
def today_str_jst() -> str:
    return now_jst().strftime("%Y-%m-%d")


# ── クライアント初期化 ─────────────────────────────────────
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
slack_client  = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))

SLACK_CHANNEL_PERSONAL = os.getenv("SLACK_CHANNEL_PERSONAL", "")
SLACK_CHANNEL_SHARED   = os.getenv("SLACK_CHANNEL_SHARED", "")

# 監視するSlackチャンネルIDのリスト（カンマ区切りで複数指定可）
SLACK_MONITOR_CHANNELS = [
    c.strip() for c in os.getenv("SLACK_CHANNELS_TO_MONITOR", "").split(",") if c.strip()
]

# ── ユーザー設定 ───────────────────────────────────────────
USERS = {
    "松坂智美": {
        "id": "matsuzaka",
        "slack_id": os.getenv("SLACK_ID_MATSUZAKA", ""),
    },
    "伊藤美樹": {
        "id": "ito",
        "slack_id": os.getenv("SLACK_ID_ITO", ""),
    },
    "高橋聖子": {
        "id": "takahashi",
        "slack_id": os.getenv("SLACK_ID_TAKAHASHI", ""),
    },
}


# ── データ永続化（ユーザーごと）──────────────────────────
def data_file(user_id: str) -> Path:
    return Path(f"data_{user_id}.json")

def load_user_data(user_id: str) -> dict:
    f = data_file(user_id)
    if f.exists():
        try:
            with open(f, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    return {"carry_over_tasks": [], "today_tasks": [], "last_updated": ""}

def save_user_data(user_id: str, data: dict):
    with open(data_file(user_id), "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


# ── Slack 未返信・未リアクションメッセージ取得 ─────────────
def get_unresponded_messages(slack_id: str) -> list:
    """ユーザーが返信もリアクションもしていないメッセージを返す"""
    if not slack_id or not SLACK_MONITOR_CHANNELS:
        return []
    results = []
    for ch in SLACK_MONITOR_CHANNELS:
        try:
            history = slack_client.conversations_history(channel=ch, limit=30)
            for msg in history.get("messages", []):
                # 自分の投稿・ボットはスキップ
                if msg.get("user") == slack_id:
                    continue
                if msg.get("bot_id"):
                    continue
                # リアクションチェック
                if any(slack_id in r.get("users", []) for r in msg.get("reactions", [])):
                    continue
                # スレッド返信チェック
                replied = False
                if msg.get("reply_count", 0) > 0:
                    try:
                        thread = slack_client.conversations_replies(
                            channel=ch, ts=msg["ts"], limit=20
                        )
                        replied = any(
                            r.get("user") == slack_id
                            for r in thread.get("messages", [])[1:]
                        )
                    except Exception:
                        pass
                if not replied:
                    ts = float(msg.get("ts", 0))
                    msg_time = datetime.fromtimestamp(ts, tz=JST).strftime("%m/%d %H:%M")
                    results.append({
                        "text": msg.get("text", "")[:80],
                        "time": msg_time,
                    })
        except SlackApiError:
            pass
    return results[:10]


# ── システムプロンプト生成 ─────────────────────────────────
def morning_system_prompt(
    user_name: str,
    carry_tasks: list,
    slack_unreplied: list,
    is_edit_mode: bool,
    existing_tasks: list,
) -> str:
    carry_section = ""
    if carry_tasks:
        lines = "\n".join(
            f"・{t['text']}" + (f"（期限: {t['deadline']}）" if t.get("deadline") else "")
            for t in carry_tasks
        )
        carry_section = f"\n\n【前回からの持ち越しタスク】\n{lines}\nこれらをまず提示してから、新しいタスクも追加で確認してください。"

    slack_section = ""
    if slack_unreplied:
        lines = "\n".join(f"・{m['text'][:60]}（{m['time']}）" for m in slack_unreplied[:4])
        slack_section = f"\n\n【未返信・未リアクションのSlackメッセージ】\n{lines}\nこれらへの対応を今日のタスクとして追加することを提案してください。"

    edit_section = ""
    if is_edit_mode and existing_tasks:
        ex_lines = "\n".join(
            f"・{t['text']}" + (f"（期限: {t['deadline']}）" if t.get("deadline") else "")
            for t in existing_tasks
        )
        edit_section = f"\n\n【本日登録済みのタスク（修正モード）】\n{ex_lines}\n追加・変更があるか{user_name}さんに確認してください。"

    return f"""あなたはFemiCrossのAIエージェント「相田ミク」です。
{user_name}さんの仕事はじめセッションを担当しています。

【キャラクター】
名前: 相田ミク
口調: フレンドリーで励ましてくれる同僚。敬語ベース。日本語で話す。

【最初の挨拶（必須・一字一句この通りに）】
「おはようございます。フェミクロスAIエージェントの相田ミクです。今日も一緒にがんばりましょう！今日のタスクと期日を教えてください。」

【ルール】
- タスクの深掘り質問は期限のみ（優先度は聞かない）
- 質問は各タスクに最大2つ、1つずつ行う
- 全タスクが揃ったら「これでよろしいでしょうか？」と確認してから出力する
{carry_section}{slack_section}{edit_section}

【タスクリスト出力フォーマット（確認が取れたら必ず出力）】
---TASK_LIST---
・タスク名｜期限: MM/DD
・タスク名｜期限: MM/DD
---END_TASK_LIST---"""


EVENING_SYSTEM_PROMPT = """あなたはFemiCrossのAIエージェント「相田ミク」です。
仕事おわりのセッションを担当しています。

【最初の挨拶（必須・一字一句この通りに）】
「お疲れさまです。フェミクロスAIエージェントの相田ミクです。今日も一日お疲れさまでした！」

挨拶の後「今日はどうでしたか？」と聞いて、ねぎらいながら振り返りをサポートしてください。
タスクのチェックはUI側で行います。AIは会話に集中してください。
日本語で。敬語ベース。フレンドリーに。"""


# ── Claude 呼び出し ────────────────────────────────────────
def chat_with_claude(messages: list, system: str) -> str:
    resp = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=system,
        messages=messages,
    )
    return resp.content[0].text


# ── タスクリスト処理 ───────────────────────────────────────
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

def parse_tasks(text: str) -> list:
    tasks = []
    for line in text.strip().split("\n"):
        line = line.strip().lstrip("・-• ").strip()
        if not line:
            continue
        parts = line.split("｜")
        task_text = parts[0].strip()
        deadline = ""
        for part in parts[1:]:
            p = part.strip()
            if "期限" in p:
                deadline = p.replace("期限:", "").replace("期限：", "").strip()
        if task_text:
            tasks.append({
                "id": str(uuid.uuid4()),
                "text": task_text,
                "deadline": deadline,
                "completed": False,
            })
    return tasks


# ── Slack 送信 ─────────────────────────────────────────────
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


# ── 音声入力（Chrome専用）────────────────────────────────
def render_voice_input() -> str | None:
    """🎤ボタンを表示し、音声認識結果をテキストで返す。Chrome以外はNoneを返す。"""
    if not VOICE_AVAILABLE:
        return None

    if st.button("🎤 音声入力", help="Chromeのみ対応", key=f"voice_btn_{st.session_state.get('voice_count', 0)}"):
        st.session_state.is_listening = True
        st.session_state.voice_count = st.session_state.get("voice_count", 0) + 1
        st.rerun()

    if st.session_state.get("is_listening"):
        st.session_state.is_listening = False
        with st.spinner("🎤 聞いています…（話してください）"):
            result = streamlit_js_eval(
                js_expressions="""
                (() => new Promise((resolve) => {
                    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
                    if (!SR) { resolve(null); return; }
                    const r = new SR();
                    r.lang = 'ja-JP';
                    r.interimResults = false;
                    r.onresult = (e) => resolve(e.results[0][0].transcript);
                    r.onerror  = ()  => resolve(null);
                    r.start();
                }))()
                """,
                want_output=True,
                key=f"voice_result_{st.session_state.get('voice_count', 0)}",
            )
        if result:
            st.success(f"🎤 認識結果: {result}")
            return result
    return None


# ══════════════════════════════════════════════════════════
# ページ設定
# ══════════════════════════════════════════════════════════
st.set_page_config(page_title="FemiCross AI秘書", page_icon="🌸", layout="centered")
st.markdown("""
<style>
.title     { font-size:26px; font-weight:bold; text-align:center; color:#e75480; margin-bottom:2px; }
.subtitle  { text-align:center; color:#aaa; font-size:13px; margin-bottom:12px; }
.badge     { text-align:center; padding:5px 14px; border-radius:20px; font-size:13px;
             margin-bottom:12px; display:inline-block; width:100%; }
.morning   { background:#fff3e0; color:#e65100; }
.evening   { background:#e8eaf6; color:#3949ab; }
.carry     { background:#fef9c3; border-left:3px solid #eab308;
             border-radius:6px; padding:6px 12px; margin:3px 0; font-size:13px; }
.slack-msg { background:#e8f4fd; border-left:3px solid #2196f3;
             border-radius:6px; padding:6px 12px; margin:3px 0; font-size:12px; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="title">🌸 FemiCross AI秘書</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">相田ミク</div>', unsafe_allow_html=True)

# ── セッション状態の初期化 ────────────────────────────────
_defaults = {
    "current_user":    None,
    "session_started": False,
    "session_type":    None,
    "messages":        [],
    "task_list_text":  None,
    "today_tasks":     [],
    "slack_sent":      False,
    "evening_tasks":   [],
    "is_edit_mode":    False,
    "slack_unreplied": [],
    "is_listening":    False,
    "voice_count":     0,
    "_morning_system": None,
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ══════════════════════════════════════════════════════════
# ① ユーザー選択画面
# ══════════════════════════════════════════════════════════
if st.session_state.current_user is None:
    st.markdown("### 👤 ユーザーを選択してください")
    cols = st.columns(3)
    for i, (name, info) in enumerate(USERS.items()):
        with cols[i]:
            if st.button(name, use_container_width=True, type="primary", key=f"user_{i}"):
                with st.spinner("Slackを確認中..."):
                    unreplied = get_unresponded_messages(info["slack_id"])
                st.session_state.current_user   = name
                st.session_state.slack_unreplied = unreplied
                st.rerun()
    st.stop()


# ══════════════════════════════════════════════════════════
# ② ホーム画面（セッション未開始）
# ══════════════════════════════════════════════════════════
if not st.session_state.session_started:
    user_name = st.session_state.current_user
    user_info = USERS[user_name]
    user_data = load_user_data(user_info["id"])
    now = now_jst()
    today = today_str_jst()

    st.markdown(
        f"<p style='text-align:center;color:gray;font-size:13px;'>"
        f"こんにちは、<b>{user_name}</b>さん　{now.strftime('%Y年%m月%d日 %H:%M')} (JST)</p>",
        unsafe_allow_html=True,
    )

    # Slack 未返信サマリー
    unreplied = st.session_state.slack_unreplied
    if unreplied:
        with st.expander(f"💬 未返信・未リアクションのSlackメッセージ（{len(unreplied)}件）", expanded=True):
            for msg in unreplied:
                st.markdown(
                    f'<div class="slack-msg">📩 {msg["time"]}　{msg["text"]}</div>',
                    unsafe_allow_html=True,
                )

    # 持ち越しタスク
    carry = user_data.get("carry_over_tasks", [])
    if carry:
        st.markdown(f"**⏭ 前回からの持ち越し（{len(carry)}件）:**")
        for t in carry:
            dl = f"　📅 {t['deadline']}" if t.get("deadline") else ""
            st.markdown(f'<div class="carry">・{t["text"]}{dl}</div>', unsafe_allow_html=True)
        st.markdown("")

    # ── ボタン（仕事はじめ / 仕事おわり）──────────────────
    is_same_day  = (user_data.get("last_updated") == today) and bool(user_data.get("today_tasks"))
    morn_label   = "🌅　今日のタスクを修正" if is_same_day else "🌅　仕事はじめ"

    col1, col2 = st.columns(2)
    with col1:
        if st.button(morn_label, use_container_width=True, type="primary"):
            st.session_state.update(
                session_started=True,
                session_type="morning",
                messages=[],
                task_list_text=None,
                slack_sent=False,
                is_edit_mode=is_same_day,
                today_tasks=user_data.get("today_tasks", []) if is_same_day else [],
                _morning_system=None,
            )
            st.rerun()
    with col2:
        if st.button("🌙　仕事おわり", use_container_width=True):
            st.session_state.update(
                session_started=True,
                session_type="evening",
                messages=[],
                task_list_text=None,
                slack_sent=False,
                evening_tasks=user_data.get("today_tasks", []),
            )
            st.rerun()

    if st.button("← ユーザーを変更", key="back_to_user"):
        for k in _defaults:
            st.session_state[k] = _defaults[k]
        st.rerun()

    if now.hour >= 16:
        st.info("💡 16時を過ぎています。「仕事おわり」セッションがおすすめです。")

    st.stop()


# ══════════════════════════════════════════════════════════
# ③ 仕事はじめセッション
# ══════════════════════════════════════════════════════════
if st.session_state.session_type == "morning":
    user_name = st.session_state.current_user
    user_info = USERS[user_name]
    user_data = load_user_data(user_info["id"])

    st.markdown('<div class="badge morning">🌅 仕事はじめセッション</div>', unsafe_allow_html=True)
    st.markdown(f"**{user_name}さん**")

    # 初回メッセージ生成
    if len(st.session_state.messages) == 0:
        carry = user_data.get("carry_over_tasks", [])
        system = morning_system_prompt(
            user_name,
            carry,
            st.session_state.slack_unreplied,
            st.session_state.is_edit_mode,
            st.session_state.today_tasks,
        )
        st.session_state._morning_system = system

        trigger = "セッションを開始してください。必ず指定の挨拶から始めてください。"
        if st.session_state.is_edit_mode and st.session_state.today_tasks:
            ex_lines = "\n".join(
                f"・{t['text']}" + (f"（期限: {t['deadline']}）" if t.get("deadline") else "")
                for t in st.session_state.today_tasks
            )
            trigger += f"\n\n今日はすでに以下のタスクが登録されています：\n{ex_lines}\n追加・修正があるか確認してください。"

        with st.spinner("相田ミクが準備中..."):
            st.session_state.messages.append({"role": "user", "content": trigger})
            reply = chat_with_claude(st.session_state.messages, system)
            st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

    system = st.session_state._morning_system or morning_system_prompt(
        user_name, [], [], False, []
    )

    # チャット履歴表示
    for i, msg in enumerate(st.session_state.messages):
        if i == 0:
            continue
        if msg["role"] == "assistant":
            with st.chat_message("assistant", avatar="🌸"):
                st.markdown(strip_task_markers(msg["content"]))
                tl = extract_task_list(msg["content"])
                if tl:
                    st.session_state.task_list_text = tl
                    parsed = parse_tasks(tl)
                    if parsed:
                        st.session_state.today_tasks = parsed
                    st.success("📋 タスクリストが作成されました！")
                    st.code(tl, language="")
        else:
            with st.chat_message("user", avatar="👤"):
                st.markdown(msg["content"])

    # Slack 送信パネル
    if st.session_state.task_list_text and not st.session_state.slack_sent:
        st.markdown("---")
        st.markdown("**📤 Slackに送りますか？**")
        c1, c2, c3 = st.columns(3)
        date_str = now_jst().strftime("%m/%d")

        def _save_tasks():
            d = load_user_data(user_info["id"])
            d["today_tasks"] = st.session_state.today_tasks
            d["last_updated"] = today_str_jst()
            save_user_data(user_info["id"], d)

        with c1:
            if st.button("個人チャンネルへ", type="primary", use_container_width=True):
                if send_to_slack(
                    f"【{user_name}さんの本日のタスク - {date_str}】\n{st.session_state.task_list_text}",
                    [SLACK_CHANNEL_PERSONAL],
                ):
                    _save_tasks()
                    st.success("✅ 送信・保存しました！")
                    st.session_state.slack_sent = True
        with c2:
            if st.button("チームにも共有", use_container_width=True):
                if send_to_slack(
                    f"【{user_name}さんの本日のタスク - {date_str}】\n{st.session_state.task_list_text}",
                    [SLACK_CHANNEL_PERSONAL, SLACK_CHANNEL_SHARED],
                ):
                    _save_tasks()
                    st.success("✅ 個人＋チームに送信しました！")
                    st.session_state.slack_sent = True
        with c3:
            if st.button("送らない（保存のみ）", use_container_width=True):
                _save_tasks()
                st.success("✅ 保存しました！")
                st.session_state.slack_sent = True
                st.rerun()

    # 入力（音声 + テキスト）
    voice_text = render_voice_input()
    final_input = voice_text

    if not final_input:
        final_input = st.chat_input("メッセージを入力してください...")

    if final_input:
        st.session_state.messages.append({"role": "user", "content": final_input})
        with st.spinner("考え中..."):
            reply = chat_with_claude(st.session_state.messages, system)
        st.session_state.messages.append({"role": "assistant", "content": reply})
        tl = extract_task_list(reply)
        if tl:
            st.session_state.task_list_text = tl
            parsed = parse_tasks(tl)
            if parsed:
                st.session_state.today_tasks = parsed
            st.session_state.slack_sent = False
        st.rerun()

    st.markdown("---")
    if st.button("🔚　セッションを終了する", use_container_width=True):
        st.session_state.update(
            session_started=False, session_type=None,
            messages=[], task_list_text=None, slack_sent=False,
        )
        st.rerun()

    st.stop()


# ══════════════════════════════════════════════════════════
# ④ 仕事おわりセッション
# ══════════════════════════════════════════════════════════
if st.session_state.session_type == "evening":
    user_name = st.session_state.current_user
    user_info = USERS[user_name]

    st.markdown('<div class="badge evening">🌙 仕事おわりセッション</div>', unsafe_allow_html=True)
    st.markdown(f"**{user_name}さん**")

    # タスクチェックリスト
    tasks = st.session_state.evening_tasks
    if tasks:
        st.markdown("### ✅ 今日のタスク確認")
        st.caption("完了したものにチェック　🗑 不要になったものは削除（持ち越しに含まれません）")

        to_remove = []
        for i, task in enumerate(tasks):
            col_chk, col_txt, col_del = st.columns([1, 8, 1])
            with col_chk:
                checked = st.checkbox("", value=task.get("completed", False), key=f"chk_{i}")
                tasks[i]["completed"] = checked
            with col_txt:
                dl = f"　📅 {task['deadline']}" if task.get("deadline") else ""
                label = f"~~{task['text']}~~" if checked else task["text"]
                st.markdown(f"{label}{dl}")
            with col_del:
                if st.button("🗑", key=f"del_t_{i}", help="次回に持ち越さない"):
                    to_remove.append(i)

        if to_remove:
            for idx in sorted(to_remove, reverse=True):
                tasks.pop(idx)
            st.session_state.evening_tasks = tasks
            st.rerun()

        done   = sum(1 for t in tasks if t.get("completed"))
        undone = len(tasks) - done
        st.caption(f"完了: {done}件　未完了（持ち越し候補）: {undone}件")
        st.markdown("---")

    # 振り返り会話
    if len(st.session_state.messages) == 0:
        with st.spinner("相田ミクが準備中..."):
            trigger = (
                "セッションを開始してください。"
                "必ず指定の挨拶から始めてください。"
            )
            st.session_state.messages.append({"role": "user", "content": trigger})
            reply = chat_with_claude(st.session_state.messages, EVENING_SYSTEM_PROMPT)
            st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

    for i, msg in enumerate(st.session_state.messages):
        if i == 0:
            continue
        if msg["role"] == "assistant":
            with st.chat_message("assistant", avatar="🌸"):
                st.markdown(msg["content"])
        else:
            with st.chat_message("user", avatar="👤"):
                st.markdown(msg["content"])

    # 入力（音声 + テキスト）
    voice_text = render_voice_input()
    final_input = voice_text

    if not final_input:
        final_input = st.chat_input("メッセージを入力してください...")

    if final_input:
        st.session_state.messages.append({"role": "user", "content": final_input})
        with st.spinner("考え中..."):
            reply = chat_with_claude(st.session_state.messages, EVENING_SYSTEM_PROMPT)
        st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

    # 終了ボタン
    st.markdown("---")
    col_end, col_save = st.columns(2)
    with col_end:
        if st.button("🔚　終了（保存しない）", use_container_width=True):
            st.session_state.update(
                session_started=False, session_type=None,
                messages=[], evening_tasks=[], slack_sent=False,
            )
            st.rerun()
    with col_save:
        if st.button("✅　承認して翌日タスクを保存", type="primary", use_container_width=True):
            uncompleted = [t for t in st.session_state.evening_tasks if not t.get("completed", False)]
            d = load_user_data(user_info["id"])
            d["carry_over_tasks"] = uncompleted
            d["today_tasks"] = []
            save_user_data(user_info["id"], d)

            if uncompleted and SLACK_CHANNEL_PERSONAL:
                date_str = now_jst().strftime("%m/%d")
                lines = "\n".join(
                    f"・{t['text']}" + (f"（期限: {t['deadline']}）" if t.get("deadline") else "")
                    for t in uncompleted
                )
                send_to_slack(
                    f"【{user_name}さんの明日の持ち越しタスク - {date_str}】\n{lines}",
                    [SLACK_CHANNEL_PERSONAL],
                )
            st.success(f"✅ 承認完了！未完了 {len(uncompleted)}件を明日に持ち越しました。")
            st.session_state.update(
                session_started=False, session_type=None,
                messages=[], evening_tasks=[], slack_sent=False,
            )
            st.rerun()
