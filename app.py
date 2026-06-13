import streamlit as st
import anthropic
import os
import json
import uuid
from datetime import datetime, date
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from pathlib import Path

load_dotenv()

# ── クライアント初期化 ─────────────────────────────────────
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
slack_client  = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))

SLACK_CHANNEL_PERSONAL = os.getenv("SLACK_CHANNEL_PERSONAL", "")
SLACK_CHANNEL_SHARED   = os.getenv("SLACK_CHANNEL_SHARED", "")

DATA_FILE = Path("femicross_data.json")


# ── データ永続化 ───────────────────────────────────────────
def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"carry_over_tasks": [], "customizations": [], "today_tasks": [], "last_updated": ""}


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── システムプロンプト ─────────────────────────────────────
BASE_MORNING = """あなたはFemiCrossのAIエージェント「相田ミク」です。
仕事はじめのセッションを担当しています。

【キャラクター】
名前: 相田ミク
口調: フレンドリーで励ましてくれる同僚。敬語ベース。日本語で話す。

【最初の挨拶（必須・一字一句この通りに）】
「おはようございます。フェミクロスAIエージェントの相田ミクです。今日も一緒にがんばりましょう！今日のタスクと期日を教えてください。」

【ルール】
- タスクについての深掘り質問は各タスクにつき最大2つまで（期限・優先度に絞る）
- 質問は1つずつ行う
- 全タスクが揃ったら確認して出力する

【タスクリスト出力フォーマット（確認が取れたら必ず出力）】
---TASK_LIST---
・タスク名｜期限: MM/DD｜優先度: 高/中/低
・タスク名｜期限: MM/DD｜優先度: 高/中/低
---END_TASK_LIST---"""

BASE_EVENING = """あなたはFemiCrossのAIエージェント「相田ミク」です。
仕事おわりのセッションを担当しています。

【キャラクター】
名前: 相田ミク
口調: ねぎらいを込めた同僚のような存在。敬語ベース。日本語で話す。

【最初の挨拶（必須・一字一句この通りに）】
「お疲れさまです。フェミクロスAIエージェントの相田ミクです。今日も一日お疲れさまでした！」

【役割】
振り返りの会話サポート。タスクのチェックはUI側で行うのでAIは会話に集中する。
「今日はどうでしたか？」と聞いて、今日を労い、明日への前向きな気持ちを引き出す。"""


def build_system_prompt(base: str, customizations: list) -> str:
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


def parse_tasks_from_text(text: str) -> list:
    """タスクリストテキストをパースしてリストに変換"""
    tasks = []
    for line in text.strip().split("\n"):
        line = line.strip().lstrip("・-• ").strip()
        if not line:
            continue
        parts = line.split("｜")
        task_text = parts[0].strip()
        deadline, priority = "", ""
        for part in parts[1:]:
            p = part.strip()
            if "期限" in p:
                deadline = p.replace("期限:", "").replace("期限：", "").strip()
            elif "優先度" in p:
                priority = p.replace("優先度:", "").replace("優先度：", "").strip()
        if task_text:
            tasks.append({
                "id": str(uuid.uuid4()),
                "text": task_text,
                "deadline": deadline,
                "priority": priority,
                "completed": False,
            })
    return tasks


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
    text = text.strip()
    if not text:
        return
    # セッション状態に追加
    if text not in st.session_state.customizations:
        st.session_state.customizations.append(text)
    # JSONに永続保存
    data = load_data()
    if text not in data["customizations"]:
        data["customizations"].append(text)
        save_data(data)
    # セッション中なら相田ミクに通知
    if st.session_state.session_started:
        notify = (
            f"【対話スタイル変更のお知らせ】ユーザーから次のリクエストがありました:「{text}」"
            f"今後はこのスタイルで対応してください。変更を受け付けたことを一言だけ伝えてください。"
        )
        st.session_state.messages.append({"role": "user", "content": notify})
        reply = chat_with_claude(
            st.session_state.messages,
            st.session_state.session_type,
            st.session_state.customizations,
        )
        st.session_state.messages.append({"role": "assistant", "content": reply})


# ── ページ設定 ─────────────────────────────────────────────
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
.carry    { background:#fef9c3; border-left:3px solid #eab308;
            border-radius:6px; padding:6px 12px; margin:3px 0; font-size:13px; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="title">🌸 FemiCross AI秘書</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">相田ミク</div>', unsafe_allow_html=True)

# ── 起動時にデータを読み込む ──────────────────────────────
app_data = load_data()

# ── セッション状態の初期化 ────────────────────────────────
for key, val in [
    ("session_started",   False),
    ("session_type",      None),
    ("messages",          []),
    ("task_list_text",    None),
    ("today_tasks",       []),
    ("slack_sent",        False),
    ("customizations",    app_data.get("customizations", [])),  # JSONから復元
    ("show_custom_panel", False),
    ("evening_tasks",     []),
]:
    if key not in st.session_state:
        st.session_state[key] = val


# ── 対話修正パネル（共通）────────────────────────────────
def render_custom_panel():
    if not st.session_state.show_custom_panel:
        return
    with st.container(border=True):
        st.markdown("### 🛠 AIの対話スタイルを調整する")
        st.caption("✨ この設定は次回以降も記憶されます")

        if st.session_state.customizations:
            st.markdown("**現在の設定:**")
            for i, c in enumerate(st.session_state.customizations):
                col_t, col_d = st.columns([6, 1])
                with col_t:
                    st.markdown(f'<span class="ctag">✅ {c}</span>', unsafe_allow_html=True)
                with col_d:
                    if st.button("✕", key=f"del_c_{i}"):
                        st.session_state.customizations.pop(i)
                        data = load_data()
                        data["customizations"] = st.session_state.customizations
                        save_data(data)
                        st.rerun()
            st.markdown("")

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
                if st.button(ex, key=f"ex_c_{i}", use_container_width=True):
                    apply_customization(ex)
                    st.session_state.show_custom_panel = False
                    st.rerun()

        st.markdown("**または自由に入力:**")
        custom_input = st.text_area(
            "リクエスト",
            placeholder="例: もっと明るいトーンで話して...",
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
# ① スタート画面
# ═══════════════════════════════════════════════════════════
if not st.session_state.session_started:
    now = datetime.now()
    st.markdown(
        f"<p style='text-align:center;color:gray;font-size:13px;'>"
        f"{now.strftime('%Y年%m月%d日 %H:%M')}</p>",
        unsafe_allow_html=True,
    )

    # 持ち越しタスクの表示
    carry_over = app_data.get("carry_over_tasks", [])
    if carry_over:
        st.markdown(f"**⏭ 前回からの持ち越しタスク（{len(carry_over)}件）:**")
        for t in carry_over:
            dl = f"　📅 {t['deadline']}" if t.get("deadline") else ""
            st.markdown(f'<div class="carry">・{t["text"]}{dl}</div>', unsafe_allow_html=True)
        st.markdown("")

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("🌅　仕事はじめ", use_container_width=True, type="primary"):
            st.session_state.update(
                session_started=True, session_type="morning",
                messages=[], task_list_text=None, today_tasks=[], slack_sent=False,
                show_custom_panel=False,
            )
            st.rerun()
    with col2:
        if st.button("🌙　仕事おわり", use_container_width=True):
            data = load_data()
            st.session_state.update(
                session_started=True, session_type="evening",
                messages=[], task_list_text=None, slack_sent=False,
                show_custom_panel=False,
                evening_tasks=data.get("today_tasks", []),
            )
            st.rerun()
    with col3:
        badge = " ✅" if st.session_state.customizations else ""
        if st.button(f"🛠　対話修正{badge}", use_container_width=True):
            st.session_state.show_custom_panel = not st.session_state.show_custom_panel
            st.rerun()

    if st.session_state.customizations:
        st.markdown("**現在の対話設定:**")
        tags = "".join(f'<span class="ctag">✅ {c}</span>' for c in st.session_state.customizations)
        st.markdown(tags, unsafe_allow_html=True)
        st.markdown("")

    render_custom_panel()

    if is_evening_time():
        st.info("💡 16時を過ぎています。「仕事おわり」セッションがおすすめです。")


# ═══════════════════════════════════════════════════════════
# ② 仕事はじめセッション
# ═══════════════════════════════════════════════════════════
elif st.session_state.session_type == "morning":
    st.markdown('<div class="badge morning">🌅 仕事はじめセッション</div>', unsafe_allow_html=True)

    badge = " ✅" if st.session_state.customizations else ""
    if st.button(f"🛠　対話修正{badge}"):
        st.session_state.show_custom_panel = not st.session_state.show_custom_panel
        st.rerun()
    render_custom_panel()

    # 最初のメッセージを自動生成
    carry_over = app_data.get("carry_over_tasks", [])
    if len(st.session_state.messages) == 0:
        with st.spinner("相田ミクが準備中..."):
            carry_text = ""
            if carry_over:
                lines = "\n".join(
                    f"・{t['text']}" + (f"（期限: {t['deadline']}）" if t.get("deadline") else "")
                    for t in carry_over
                )
                carry_text = f"\n\n【前回からの持ち越しタスク】\n{lines}\nこれらを最初に提示してから、新しいタスクも追加で聞いてください。"

            trigger = (
                "セッションを開始してください。"
                "必ず「おはようございます。フェミクロスAIエージェントの相田ミクです。"
                "今日も一緒にがんばりましょう！今日のタスクと期日を教えてください。」"
                "という挨拶から始めてください。" + carry_text
            )
            st.session_state.messages.append({"role": "user", "content": trigger})
            reply = chat_with_claude(st.session_state.messages, "morning", st.session_state.customizations)
            st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

    # チャット履歴表示
    for i, msg in enumerate(st.session_state.messages):
        if i == 0:
            continue
        if msg["role"] == "user" and "【対話スタイル変更のお知らせ】" in msg["content"]:
            continue
        if msg["role"] == "assistant":
            with st.chat_message("assistant", avatar="🌸"):
                st.markdown(strip_task_markers(msg["content"]))
                tl = extract_task_list(msg["content"])
                if tl:
                    st.session_state.task_list_text = tl
                    parsed = parse_tasks_from_text(tl)
                    if parsed:
                        st.session_state.today_tasks = parsed
                    st.success("📋 タスクリストが作成されました！")
                    st.code(tl, language="")
        else:
            with st.chat_message("user", avatar="👤"):
                st.markdown(msg["content"])

    # Slack送信・保存パネル
    if st.session_state.task_list_text and not st.session_state.slack_sent:
        st.markdown("---")
        st.markdown("**📤 Slackに送りますか？**")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("個人チャンネルへ", type="primary", use_container_width=True):
                date_str = datetime.now().strftime("%m/%d")
                if send_to_slack(f"【本日のタスク - {date_str}】\n{st.session_state.task_list_text}",
                                 [SLACK_CHANNEL_PERSONAL]):
                    data = load_data()
                    data["today_tasks"] = st.session_state.today_tasks
                    data["last_updated"] = str(date.today())
                    save_data(data)
                    st.success("✅ 送信・保存しました！")
                    st.session_state.slack_sent = True
        with c2:
            if st.button("チームにも共有", use_container_width=True):
                date_str = datetime.now().strftime("%m/%d")
                if send_to_slack(f"【本日のタスク - {date_str}】\n{st.session_state.task_list_text}",
                                 [SLACK_CHANNEL_PERSONAL, SLACK_CHANNEL_SHARED]):
                    data = load_data()
                    data["today_tasks"] = st.session_state.today_tasks
                    data["last_updated"] = str(date.today())
                    save_data(data)
                    st.success("✅ 個人＋チームに送信しました！")
                    st.session_state.slack_sent = True
        with c3:
            if st.button("送らない（保存のみ）", use_container_width=True):
                data = load_data()
                data["today_tasks"] = st.session_state.today_tasks
                data["last_updated"] = str(date.today())
                save_data(data)
                st.success("✅ タスクを保存しました！")
                st.session_state.slack_sent = True
                st.rerun()

    if prompt := st.chat_input("メッセージを入力してください..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.spinner("考え中..."):
            reply = chat_with_claude(st.session_state.messages, "morning", st.session_state.customizations)
        st.session_state.messages.append({"role": "assistant", "content": reply})
        tl = extract_task_list(reply)
        if tl:
            st.session_state.task_list_text = tl
            parsed = parse_tasks_from_text(tl)
            if parsed:
                st.session_state.today_tasks = parsed
            st.session_state.slack_sent = False
        st.rerun()

    st.markdown("---")
    if st.button("🔚　セッションを終了する", use_container_width=True):
        st.session_state.update(
            session_started=False, session_type=None,
            messages=[], task_list_text=None, slack_sent=False, show_custom_panel=False,
        )
        st.rerun()


# ═══════════════════════════════════════════════════════════
# ③ 仕事おわりセッション
# ═══════════════════════════════════════════════════════════
elif st.session_state.session_type == "evening":
    st.markdown('<div class="badge evening">🌙 仕事おわりセッション</div>', unsafe_allow_html=True)

    badge = " ✅" if st.session_state.customizations else ""
    if st.button(f"🛠　対話修正{badge}"):
        st.session_state.show_custom_panel = not st.session_state.show_custom_panel
        st.rerun()
    render_custom_panel()

    # ── タスクチェックリスト ──────────────────────────────
    tasks = st.session_state.evening_tasks
    if tasks:
        st.markdown("### ✅ 今日のタスク確認")
        st.caption("完了したものにチェック　🗑 不要になったものは削除")

        to_remove = []
        for i, task in enumerate(tasks):
            col_chk, col_txt, col_del = st.columns([1, 8, 1])
            with col_chk:
                checked = st.checkbox("", value=task.get("completed", False), key=f"chk_{i}")
                tasks[i]["completed"] = checked
            with col_txt:
                dl = f"　📅 {task['deadline']}" if task.get("deadline") else ""
                pr = f"　🔖 {task['priority']}" if task.get("priority") else ""
                label = f"~~{task['text']}~~" if checked else task["text"]
                st.markdown(f"{label}{dl}{pr}")
            with col_del:
                if st.button("🗑", key=f"del_t_{i}", help="次回に持ち越さない"):
                    to_remove.append(i)

        if to_remove:
            for idx in sorted(to_remove, reverse=True):
                tasks.pop(idx)
            st.session_state.evening_tasks = tasks
            st.rerun()

        done  = sum(1 for t in tasks if t.get("completed"))
        undone = len(tasks) - done
        st.caption(f"完了: {done}件　未完了（持ち越し候補）: {undone}件")
        st.markdown("---")

    # ── 振り返り会話 ─────────────────────────────────────
    if len(st.session_state.messages) == 0:
        with st.spinner("相田ミクが準備中..."):
            trigger = (
                "セッションを開始してください。"
                "必ず「お疲れさまです。フェミクロスAIエージェントの相田ミクです。"
                "今日も一日お疲れさまでした！」という挨拶から始めてください。"
            )
            st.session_state.messages.append({"role": "user", "content": trigger})
            reply = chat_with_claude(st.session_state.messages, "evening", st.session_state.customizations)
            st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

    for i, msg in enumerate(st.session_state.messages):
        if i == 0:
            continue
        if msg["role"] == "user" and "【対話スタイル変更のお知らせ】" in msg["content"]:
            continue
        if msg["role"] == "assistant":
            with st.chat_message("assistant", avatar="🌸"):
                st.markdown(msg["content"])
        else:
            with st.chat_message("user", avatar="👤"):
                st.markdown(msg["content"])

    if prompt := st.chat_input("メッセージを入力してください..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.spinner("考え中..."):
            reply = chat_with_claude(st.session_state.messages, "evening", st.session_state.customizations)
        st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

    # ── 終了ボタン群 ─────────────────────────────────────
    st.markdown("---")
    col_end, col_save = st.columns(2)
    with col_end:
        if st.button("🔚　終了（保存しない）", use_container_width=True):
            st.session_state.update(
                session_started=False, session_type=None,
                messages=[], evening_tasks=[], slack_sent=False, show_custom_panel=False,
            )
            st.rerun()
    with col_save:
        if st.button("✅　承認して翌日タスクを保存", type="primary", use_container_width=True):
            uncompleted = [t for t in st.session_state.evening_tasks if not t.get("completed", False)]

            # 翌日持ち越しとして保存
            data = load_data()
            data["carry_over_tasks"] = uncompleted
            data["today_tasks"] = []
            save_data(data)

            # Slackに持ち越しリストを送信
            if uncompleted and SLACK_CHANNEL_PERSONAL:
                date_str = datetime.now().strftime("%m/%d")
                lines = "\n".join(
                    f"・{t['text']}" + (f"（期限: {t['deadline']}）" if t.get("deadline") else "")
                    for t in uncompleted
                )
                send_to_slack(f"【明日の持ち越しタスク - {date_str}】\n{lines}", [SLACK_CHANNEL_PERSONAL])

            st.success(f"✅ 承認完了！未完了 {len(uncompleted)}件を明日に持ち越しました。")
            st.session_state.update(
                session_started=False, session_type=None,
                messages=[], evening_tasks=[], slack_sent=False, show_custom_panel=False,
            )
            st.rerun()
