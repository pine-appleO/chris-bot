import os
import threading
import time
import requests
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import pytz
import schedule

app = Flask(__name__)

LINE_TOKEN  = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
USER_ID     = os.environ.get("LINE_USER_ID", "")
IG_TOKEN    = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
IG_USER_ID  = os.environ.get("INSTAGRAM_USER_ID", "")

configuration = Configuration(access_token=LINE_TOKEN)
handler = WebhookHandler(LINE_SECRET)
JST = pytz.timezone("Asia/Tokyo")

# ── 週別タスク ─────────────────────────────────────────────────────
WEEKLY_TASKS = {
    0: ["📋 今週のタスク・仕入れ確認", "📊 先週のインスタ数値振り返り", "📝 今週の投稿計画（目標10投稿）", "💬 口コミ・DM返信"],
    1: ["📱 インスタ投稿", "🥩 仕入れ・在庫確認", "💬 口コミ返信"],
    2: ["🔴 定休日", "📊 週半レポート確認", "✏️ 翌日以降の投稿準備"],
    3: ["📱 インスタ投稿", "🏪 店舗運営確認", "📞 予約確認・調整"],
    4: ["📱 インスタ投稿（週末向け）", "💡 来週コンテンツ企画", "📋 週末スタッフ連絡"],
    5: ["📸 シャトーブリアン動画・写真撮影", "📱 インスタ投稿", "🔥 週末ピーク準備"],
    6: ["📊 週次まとめ", "📱 インスタ投稿", "📋 翌週仕込み・準備"],
}

MONTHLY_TASKS = {
    1:  ["🗓️ 月初：先月の数値まとめ", "📋 今月の目標設定"],
    15: ["📊 月半レポート確認"],
}

# 特別予定（手動で追加・日付はYYYY-MM-DD形式）
SPECIAL_EVENTS = [
    # {"date": "2026-04-25", "name": "撮影"},
]

BEEF_FACTS = [
    "🥩 シャトーブリアンは牛1頭から約200gしか取れないヒレの中心部。うしうらら は毎日A5雌牛を直送しています。",
    "🥩 A5ランクの「5」は脂肪交雑・色沢・きめなど5項目すべてが最高評価。雌牛は脂のきめが細かく、より上品な甘みが出ます。",
    "🥩 シャトーブリアンの名前はフランスの外交官ヴィコント・ド・シャトーブリアンに由来。19世紀パリで生まれた格式ある調理法です。",
    "🥩 横浜・関内エリアでシャトーブリアンを看板コースにしているのは、うしうらら が数少ない存在。希少性を積極的に発信しましょう。",
    "🥩 ミディアムレアは内部温度55〜60℃。シャトーブリアンはこの焼き加減でジューシーさと旨みのピークが重なります。",
]
BEEF_FACT_IDX = [0]

# ── 天気（wttr.in アカウント不要）────────────────────────────────
def get_weather(location, city_name):
    try:
        url = f"https://wttr.in/{location}?format=%C+%t+%f&lang=ja"
        r = requests.get(url, timeout=5)
        text = r.text.strip()
        return f"🌤️ {city_name}：{text}"
    except Exception:
        return f"⛅ {city_name} 天気取得失敗"

# ── Instagram ─────────────────────────────────────────────────────
def get_instagram_yesterday():
    if not IG_TOKEN or not IG_USER_ID:
        return "📱 Instagram データ未設定"
    try:
        since = int((datetime.now(JST) - timedelta(days=1)).replace(hour=0, minute=0, second=0).timestamp())
        until = int(datetime.now(JST).replace(hour=0, minute=0, second=0).timestamp())
        fields = "timestamp,like_count,comments_count,reach,saved"
        url = f"https://graph.instagram.com/{IG_USER_ID}/media?fields={fields}&since={since}&until={until}&access_token={IG_TOKEN}"
        r = requests.get(url, timeout=5).json()
        posts = r.get("data", [])
        if not posts:
            return "📱 Instagram：昨日の投稿なし"
        lines = ["📱 Instagram 昨日の投稿"]
        for p in posts[:3]:
            lines.append(f"  ❤️ {p.get('like_count','-')}  💬 {p.get('comments_count','-')}  👁️ {p.get('reach','-')}  🔖 {p.get('saved','-')}")
        return "\n".join(lines)
    except Exception:
        return "📱 Instagram 取得失敗"

def get_monthly_ig_summary():
    if not IG_TOKEN or not IG_USER_ID:
        return "📱 Instagram データ未設定"
    try:
        now = datetime.now(JST)
        since = int(now.replace(day=1, hour=0, minute=0, second=0).timestamp())
        fields = "timestamp,like_count,comments_count,reach,saved"
        url = f"https://graph.instagram.com/{IG_USER_ID}/media?fields={fields}&since={since}&access_token={IG_TOKEN}&limit=100"
        r = requests.get(url, timeout=10).json()
        posts = r.get("data", [])
        if not posts:
            return "📱 今月投稿なし"
        total_likes    = sum(p.get("like_count", 0) for p in posts)
        total_comments = sum(p.get("comments_count", 0) for p in posts)
        total_reach    = sum(p.get("reach", 0) for p in posts)
        total_saved    = sum(p.get("saved", 0) for p in posts)
        return (f"📱 Instagram 今月サマリー（{len(posts)}投稿）\n"
                f"  ❤️ 合計いいね：{total_likes}（平均{total_likes//len(posts)}）\n"
                f"  💬 合計コメント：{total_comments}\n"
                f"  👁️ 合計リーチ：{total_reach}\n"
                f"  🔖 合計保存：{total_saved}")
    except Exception:
        return "📱 Instagram 取得失敗"

# ── 特別予定リマインド ──────────────────────────────────────────
def get_upcoming_events(days_ahead=3):
    now = datetime.now(JST).date()
    lines = []
    for event in SPECIAL_EVENTS:
        event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()
        diff = (event_date - now).days
        if 0 < diff <= days_ahead:
            lines.append(f"🍍 {event['name']}まであと{diff}日！")
        elif diff == 0:
            lines.append(f"🏝️ 今日は{event['name']}の日！")
    return "\n".join(lines) if lines else ""

# ── 朝のメッセージ ────────────────────────────────────────────────
def build_morning_message():
    now = datetime.now(JST)
    weekday = now.weekday()
    day_names = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = now.strftime(f"%Y年%-m月%-d日（{day_names[weekday]}）")

    yokohama  = get_weather("Yokohama", "横浜")
    sodegaura = get_weather("Sodegaura", "袖ヶ浦のぞみ野")

    tasks = WEEKLY_TASKS.get(weekday, []) + MONTHLY_TASKS.get(now.day, [])
    task_text = "\n".join(f"  • {t}" for t in tasks)

    ig   = get_instagram_yesterday()
    fact = BEEF_FACTS[BEEF_FACT_IDX[0] % len(BEEF_FACTS)]
    BEEF_FACT_IDX[0] += 1

    events = get_upcoming_events(3)
    event_section = f"\n━━━ 🍍 近日予定 ━━━\n{events}\n" if events else ""

    return (f"アロハ🤙 プルおさん！\n{date_str}\n\n"
            f"{yokohama}\n{sodegaura}\n"
            f"{event_section}\n"
            f"━━━ 今日のタスク ━━━\n{task_text}\n\n"
            f"━━━ 昨日のインスタ ━━━\n{ig}\n\n"
            f"━━━ 今日の牛ネタ 🥩 ━━━\n{fact}\n\n"
            f"今日もよろしく！🏝️")

def build_tomorrow_schedule():
    now = datetime.now(JST)
    tomorrow = now + timedelta(days=1)
    weekday = tomorrow.weekday()
    day_names = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = tomorrow.strftime(f"%-m月%-d日（{day_names[weekday]}）")

    tasks = WEEKLY_TASKS.get(weekday, []) + MONTHLY_TASKS.get(tomorrow.day, [])
    task_text = "\n".join(f"  • {t}" for t in tasks)

    events = []
    for event in SPECIAL_EVENTS:
        event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()
        if event_date == tomorrow.date():
            events.append(f"  🍍 {event['name']}")
    event_text = "\n".join(events)

    msg = f"明日 {date_str} の予定 🏝️\n\n━━━ タスク ━━━\n{task_text}"
    if event_text:
        msg += f"\n\n━━━ 特別予定 ━━━\n{event_text}"
    return msg

def build_monthly_report():
    now = datetime.now(JST)
    report_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y年%-m月")
    ig = get_monthly_ig_summary()
    return (f"📊 {report_month} 月次レポート 🍍\n\n{ig}\n\n"
            f"━━━ 来月に向けて ━━━\n"
            f"  • Reels投稿：月4本以上（シャトーブリアン）\n"
            f"  • 投稿数：月10本以上をキープ\n"
            f"  • 保存数アップ施策：希少性テキスト強化\n"
            f"  • シェア誘発コンテンツ：「連れて行きたい人」訴求\n\n"
            f"来月もいくぞ！🤙🏝️")

def send_to_user(text):
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=USER_ID, messages=[TextMessage(text=text)])
            )
    except Exception as e:
        print(f"送信失敗: {e}")

# ── スケジューラー ─────────────────────────────────────────────────
def run_scheduler():
    schedule.every().day.at("07:00").do(lambda: send_to_user(build_morning_message()))

    def reminder_check():
        msg = get_upcoming_events(3)
        if msg:
            send_to_user(f"🍍 リマインド！\n{msg}")

    def monthly_report_check():
        if (datetime.now(JST) + timedelta(days=1)).day == 1:
            send_to_user(build_monthly_report())

    schedule.every().day.at("09:00").do(reminder_check)
    schedule.every().day.at("22:00").do(monthly_report_check)

    while True:
        schedule.run_pending()
        time.sleep(30)

# ── Webhook ────────────────────────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    now  = datetime.now(JST)

    def match(keywords):
        return any(text.startswith(k) or text == k for k in keywords)

    if match(["おはよう", "アロハ", "朝", "morning"]):
        reply = build_morning_message()
    elif match(["明日の予定", "明日", "tomorrow"]):
        reply = build_tomorrow_schedule()
    elif match(["天気", "weather"]):
        reply = get_weather("Yokohama", "横浜") + "\n" + get_weather("Sodegaura", "袖ヶ浦のぞみ野")
    elif match(["インスタ", "instagram", "IG"]):
        reply = get_instagram_yesterday()
    elif match(["月報", "レポート", "report"]):
        reply = build_monthly_report()
    elif match(["タスク", "todo", "今日"]):
        tasks = WEEKLY_TASKS.get(now.weekday(), [])
        reply = "今日のタスク 🍍\n" + "\n".join(f"• {t}" for t in tasks)
    elif match(["ヘルプ", "help", "使い方"]):
        reply = ("📖 使い方 🤙\n"
                 "「アロハ」or「おはよう」→ 朝のまとめ\n"
                 "「明日の予定」→ 明日のスケジュール\n"
                 "「天気」→ 横浜・袖ヶ浦の天気\n"
                 "「インスタ」→ 昨日のInstagram\n"
                 "「タスク」→ 今日のToDoリスト\n"
                 "「月報」→ 今月のまとめ")
    else:
        reply = f"📌 メモしました！\n「{text}」🍍"

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply)])
        )

@app.route("/")
def index():
    return "Chris 稼働中 ✅🤙"

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
