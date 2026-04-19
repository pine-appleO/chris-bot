import os
import json
import threading
import time
import requests
from datetime import datetime, timedelta
from googleapiclient.discovery import build
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
from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric, Dimension

app = Flask(__name__)

LINE_TOKEN  = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
USER_ID     = os.environ.get("LINE_USER_ID", "")
IG_TOKEN    = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
IG_USER_ID  = os.environ.get("INSTAGRAM_USER_ID", "")
GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID", "313654685")
GA4_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_CHANNELS = {
    "yokohamalofichill": "UCYfWhwYkK_UKLCC772OY_xQ",
    "pinea_ppleO": "UCDXLRuSiPGk1kR_vq9C_iag",
}
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
SHEET_TAB = "予定"
MEMO_TAB = "メモ"
STATS_TAB = "統計"

configuration = Configuration(access_token=LINE_TOKEN)
handler = WebhookHandler(LINE_SECRET)
JST = pytz.timezone("Asia/Tokyo")

# 秘書メニューの状態管理
secretary_mode = {"active": False, "entry": False}

# ── Google Sheets（店訪問管理）────────────────────────────────────────
def _sheets_service():
    creds_dict = json.loads(GA4_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)

def _parse_visit_date(text):
    """「4/25」や「04-25」→ YYYY-MM-DD"""
    now = datetime.now(JST)
    for fmt in ("%m/%d", "%m-%d"):
        try:
            d = datetime.strptime(text.strip(), fmt)
            candidate = d.replace(year=now.year)
            if candidate.date() < now.date():
                candidate = candidate.replace(year=now.year + 1)
            return candidate.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

def get_store_visits():
    if not GA4_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID:
        return []
    try:
        result = _sheets_service().spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{SHEET_TAB}!A:B"
        ).execute()
        return result.get("values", [])
    except Exception:
        return []

def add_store_visit(date_str, time_str):
    _sheets_service().spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{SHEET_TAB}!A:B",
        valueInputOption="RAW",
        body={"values": [[date_str, time_str]]}
    ).execute()

def delete_store_visit(index):
    visits = get_store_visits()
    if index < 1 or index > len(visits):
        return False
    new_visits = [v for i, v in enumerate(visits) if i != index - 1]
    svc = _sheets_service()
    svc.spreadsheets().values().clear(
        spreadsheetId=GOOGLE_SHEET_ID, range=f"{SHEET_TAB}!A:B"
    ).execute()
    if new_visits:
        svc.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{SHEET_TAB}!A1",
            valueInputOption="RAW",
            body={"values": new_visits}
        ).execute()
    return True

def add_memo(text):
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    _sheets_service().spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{MEMO_TAB}!A:B",
        valueInputOption="RAW",
        body={"values": [[now, text]]}
    ).execute()

def get_memos():
    if not GA4_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID:
        return []
    try:
        result = _sheets_service().spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{MEMO_TAB}!A:B"
        ).execute()
        return result.get("values", [])
    except Exception:
        return []

def delete_memo(index):
    memos = get_memos()
    if index < 1 or index > len(memos):
        return False
    new_memos = [m for i, m in enumerate(memos) if i != index - 1]
    svc = _sheets_service()
    svc.spreadsheets().values().clear(
        spreadsheetId=GOOGLE_SHEET_ID, range=f"{MEMO_TAB}!A:B"
    ).execute()
    if new_memos:
        svc.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{MEMO_TAB}!A1",
            valueInputOption="RAW",
            body={"values": new_memos}
        ).execute()
    return True

def get_stat(key):
    try:
        rows = _sheets_service().spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID, range=f"{STATS_TAB}!A:B"
        ).execute().get("values", [])
        for row in rows:
            if row and row[0] == key:
                return row[1] if len(row) > 1 else None
    except Exception:
        return None

def set_stat(key, value):
    try:
        rows = _sheets_service().spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID, range=f"{STATS_TAB}!A:B"
        ).execute().get("values", [])
        for i, row in enumerate(rows):
            if row and row[0] == key:
                _sheets_service().spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEET_ID,
                    range=f"{STATS_TAB}!B{i+1}",
                    valueInputOption="RAW",
                    body={"values": [[str(value)]]}
                ).execute()
                return
        _sheets_service().spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID, range=f"{STATS_TAB}!A:B",
            valueInputOption="RAW", body={"values": [[key, str(value)]]}
        ).execute()
    except Exception:
        pass

def fmt_diff(current, prev_str):
    """前日比を「+12」「-5」形式で返す"""
    try:
        diff = int(current) - int(prev_str)
        return f"+{diff}" if diff >= 0 else str(diff)
    except Exception:
        return "?"

def get_today_store_visit():
    today = datetime.now(JST).strftime("%Y-%m-%d")
    for v in get_store_visits():
        if v[0] == today:
            memo = v[1] if len(v) > 1 else ""
            return f"📅 今日の予定：{memo}"
    return ""

# ── 週別タスク ─────────────────────────────────────────────────────
WEEKLY_TASKS = {
    0: ["📤 インスタ動画投稿（うしうらら）", "📖 ストーリー投稿", "📊 先週インスタ数値確認"],
    1: ["📖 ストーリー投稿", "🎬 動画編集（うしうらら or YouTube）"],
    2: ["📖 ストーリー投稿", "🎬 動画編集 or 🎵 音楽制作"],
    3: ["📷 インスタ画像投稿（うしうらら）", "📖 ストーリー投稿"],
    4: ["📖 ストーリー投稿", "🎬 動画編集 or 🎵 音楽制作"],
    5: ["📖 ストーリー投稿"],
    6: ["🎬 明日の動画を準備・編集（月曜投稿用）", "📖 ストーリー投稿"],
}

MONTHLY_TASKS = {
    1:  ["🗓️ 月初：先月の数値まとめ", "📋 今月の目標設定"],
    9:  ["🎵 明日Sunoクレジット2500にリセット！今日中に使い切ろう🍍"],
    15: ["📊 月半：インスタ数値確認"],
}

# 特別予定（3日前から毎朝リマインドが届く）
SPECIAL_EVENTS = [
    {"date": "2026-07-21", "name": "横浜撮影1日目"},
    {"date": "2026-07-22", "name": "横浜撮影2日目"},
    {"date": "2026-10-19", "name": "横浜撮影1日目"},
    {"date": "2026-10-20", "name": "横浜撮影2日目"},
    {"date": "2026-10-21", "name": "横浜撮影3日目"},
]

BEEF_FACTS = [
    "🥩 シャトーブリアンはヒレの中心部。600g以下であればシャトーブリアンと謳える希少部位で、うしうらら ではA5雌牛のみを使用しています。",
    "🥩 A5ランクの「5」は脂肪交雑・色沢・きめなど5項目すべてが最高評価。雌牛は脂のきめが細かく、より上品な甘みが出ます。",
    "🥩 シャトーブリアンの名前はフランスの外交官ヴィコント・ド・シャトーブリアンに由来。19世紀パリで生まれた格式ある調理法です。",
    "🥩 横浜・関内エリアでシャトーブリアンを看板コースにしているのは、うしうらら が数少ない存在。希少性を積極的に発信しましょう。",
    "🥩 ミディアムレアは内部温度55〜60℃。シャトーブリアンはこの焼き加減でジューシーさと旨みのピークが重なります。",
]
BEEF_FACT_IDX = [0]

# Suno残高（LINEコマンド「Suno 200」で更新）
suno_state = {"balance": None, "updated_at": None}

def get_suno_section():
    now = datetime.now(JST)
    if (now.day - 1) % 3 != 0:
        return ""
    if suno_state["balance"] is None:
        return "━━━ 🎵 Suno残高 ━━━\n  未設定　「Suno 200」みたいに送って！\n"
    bal = suno_state["balance"]
    updated = suno_state["updated_at"].strftime("%-m/%-d") if suno_state["updated_at"] else "?"
    reset_day = 10
    days_left = (reset_day - now.day) if now.day < reset_day else (reset_day + 30 - now.day)
    bar = "🟩" * min(10, int(bal / 250)) + "⬜" * (10 - min(10, int(bal / 250)))
    return f"━━━ 🎵 Suno残高 ━━━\n  {bar}\n  {bal}/2500クレジット（{updated}更新）\n  リセットまであと{days_left}日\n"

# ── 天気（wttr.in アカウント不要）────────────────────────────────
WEATHER_EMOJI = {
    113: ("☀️", "快晴"), 116: ("🌤️", "晴れ時々曇り"), 119: ("☁️", "曇り"), 122: ("☁️", "曇り"),
    143: ("🌫️", "霧"), 248: ("🌫️", "霧"), 260: ("🌫️", "霧"),
    200: ("⛈️", "雷雨"), 386: ("⛈️", "雷雨"), 389: ("⛈️", "雷雨"),
    227: ("❄️", "雪"), 230: ("❄️", "吹雪"), 335: ("❄️", "雪"), 338: ("❄️", "大雪"),
    371: ("❄️", "大雪"), 377: ("❄️", "みぞれ"),
}

def get_weather(location, city_name):
    try:
        url = f"https://wttr.in/{location}?format=j1"
        r = requests.get(url, timeout=5).json()
        current = r["current_condition"][0]
        temp_c  = current["temp_C"]
        feels_c = current["FeelsLikeC"]
        code    = int(current["weatherCode"])
        if 293 <= code <= 377 and code not in WEATHER_EMOJI:
            icon, jp = "🌧️", "雨"
        else:
            icon, jp = WEATHER_EMOJI.get(code, ("🌤️", ""))
        return f"{icon} {city_name}：{jp}　{temp_c}℃（体感{feels_c}℃）"
    except Exception:
        return f"⛅ {city_name} 天気取得失敗"

# ── Instagram ─────────────────────────────────────────────────────
def get_instagram_yesterday():
    if not IG_TOKEN or not IG_USER_ID:
        return "📱 Instagram データ未設定"
    try:
        # フォロワー数
        profile_url = f"https://graph.instagram.com/{IG_USER_ID}?fields=followers_count,media_count&access_token={IG_TOKEN}"
        profile = requests.get(profile_url, timeout=5).json()
        followers = profile.get("followers_count", "-")
        media_count = profile.get("media_count", "-")
        prev = get_stat("ig_followers")
        diff = f"（{fmt_diff(followers, prev)}）" if prev else ""
        set_stat("ig_followers", followers)
        return (f"📱 Instagram\n"
                f"  👥 フォロワー：{followers}人{diff}\n"
                f"  📸 総投稿数：{media_count}")
    except Exception as e:
        return f"📱 Instagram 取得失敗: {e}"

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

# ── YouTube ───────────────────────────────────────────────────────
def get_youtube_stats():
    if not YOUTUBE_API_KEY:
        return "🎬 YouTube APIキー未設定"
    try:
        lines = ["🎬 YouTube チャンネル"]
        for name, channel_id in YOUTUBE_CHANNELS.items():
            url = (f"https://www.googleapis.com/youtube/v3/channels"
                   f"?part=statistics&id={channel_id}&key={YOUTUBE_API_KEY}")
            r = requests.get(url, timeout=5).json()
            stats = r["items"][0]["statistics"]
            subs  = int(stats["subscriberCount"])
            views = int(stats["viewCount"])
            videos = int(stats["videoCount"])
            prev_subs = get_stat(f"yt_subs_{name}")
            diff = f"（{fmt_diff(subs, prev_subs)}）" if prev_subs else ""
            set_stat(f"yt_subs_{name}", subs)
            lines.append(f"  @{name}\n  👥 {subs:,}人{diff}  👁️ {views:,}回  📹 {videos}本")
        return "\n".join(lines)
    except Exception as e:
        return f"🎬 YouTube 取得失敗: {e}"

# ── GA4 ───────────────────────────────────────────────────────────
def get_ga4_yesterday():
    if not GA4_SERVICE_ACCOUNT_JSON:
        return "🌐 GA4 未設定"
    try:
        creds_dict = json.loads(GA4_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
        )
        client = BetaAnalyticsDataClient(credentials=creds)
        request_obj = RunReportRequest(
            property=f"properties/{GA4_PROPERTY_ID}",
            date_ranges=[
                DateRange(start_date="yesterday", end_date="yesterday"),
                DateRange(start_date="2daysAgo", end_date="2daysAgo"),
            ],
            metrics=[
                Metric(name="sessions"),
                Metric(name="activeUsers"),
                Metric(name="screenPageViews"),
            ],
        )
        response = client.run_report(request_obj)
        rows = {r.dimension_values[0].value: r.metric_values for r in response.rows} if response.rows else {}
        row = rows.get("date_range_0") or (response.rows[0].metric_values if response.rows else None)
        prev_row = rows.get("date_range_1")
        if not row:
            return "🌐 HP：昨日のデータなし"
        sessions  = row[0].value
        users     = row[1].value
        pageviews = row[2].value
        if prev_row:
            u_diff = fmt_diff(users, prev_row[1].value)
            s_diff = fmt_diff(sessions, prev_row[0].value)
            p_diff = fmt_diff(pageviews, prev_row[2].value)
        else:
            u_diff = s_diff = p_diff = ""
        return (f"🌐 ホームページ 昨日\n"
                f"  👤 ユーザー：{users}人{'（'+u_diff+'）' if u_diff else ''}\n"
                f"  🔄 セッション：{sessions}{'（'+s_diff+'）' if s_diff else ''}\n"
                f"  📄 ページビュー：{pageviews}{'（'+p_diff+'）' if p_diff else ''}")
    except Exception as e:
        return f"🌐 GA4 取得失敗: {e}"

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
    store_today = get_today_store_visit()
    if store_today:
        tasks = [store_today] + tasks
    task_text = "\n".join(f"  • {t}" for t in tasks)

    ig   = get_instagram_yesterday()
    ga4  = get_ga4_yesterday()
    yt   = get_youtube_stats()
    fact = BEEF_FACTS[BEEF_FACT_IDX[0] % len(BEEF_FACTS)]
    BEEF_FACT_IDX[0] += 1

    events = get_upcoming_events(3)
    event_section = f"\n━━━ 🍍 近日予定 ━━━\n{events}\n" if events else ""
    suno_section = get_suno_section()

    return (f"アロハ🤙 BOSS！\n{date_str}\n\n"
            f"{yokohama}\n{sodegaura}\n"
            f"{event_section}\n"
            f"━━━ 今日のタスク ━━━\n{task_text}\n\n"
            f"━━━ 昨日のインスタ ━━━\n{ig}\n\n"
            f"━━━ 昨日のHP ━━━\n{ga4}\n\n"
            f"━━━ YouTube ━━━\n{yt}\n\n"
            f"{suno_section}"
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
        t = text.lower()
        return any(t.startswith(k.lower()) or t == k.lower() for k in keywords)

    if match(["おはよう", "アロハ", "朝", "morning"]):
        reply = build_morning_message()
    elif match(["明日の予定", "明日", "tomorrow"]):
        reply = build_tomorrow_schedule()
    elif match(["天気", "weather"]):
        reply = get_weather("Yokohama", "横浜") + "\n" + get_weather("Sodegaura", "袖ヶ浦のぞみ野")
    elif match(["インスタ", "instagram", "IG"]):
        reply = get_instagram_yesterday()
    elif match(["HP", "ホームページ", "GA4", "サイト"]):
        reply = get_ga4_yesterday()
    elif match(["YouTube", "ユーチューブ", "youtube"]):
        reply = get_youtube_stats()
    elif match(["月報", "レポート", "report"]):
        reply = build_monthly_report()
    elif match(["タスク", "todo"]):
        tasks = WEEKLY_TASKS.get(now.weekday(), [])
        reply = "今日のタスク 🍍\n" + "\n".join(f"• {t}" for t in tasks)
    elif text.strip() == "今日":
        tasks = WEEKLY_TASKS.get(now.weekday(), []) + MONTHLY_TASKS.get(now.day, [])
        store = get_today_store_visit()
        if store:
            tasks = [store] + tasks
        task_text = "\n".join(f"• {t}" for t in tasks)
        reply = f"今日の予定 🏝️\n\n{task_text}"
    elif match(["秘書"]):
        secretary_mode["active"] = True
        reply = ("BOSS🍍何をしますか？👩‍💼\n\n"
                 "1️⃣ 予定を入れる\n"
                 "2️⃣ 予定を見る\n"
                 "3️⃣ メモを見る")
    elif secretary_mode["active"] and text.strip() in ["1", "１", "1️⃣"]:
        secretary_mode["active"] = False
        secretary_mode["entry"] = True
        reply = ("予定の入れ方はこちら👩‍💼\n\n"
                 "「月/日 内容」で送ってね🍍\n\n"
                 "例：\n"
                 "  4/25 撮影\n"
                 "  5/1 コンサルMTG\n"
                 "  5/10 歯医者\n\n"
                 "予定をどうぞ！")
    elif secretary_mode["entry"]:
        secretary_mode["entry"] = False
        parts = text.split()
        if len(parts) >= 2:
            date_str = _parse_visit_date(parts[0])
            memo = " ".join(parts[1:])
            if date_str:
                try:
                    add_store_visit(date_str, memo)
                    reply = f"📅 登録したよ！BOSS🍍\n{date_str} {memo}"
                except Exception as e:
                    reply = f"📅 登録失敗: {e}"
            else:
                reply = "📅 日付が読めなかった💦\n「4/25 撮影」の形式で送って！"
        else:
            reply = "📅 「4/25 撮影」の形式で送って！"
    elif secretary_mode["active"] and text.strip() in ["2", "２", "2️⃣", "2"]:
        secretary_mode["active"] = False
        visits = get_store_visits()
        if not visits:
            reply = "かしこまりました🍍\nまだ登録された予定はないよ！👩‍💼"
        else:
            lines = "\n".join(f"  {v[0]} {v[1] if len(v)>1 else ''}" for v in visits)
            reply = f"かしこまりました🍍こちらが予定です👩‍💼\n\n{lines}"
    elif secretary_mode["active"] and text.strip() in ["3", "３", "3️⃣"]:
        secretary_mode["active"] = False
        memos = get_memos()
        if not memos:
            reply = "メモですね🍍\nまだメモはないよ！👩‍💼"
        else:
            lines = "\n".join(f"  {i+1}. {m[0]} {m[1] if len(m)>1 else ''}" for i, m in enumerate(memos[-10:]))
            reply = f"メモですね🍍こちらです👩‍💼\n\n{lines}\n\n削除は「メモ削除 3」で！"
    elif match(["メモ確認"]):
        memos = get_memos()
        if not memos:
            reply = "📝 保存されたメモはないよ！"
        else:
            lines = "\n".join(f"  {i+1}. {m[0]} {m[1] if len(m)>1 else ''}" for i, m in enumerate(memos[-10:]))
            reply = f"📝 メモ一覧（最新10件）\n\n{lines}\n\n削除は「メモ削除 3」で！"
    elif any(text.lower().replace(" ", "").replace("　", "").startswith(k) for k in ["suno", "スーノ", "すーの", "すの", "スノ"]):
        normalized = text.lower().replace(" ", "").replace("　", "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        num_str = ""
        for k in ["suno", "スーノ", "すーの", "すの", "スノ"]:
            if normalized.startswith(k):
                num_str = normalized[len(k):]
                break
        if num_str.isdigit():
            suno_state["balance"] = int(num_str)
            suno_state["updated_at"] = datetime.now(JST)
            reply = f"🎵 Suno残高を {num_str}クレジットに更新したよ！3日おきに朝報告するね🍍"
        else:
            bal = suno_state["balance"]
            reply = f"🎵 Suno残高：{bal}クレジット" if bal is not None else "🎵 Suno残高未設定。「Suno 200」みたいに送って！"
    elif text.startswith("予定") and not match(["予定確認", "予定削除"]):
        parts = text.split()
        if len(parts) >= 3:
            date_str = _parse_visit_date(parts[1])
            memo = " ".join(parts[2:])
            if date_str:
                try:
                    add_store_visit(date_str, memo)
                    reply = f"📅 予定を登録したよ！\n{date_str} {memo}🍍"
                except Exception as e:
                    reply = f"📅 登録失敗: {e}"
            else:
                reply = "📅 日付の形式は「4/25」で送って！\n例：「予定 4/25 撮影」"
        else:
            reply = "📅 フォーマット：「予定 4/25 撮影」"
    elif match(["予定確認"]):
        visits = get_store_visits()
        if not visits:
            reply = "📅 登録済みの予定はないよ！"
        else:
            lines = "\n".join(f"  {i+1}. {v[0]} {v[1] if len(v)>1 else ''}" for i, v in enumerate(visits))
            reply = f"📅 登録済みの予定\n\n{lines}\n\n削除は「予定削除2」で！"
    elif text.startswith("予定削除"):
        num_str = text.replace("予定削除", "").strip()
        num_str = num_str.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        if num_str.isdigit():
            idx = int(num_str)
            try:
                success = delete_store_visit(idx)
                reply = f"📅 {idx}番の予定を削除したよ！🍍" if success else "📅 その番号の予定はないよ！"
            except Exception as e:
                reply = f"📅 削除失敗: {e}"
        else:
            reply = "📅 番号で指定してね！\n例：「予定削除2」"
    elif match(["ヘルプ", "help", "使い方"]):
        reply = ("📖 使い方 🤙\n"
                 "「アロハ」or「おはよう」→ 朝のまとめ\n"
                 "「明日の予定」→ 明日のスケジュール\n"
                 "「天気」→ 横浜・袖ヶ浦の天気\n"
                 "「インスタ」→ 昨日のInstagram\n"
                 "「タスク」→ 今日のToDoリスト\n"
                 "「月報」→ 今月のまとめ\n"
                 "「Suno 200」→ Suno残高を更新\n"
                 "「予定 4/25 撮影」→ 予定を登録\n"
                 "「予定確認」→ 登録済み一覧\n"
                 "「予定削除 4/25」→ 予定を削除")
    elif match(["メモ確認"]):
        memos = get_memos()
        if not memos:
            reply = "📝 保存されたメモはないよ！"
        else:
            lines = "\n".join(f"  {i+1}. {m[0]} {m[1] if len(m)>1 else ''}" for i, m in enumerate(memos[-10:]))
            reply = f"📝 メモ一覧（最新10件）\n\n{lines}\n\n削除は「メモ削除1」で！"
    elif text.startswith("メモ削除"):
        num_str = text.replace("メモ削除", "").strip()
        num_str = num_str.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        if num_str.isdigit():
            idx = int(num_str)
            try:
                success = delete_memo(idx)
                reply = f"📝 {idx}番のメモを削除したよ！🍍" if success else "📝 その番号のメモはないよ！"
            except Exception as e:
                reply = f"📝 削除失敗: {e}"
        else:
            reply = "📝 番号で指定してね！\n例：「メモ削除1」"
    else:
        try:
            add_memo(text)
            reply = f"📝 メモしたよ！BOSS🍍\n「{text}」\n\n確認は「メモ確認」で！"
        except Exception:
            reply = f"📝 メモしました！\n「{text}」🍍"

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
