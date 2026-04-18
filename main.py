import os
import threading
import time
import requests
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import pytz
import schedule

app = Flask(__name__)

LINE_TOKEN  = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
USER_ID     = os.environ.get("LINE_USER_ID", "")
WEATHER_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
IG_TOKEN    = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
IG_USER_ID  = os.environ.get("INSTAGRAM_USER_ID", "")

line_bot_api = LineBotApi(LINE_TOKEN)
handler = WebhookHandler(LINE_SECRET)
JST = pytz.timezone("Asia/Tokyo")

# ── スケジュール別タスク ───────────────────────────────────────────
WEEKLY_TASKS = {
    0: [  # 月曜
        "📋 今週のタスク・仕入れ確認",
        "📊 先週のインスタ数値振り返り",
        "📝 今週の投稿計画（目標10投稿）",
        "💬 口コミ・DM返信",
    ],
    1: [  # 火曜（第2・4は定休）
        "📱 インスタ投稿",
        "🥩 仕入れ・在庫確認",
        "💬 口コミ返信",
    ],
    2: [  # 水曜（定休）
        "🔴 定休日",
        "📊 週半レポート確認",
        "✏️ 翌日以降の投稿準備",
    ],
    3: [  # 木曜
        "📱 インスタ投稿",
        "🏪 店舗運営確認",
        "📞 予約確認・調整",
    ],
    4: [  # 金曜
        "📱 インスタ投稿（週末向け）",
        "💡 来週コンテンツ企画",
        "📋 週末スタッフ連絡",
    ],
    5: [  # 土曜
        "📸 シャトーブリアン動画・写真撮影",
        "📱 インスタ投稿",
        "🔥 週末ピーク準備",
    ],
    6: [  # 日曜
        "📊 週次まとめ",
        "📱 インスタ投稿",
        "📋 翌週仕込み・準備",
    ],
}

MONTHLY_TASKS = {
    1:  ["🗓️ 月初：先月の数値まとめ", "📋 今月の目標設定"],
    15: ["📊 月半レポート確認"],
}

BEEF_FACTS = [
    "🥩 シャトーブリアンは牛1頭から約200gしか取れないヒレの中心部。うしうらら は毎日A5雌牛を直送しています。",
    "🥩 A5ランクの「5」は脂肪交雑・色沢・きめなど5項目すべてが最高評価。雌牛は脂のきめが細かく、より上品な甘みが出ます。",
    "🥩 シャトーブリアンの名前はフランスの外交官ヴィコント・ド・シャトーブリアンに由来。19世紀パリで生まれた格式ある調理法です。",
    "🥩 横浜・関内エリアでシャトーブリアンを看板コースにしているのは、うしうらら が数少ない存在。希少性を積極的に発信しましょう。",
    "🥩 肉割烹スタイルは「焼肉」でも「鉄板焼き」でもない第三の形態。出汁ベースの割烹料理と和牛を融合させたうしうらら独自の世界観です。",
    "🥩 ミディアムレアは内部温度55〜60℃。シャトーブリアンはこの焼き加減でジューシーさと旨みのピークが重なります。",
]
BEEF_FACT_IDX = [0]

def get_weather(city_id=None, lat=None, lon=None, city_name=""):
    if not WEATHER_KEY:
        return f"⛅ {city_name} 天気取得不可（APIキー未設定）"
    try:
        if city_id:
            url = f"https://api.openweathermap.org/data/2.5/weather?id={city_id}&appid={WEATHER_KEY}&units=metric&lang=ja"
        else:
            url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={WEATHER_KEY}&units=metric&lang=ja"
        r = requests.get(url, timeout=5).json()
        desc  = r["weather"][0]["description"]
        temp  = round(r["main"]["temp"])
        feels = round(r["main"]["feels_like"])
        return f"{'☀️' if '晴' in desc else '🌧️' if '雨' in desc else '⛅'} {city_name}：{desc}　{temp}℃（体感{feels}℃）"
    except Exception as e:
        return f"⛅ {city_name} 天気取得失敗: {e}"

def get_instagram_yesterday():
    if not IG_TOKEN or not IG_USER_ID:
        return "📱 Instagram データ未設定"
    try:
        since = int((datetime.now(JST) - timedelta(days=1)).replace(hour=0, minute=0, second=0).timestamp())
        until = int(datetime.now(JST).replace(hour=0, minute=0, second=0).timestamp())
        fields = "timestamp,like_count,comments_count,reach,saved,shares_count"
        url = f"https://graph.instagram.com/{IG_USER_ID}/media?fields={fields}&since={since}&until={until}&access_token={IG_TOKEN}"
        r = requests.get(url, timeout=5).json()
        posts = r.get("data", [])
        if not posts:
            return "📱 Instagram：昨日の投稿なし"
        lines = ["📱 Instagram 昨日の投稿"]
        for p in posts[:3]:
            ts = p.get("timestamp", "")[:10]
            likes    = p.get("like_count", "-")
            comments = p.get("comments_count", "-")
            reach    = p.get("reach", "-")
            saved    = p.get("saved", "-")
            lines.append(f"  ❤️ {likes}  💬 {comments}  👁️ {reach}  🔖 {saved}")
        return "\n".join(lines)
    except Exception as e:
        return f"📱 Instagram 取得失敗: {e}"

def get_monthly_ig_summary():
    if not IG_TOKEN or not IG_USER_ID:
        return "📱 Instagram データ未設定"
    try:
        now = datetime.now(JST)
        first_day = now.replace(day=1, hour=0, minute=0, second=0)
        since = int(first_day.timestamp())
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
        avg_likes = round(total_likes / len(posts))
        return (f"📱 Instagram 今月サマリー（{len(posts)}投稿）\n"
                f"  ❤️ 合計いいね：{total_likes}（平均{avg_likes}）\n"
                f"  💬 合計コメント：{total_comments}\n"
                f"  👁️ 合計リーチ：{total_reach}\n"
                f"  🔖 合計保存：{total_saved}")
    except Exception as e:
        return f"📱 Instagram 取得失敗: {e}"

def build_morning_message():
    now = datetime.now(JST)
    weekday = now.weekday()
    day_names = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = now.strftime(f"%Y年%-m月%-d日（{day_names[weekday]}）")

    # 天気
    yokohama = get_weather(city_id=1848354, city_name="横浜")
    sodegaura = get_weather(lat=35.4282, lon=139.9987, city_name="袖ヶ浦のぞみ野")

    # タスク
    tasks = WEEKLY_TASKS.get(weekday, [])
    extra = MONTHLY_TASKS.get(now.day, [])
    task_text = "\n".join(f"  • {t}" for t in (tasks + extra))

    # インスタ
    ig = get_instagram_yesterday()

    # 牛ネタ
    fact = BEEF_FACTS[BEEF_FACT_IDX[0] % len(BEEF_FACTS)]
    BEEF_FACT_IDX[0] += 1

    msg = f"""おはようございます！プルおさん 🌅
{date_str}

{yokohama}
{sodegaura}

━━━ 今日のタスク ━━━
{task_text}

━━━ 昨日のインスタ ━━━
{ig}

━━━ 今日の牛ネタ ━━━
{fact}

今日もよろしくお願いします！💪"""
    return msg

def build_monthly_report():
    now = datetime.now(JST)
    report_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y年%-m月")
    ig = get_monthly_ig_summary()

    msg = f"""📊 {report_month} 月次レポート

{ig}

━━━ 来月に向けて ━━━
  • Reels投稿：月4本以上（シャトーブリアン）
  • 投稿数：月10本以上をキープ
  • 保存数アップ施策：希少性テキスト強化
  • シェア誘発コンテンツ：「連れて行きたい人」訴求

引き続きがんばりましょう！🥩"""
    return msg

def send_to_user(text):
    try:
        line_bot_api.push_message(USER_ID, TextSendMessage(text=text))
    except Exception as e:
        print(f"送信失敗: {e}")

# ── スケジューラー ─────────────────────────────────────────────────
def run_scheduler():
    schedule.every().day.at("07:00").do(lambda: send_to_user(build_morning_message()))

    def monthly_report_check():
        now = datetime.now(JST)
        next_day = (now + timedelta(days=1)).day
        if next_day == 1:
            send_to_user(build_monthly_report())

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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    now  = datetime.now(JST)

    if text in ["おはよう", "朝", "morning"]:
        reply = build_morning_message()
    elif text in ["天気", "weather"]:
        reply = get_weather(city_id=1848354, city_name="横浜") + "\n" + get_weather(lat=35.4282, lon=139.9987, city_name="袖ヶ浦のぞみ野")
    elif text in ["インスタ", "instagram", "IG"]:
        reply = get_instagram_yesterday()
    elif text in ["月報", "レポート", "report"]:
        reply = build_monthly_report()
    elif text in ["タスク", "todo", "今日"]:
        tasks = WEEKLY_TASKS.get(now.weekday(), [])
        reply = "今日のタスク:\n" + "\n".join(f"• {t}" for t in tasks)
    elif text in ["ヘルプ", "help", "使い方"]:
        reply = ("📖 使い方\n"
                 "「おはよう」→ 朝のまとめ\n"
                 "「天気」→ 横浜・袖ヶ浦の天気\n"
                 "「インスタ」→ 昨日のInstagram\n"
                 "「タスク」→ 今日のToDoリスト\n"
                 "「月報」→ 今月のまとめ")
    else:
        # 受け取ったメッセージをリマインダーとして保存（エコーで確認）
        reply = f"📌 メモしました！\n「{text}」\n\n※ リマインダー機能は近日追加予定"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@app.route("/")
def index():
    return "Chris 稼働中 ✅"

if __name__ == "__main__":
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
