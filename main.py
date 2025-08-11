
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import asyncio
import os
from datetime import datetime, timedelta
import pytz
from functools import wraps
import json

# ===== Google Calendar =====
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ================= Settings =================
KST = pytz.timezone("Asia/Seoul")

# Calendar API
SCOPES = ["https://www.googleapis.com/auth/calendar"]
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")  # 권장: 별도 캘린더 ID

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_TOKEN")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "0000")

# Local files for bot meta
USER_ID_FILE = "user_ids.json"
ADMIN_FILE = "admins.json"

# ===== Utilities =====
def get_calendar_service():
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def load_user_ids():
    try:
        with open(USER_ID_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()

def save_user_ids(user_ids):
    with open(USER_ID_FILE, "w", encoding="utf-8") as f:
        json.dump(list(user_ids), f, ensure_ascii=False, indent=2)

def load_admins():
    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def save_admins(admins):
    with open(ADMIN_FILE, "w", encoding="utf-8") as f:
        json.dump(admins, f, ensure_ascii=False, indent=2)

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat_id = update.message.chat_id
        admins = load_admins()
        if not any(a["chat_id"] == chat_id for a in admins):
            await update.message.reply_text("❌ 관리 권한이 필요한 기능입니다.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def dow_kr(dt: datetime) -> str:
    return {"Mon":"월","Tue":"화","Wed":"수","Thu":"목","Fri":"금","Sat":"토","Sun":"일"}[dt.strftime("%a")]

def ampm_kr(dt: datetime) -> str:
    return "오전" if dt.strftime("%p") == "AM" else "오후"

def fmt_event_time(dt: datetime, now: datetime) -> str:
    # 연도가 같으면 MM/DD, 다르면 YY/MM/DD
    date_part = dt.strftime("%m/%d") if dt.year == now.year else dt.strftime("%y/%m/%d")
    return f"{date_part}({dow_kr(dt)}) {ampm_kr(dt)} {dt.strftime('%I:%M')}"

def parse_yyMMdd_HHmm(s_date: str, s_time: str) -> datetime:
    return KST.localize(datetime.strptime(f"{s_date} {s_time}", "%y%m%d %H%M"))

def is_muted(ev: dict) -> bool:
    priv = (ev.get("extendedProperties") or {}).get("private") or {}
    val = str(priv.get("mute", "")).strip().lower()
    return val in ("v", "true", "1", "✓", "✔")

def set_mute_on_body(existing_ev: dict, mute: bool) -> dict:
    priv = ((existing_ev.get("extendedProperties") or {}).get("private") or {}).copy()
    priv["mute"] = "v" if mute else ""
    return {"extendedProperties": {"private": priv}}

def get_event_start_dt(ev: dict) -> datetime | None:
    start = ev.get("start", {})
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"].replace("Z","+00:00"))
        return dt.astimezone(KST)
    elif "date" in start:
        dt = KST.localize(datetime.strptime(start["date"], "%Y-%m-%d"))
        return dt
    return None

def ensure_end(dt_start: datetime) -> datetime:
    # default 1 hour duration
    return dt_start + timedelta(hours=1)

# ===== Calendar data accessors =====
def list_upcoming(limit=300):
    service = get_calendar_service()
    now = datetime.now(KST).isoformat()
    res = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now,
        singleEvents=True,
        orderBy="startTime",
        maxResults=limit
    ).execute()
    return res.get("items", [])

def list_past(days=30, limit=500):
    service = get_calendar_service()
    now = datetime.now(KST)
    time_min = (now - timedelta(days=days)).isoformat()
    time_max = now.isoformat()
    res = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
        maxResults=limit
    ).execute()
    return res.get("items", [])

def create_event(dt_kst: datetime, summary: str, mute=False):
    service = get_calendar_service()
    body = {
        "summary": summary,
        "start": {"dateTime": dt_kst.isoformat(), "timeZone": "Asia/Seoul"},
        "end":   {"dateTime": ensure_end(dt_kst).isoformat(), "timeZone": "Asia/Seoul"},
        "extendedProperties": {"private": {"mute": "v" if mute else ""}},
    }
    return service.events().insert(calendarId=CALENDAR_ID, body=body).execute()

def patch_event(event_id: str, dt_kst=None, summary=None, mute=None):
    service = get_calendar_service()
    body = {}
    if dt_kst is not None:
        body["start"] = {"dateTime": dt_kst.isoformat(), "timeZone": "Asia/Seoul"}
        body["end"]   = {"dateTime": ensure_end(dt_kst).isoformat(), "timeZone": "Asia/Seoul"}
    if summary is not None:
        body["summary"] = summary
    if mute is not None:
        body["extendedProperties"] = {"private": {"mute": "v" if mute else ""}}
    return service.events().patch(calendarId=CALENDAR_ID, eventId=event_id, body=body).execute()

def delete_event(event_id: str):
    service = get_calendar_service()
    return service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()

# ========== Telegram Bot ==========
notified_schedules_hour = set()
notified_schedules_day = set()
notified_schedules_week = set()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if "user_ids" not in context.application.bot_data:
        context.application.bot_data["user_ids"] = load_user_ids()
    user_ids = context.application.bot_data["user_ids"]
    if chat_id not in user_ids:
        user_ids.add(chat_id)
        save_user_ids(user_ids)

    await update.message.reply_text(
        "안녕하세요! 전교조 경기지부 일정 알림 봇입니다.\n도움말을 보시려면 /help 를 입력하세요.\n\n🔔 [알림] 3시간 전, 하루 전, 일주일 전"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 **일정 알림 봇 사용법**\n\n"
        "1️⃣ **일정 목록 보기**\n"
        "`/list`\n"
        "등록된 모든 일정을 확인합니다.\n\n"
        "2️⃣ **지난 일정 보기**\n"
        "`/history`\n"
        "지난 30일 간의 일정을 확인합니다.\n\n"
        "`/history365`\n"
        "지난 1년 간의 일정을 확인합니다.\n\n"
        "🔔 **알림**\n"
        "3시간 전, 하루 전, 일주일 전 알림 발송\n\n"
        "=======================\n\n"
        "⚠️ 관리자 전용 기능입니다.\n\n"
        "3️⃣ **공지사항 보내기**\n"
        "`/noti 공지내용`\n"
        "봇 사용자에게 공지사항을 보냅니다.\n"
        "예) `/noti 오늘 오후 3시에 회의가 있습니다.`\n\n"
        "`/adminnoti 내용`\n"
        "등록된 관리자에게만 공지를 보냅니다.\n"
        "예) `/adminnoti 오늘 5시에 회의가 있습니다.`\n\n"
        "4️⃣ **일정 추가**\n"
        "`/add YYMMDD HHMM 내용`\n"
        "예) `/add 241225 0900 성탄절`\n\n"
        "5️⃣ **일정 수정**\n"
        "`/edit 번호 YYMMDD HHMM 내용`\n"
        "예) `/edit 3 241231 1800 송년회`\n\n"
        "6️⃣ **일정 삭제**\n"
        "`/del 번호`\n"
        "예) `/del 4`\n\n"
        "7️⃣ **알림 음소거**\n"
        "`/mute 번호`\n"
        "해당 일정의 알림을 음소거합니다.\n"
        "`/unmute 번호`\n"
        "해당 일정의 알림 음소거를 해제합니다.\n"
        "예) `/mute 4 (음소거 해제는 /unmute)`\n\n"
        "1️⃣0️⃣ **사용자 수 확인**\n"
        "`/user`\n"
        "등록된 사용자 수를 확인합니다. (관리자 전용)\n\n"
        "🔑 **관리자 설정 명령어**\n"
        "· 관리자 추가(개인)\n/admin → 비밀번호 입력 → 이름 입력\n"
        "· 관리자 추가(단톡)\n/adminroom 비밀번호 방이름\n"
        "· 명단 확인 : /adminlist, 삭제 : /admindel 번호\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ===== Admin auth & list =====
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    chat_type = update.message.chat.type
    admins = load_admins()
    if chat_type in ["group", "supergroup"]:
        await update.message.reply_text(
            "❌ 개인 채팅에서만 사용 가능한 명령어입니다.\n단톡방에서는 [/adminroom 비밀번호 방이름] 을 사용해 방 전체에 관리 권한을 부여하세요."
        )
        return
    if any(a["chat_id"] == chat_id for a in admins):
        await update.message.reply_text("✅ 이미 관리자로 등록된 계정입니다.")
        return
    context.user_data["admin_state"] = "awaiting_password"
    await update.message.reply_text("🔒 관리자 비밀번호를 입력하세요:")

async def adminroom_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    chat_type = update.message.chat.type
    args = context.args
    admins = load_admins()
    if chat_type not in ["group", "supergroup"]:
        await update.message.reply_text("❌ 이 명령어는 단톡방에서만 사용할 수 있습니다.")
        return
    if len(args) < 2:
        await update.message.reply_text("❌ 명령어 형식이 올바르지 않습니다.\n예) /adminroom 비밀번호 방이름")
        return
    password, room_name = args[0], " ".join(args[1:])
    if password != ADMIN_PASSWORD:
        await update.message.reply_text("❌ 비밀번호가 올바르지 않습니다.")
        return
    if any(a["chat_id"] == chat_id for a in admins):
        await update.message.reply_text("✅ 이미 단톡방에 관리 권한이 부여되어 있습니다.")
        return
    admins.append({"name": f"{room_name}(단톡방)", "chat_id": chat_id})
    save_admins(admins)
    await update.message.reply_text(f"✅ '{room_name}' 단톡방에 관리 권한을 부여하였습니다.")

async def handle_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = update.message.text.strip()
    admins = load_admins()
    if context.user_data.get("admin_state") == "awaiting_password":
        if text == ADMIN_PASSWORD:
            context.user_data["admin_state"] = "awaiting_name"
            await update.message.reply_text("✅ 비밀번호가 확인되었습니다. 이름을 입력해주세요:")
        else:
            context.user_data.pop("admin_state", None)
            await update.message.reply_text("❌ 비밀번호가 올바르지 않습니다.")
    elif context.user_data.get("admin_state") == "awaiting_name":
        context.user_data.pop("admin_state", None)
        admin_name = text
        admins.append({"name": admin_name, "chat_id": chat_id})
        save_admins(admins)
        await update.message.reply_text(f"✅ {admin_name}님이 관리자로 등록되었습니다.")
    else:
        await fallback_handler(update, context)

@admin_only
async def admin_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = load_admins()
    if not admins:
        await update.message.reply_text("❌ 등록된 관리자가 없습니다.")
        return
    resp = "📋 관리자 목록:\n" + "\n".join(f"{i}. {a['name']}" for i, a in enumerate(admins, 1))
    await update.message.reply_text(resp)

@admin_only
async def admin_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = load_admins()
    if not admins:
        await update.message.reply_text("❌ 삭제할 관리자가 없습니다.")
        return
    try:
        idx = int(context.args[0]) - 1
        if 0 <= idx < len(admins):
            deleted = admins.pop(idx)
            save_admins(admins)
            await update.message.reply_text(f"✅ {deleted['name']}님이 관리자에서 삭제되었습니다.")
        else:
            await update.message.reply_text("❌ 유효한 번호를 입력하세요.")
    except Exception:
        await update.message.reply_text("❌ 삭제할 번호를 올바르게 입력하세요.\n예) /admindel 1")

# ===== Schedules (Calendar-backed) =====
def _sorted_upcoming_items():
    events = list_upcoming()
    items = []
    for ev in events:
        dt = get_event_start_dt(ev)
        if not dt:
            continue
        items.append({
            "id": ev["id"],
            "dt": dt,
            "summary": ev.get("summary", "").strip(),
            "mute": is_muted(ev),
        })
    items.sort(key=lambda x: x["dt"])
    return items

@admin_only
async def add_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 3:
            await update.message.reply_text("❌ 형식: /add YYMMDD HHMM 내용")
            return
        dt = parse_yyMMdd_HHmm(args[0], args[1])
        summary = " ".join(args[2:]).strip()
        if dt < datetime.now(KST):
            await update.message.reply_text("❌ 과거의 일정은 추가할 수 없습니다.")
            return
        create_event(dt, summary, mute=False)
        formatted = fmt_event_time(dt, datetime.now(KST))
        await update.message.reply_text(f"✅ 새 일정이 추가되었습니다\n일정: {summary}\n일시: {formatted}")
    except Exception:
        await update.message.reply_text("❌ 일정을 추가할 수 없습니다. 올바른 형식인지 확인하세요.\n예) /add 241231 1500 새해맞이 준비")

@admin_only
async def edit_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 4:
            await update.message.reply_text("❌ 형식: /edit [번호] [YYMMDD HHMM] [내용]")
            return
        idx = int(args[0]) - 1
        dt = parse_yyMMdd_HHmm(args[1], args[2])
        summary = " ".join(args[3:]).strip()
        items = _sorted_upcoming_items()
        if 0 <= idx < len(items):
            target = items[idx]
            if dt < datetime.now(KST):
                await update.message.reply_text("❌ 과거의 일정으로 수정할 수 없습니다.")
                return
            patch_event(target["id"], dt_kst=dt, summary=summary, mute=target["mute"])
            formatted = fmt_event_time(dt, datetime.now(KST))
            await update.message.reply_text(f"✅ 일정이 수정되었습니다\n일정: {summary}\n일시: {formatted}")
        else:
            await update.message.reply_text("❌ 유효한 번호를 입력하세요.")
    except Exception:
        await update.message.reply_text("❌ 일정을 수정할 수 없습니다. 올바른 형식인지 확인하세요.\n예) /edit 3 241231 1500 새해맞이 준비")

async def list_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = _sorted_upcoming_items()
    if not items:
        await update.message.reply_text("❌ 일정이 없습니다.")
        return
    now = datetime.now(KST)
    msg = "📅 등록된 일정:\n"
    for i, s in enumerate(items, 1):
        formatted = fmt_event_time(s["dt"], now)
        mute_icon = "*" if s["mute"] else ""
        msg += f"{i}. {formatted} - {mute_icon}{s['summary']}\n"
    msg += "\n* : 알림이 울리지 않도록 설정된 일정"
    await update.message.reply_text(msg)

@admin_only
async def delete_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(context.args[0]) - 1
        items = _sorted_upcoming_items()
        if 0 <= idx < len(items):
            target = items[idx]
            delete_event(target["id"])
            formatted = fmt_event_time(target["dt"], datetime.now(KST))
            await update.message.reply_text(f"✅ 일정이 삭제되었습니다\n일정: {target['summary']}\n일시: {formatted}")
        else:
            await update.message.reply_text("❌ 유효한 번호를 입력하세요.\n예) /del 1")
    except Exception:
        await update.message.reply_text("❌ 일정 삭제 중 오류가 발생했습니다.")

async def view_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(KST)
    events = list_past(days=30)
    items = []
    for ev in events:
        dt = get_event_start_dt(ev)
        if dt and dt < now:
            items.append({"dt": dt, "summary": ev.get("summary","").strip()})
    if not items:
        await update.message.reply_text("🔍 지난 30일 간의 일정이 없습니다.")
        return
    items.sort(key=lambda x: x["dt"], reverse=False)
    resp = "📅 지난 30일 간의 일정:\n"
    for i, s in enumerate(items, 1):
        resp += f"{i}. {fmt_event_time(s['dt'], now)} - {s['summary']}\n"
    await update.message.reply_text(resp)

async def view_history_365(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(KST)
    events = list_past(days=365)
    items = []
    for ev in events:
        dt = get_event_start_dt(ev)
        if dt and dt < now:
            items.append({"dt": dt, "summary": ev.get("summary","").strip()})
    if not items:
        await update.message.reply_text("🔍 지난 1년 간의 일정이 없습니다.")
        return
    items.sort(key=lambda x: x["dt"], reverse=False)
    resp = "📅 지난 1년 간의 일정:\n"
    for i, s in enumerate(items, 1):
        resp += f"{i}. {fmt_event_time(s['dt'], now)} - {s['summary']}\n"
    await update.message.reply_text(resp)

@admin_only
async def mute_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(context.args[0]) - 1
        items = _sorted_upcoming_items()
        if 0 <= idx < len(items):
            target = items[idx]
            service = get_calendar_service()
            ev = service.events().get(calendarId=CALENDAR_ID, eventId=target["id"]).execute()
            body = set_mute_on_body(ev, True)
            service.events().patch(calendarId=CALENDAR_ID, eventId=target["id"], body=body).execute()
            await update.message.reply_text(f"✅ 일정이 음소거 처리되었습니다:\n{target['summary']}")
        else:
            await update.message.reply_text("❌ 유효한 번호를 입력하세요.")
    except Exception:
        await update.message.reply_text("❌ 음소거 처리 중 오류가 발생했습니다. 올바른 형식인지 확인하세요.\n예) /mute 4")

@admin_only
async def unmute_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(context.args[0]) - 1
        items = _sorted_upcoming_items()
        if 0 <= idx < len(items):
            target = items[idx]
            service = get_calendar_service()
            ev = service.events().get(calendarId=CALENDAR_ID, eventId=target["id"]).execute()
            body = set_mute_on_body(ev, False)
            service.events().patch(calendarId=CALENDAR_ID, eventId=target["id"], body=body).execute()
            await update.message.reply_text(f"✅ 일정이 음소거 해제되었습니다:\n{target['summary']}")
        else:
            await update.message.reply_text("❌ 유효한 번호를 입력하세요.")
    except Exception:
        await update.message.reply_text("❌ 음소거 처리 중 오류가 발생했습니다. 올바른 형식인지 확인하세요.\n예) /unmute 4")

# ===== Notices & counts =====
@admin_only
async def user_count_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_ids = context.application.bot_data.get("user_ids", set())
    await update.message.reply_text(f"👥 현재 등록된 사용자는 총 {len(user_ids)}명입니다.")

@admin_only
async def notice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text or ""
    if not message_text.strip() or message_text.strip() == "/noti":
        await update.message.reply_text("❌ 공지 내용을 입력하세요.\n예) /noti 오늘 오후 3시에 회의가 있습니다.")
        return
    notice_message = message_text[5:].strip()
    user_ids = context.application.bot_data.get("user_ids", set())
    if not user_ids:
        await update.message.reply_text("❌ 알림을 보낼 대상이 없습니다.")
        return
    failed = []
    for chat_id in list(user_ids):
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"📢 알림:\n\n{notice_message}")
        except Exception as e:
            failed.append(chat_id)
            user_ids.remove(chat_id)
            save_user_ids(user_ids)
    if failed:
        await update.message.reply_text(
            f"⚠️ 차단 등으로 {len(failed)}개 대상에 메시지 전송 실패. 목록에서 제거했습니다.\n"
            f"✅ 공지사항이 {len(user_ids)}명에게 전송되었습니다."
        )
    else:
        await update.message.reply_text(f"✅ 공지사항이 모든 사용자({len(user_ids)}명)에게 전송되었습니다.")

@admin_only
async def admin_notice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text or ""
    if not message_text.strip() or message_text.strip() == "/adminnoti":
        await update.message.reply_text("❌ 공지 내용을 입력하세요.\n예) /adminnoti 긴급 관리자 회의가 있습니다.")
        return
    notice_message = message_text[10:].strip()
    admins = load_admins()
    if not admins:
        await update.message.reply_text("❌ 등록된 관리자가 없습니다.")
        return
    failed = []
    for a in admins:
        try:
            await context.bot.send_message(chat_id=a["chat_id"], text=f"📢 관리자용 알림:\n\n{notice_message}")
        except Exception:
            failed.append(a["chat_id"])
    success = len(admins) - len(failed)
    if failed:
        await update.message.reply_text(f"⚠️ 일부 관리자에게 메시지 전송 실패 ({len(failed)}명). ✅ 전송: {success}명")
    else:
        await update.message.reply_text(f"✅ 공지사항이 모든 관리자({success}명)에게 전송되었습니다.")

# ===== Confirmation (/delall, /delhistory) =====
@admin_only
async def delall_confirm_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if f"confirm_action_{chat_id}" in context.application.bot_data:
        await update.message.reply_text("❌ 이전 확인 작업이 진행 중입니다.\n/ok 를 입력하거나 30초 후 다시 시도하세요.")
        return
    context.application.bot_data[f"confirm_action_{chat_id}"] = "delall"
    context.application.bot_data[f"confirm_task_{chat_id}"] = asyncio.create_task(confirm_timeout(chat_id, context))
    await update.message.reply_text(
        "⚠️ 캘린더의 '앞으로의 이벤트'를 모두 삭제하시겠습니까?\n이 작업은 되돌릴 수 없습니다.\n확인하려면 /ok 를 입력하세요.\n\n⏳ 30초 이내 미응답 시 취소됩니다."
    )

@admin_only
async def delhistory_confirm_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if f"confirm_action_{chat_id}" in context.application.bot_data:
        await update.message.reply_text("❌ 이전 확인 작업이 진행 중입니다.\n/ok 를 입력하거나 30초 후 다시 시도하세요.")
        return
    context.application.bot_data[f"confirm_action_{chat_id}"] = "delhistory"
    context.application.bot_data[f"confirm_task_{chat_id}"] = asyncio.create_task(confirm_timeout(chat_id, context))
    await update.message.reply_text(
        "⚠️ 지난 일정(최근 1년)을 모두 삭제하시겠습니까?\n이 작업은 되돌릴 수 없습니다.\n확인하려면 /ok 를 입력하세요.\n\n⏳ 30초 이내 미응답 시 취소됩니다."
    )

async def confirm_timeout(chat_id, context):
    await asyncio.sleep(30)
    if context.application.bot_data.get(f"confirm_action_{chat_id}"):
        context.application.bot_data.pop(f"confirm_action_{chat_id}", None)
        context.application.bot_data.pop(f"confirm_task_{chat_id}", None)
        await context.bot.send_message(chat_id=chat_id, text="❌ 시간이 초과되어 작업이 취소되었습니다.")

async def ok_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    action = context.application.bot_data.pop(f"confirm_action_{chat_id}", None)
    task = context.application.bot_data.pop(f"confirm_task_{chat_id}", None)
    if task:
        task.cancel()
    if action == "delall":
        events = _sorted_upcoming_items()
        count = 0
        for it in events:
            try:
                delete_event(it["id"])
                count += 1
            except HttpError:
                pass
        await update.message.reply_text(f"✅ 앞으로의 일정 {count}건을 삭제했습니다.")
    elif action == "delhistory":
        events = list_past(days=365)
        count = 0
        now = datetime.now(KST)
        for ev in events:
            dt = get_event_start_dt(ev)
            if dt and dt < now:
                try:
                    delete_event(ev["id"])
                    count += 1
                except HttpError:
                    pass
        await update.message.reply_text(f"✅ 지난 일정 {count}건을 삭제했습니다.")
    else:
        await update.message.reply_text("❌ 확인할 작업이 없습니다.")

# ===== Fallback =====
async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.message.chat.type
    if chat_type == "private":
        help_message = (
            "⚠️ 봇을 이용하려면 명령어를 입력해야 합니다.\n"
            "=======================\n\n"
            "🔔 **일정 알림 봇 사용법**\n\n"
            "1️⃣ **일정 목록 보기**\n"
            "`/list`\n"
            "등록된 모든 일정을 확인합니다.\n\n"
            "2️⃣ **지난 일정 보기**\n"
            "`/history`\n"
            "지난 30일 간의 일정을 확인합니다.\n\n"
            "📖 더 많은 기능은 /help를 참고하세요."
        )
        await update.message.reply_text(help_message, parse_mode="Markdown")
    elif chat_type in ["group", "supergroup"]:
        return

# ===== Schedulers =====
async def notify_schedules(application: Application):
    print("🔄 notify_schedules task started")
    while True:
        try:
            now = datetime.now(KST)
            user_ids = application.bot_data.get("user_ids", [])
            if not user_ids:
                await asyncio.sleep(60); continue

            items = _sorted_upcoming_items()
            for s in items:
                if s["mute"]:
                    continue
                event_time = s["dt"]
                description = s["summary"]
                diff = event_time - now

                uid_hour = f"{event_time.strftime('%y%m%d %H%M')}_{description}_hour"
                uid_day  = f"{event_time.strftime('%y%m%d %H%M')}_{description}_day"
                uid_week = f"{event_time.strftime('%y%m%d %H%M')}_{description}_week"

                formatted = fmt_event_time(event_time, now)

                # 3시간 전: 180 ± 1분 윈도우
                if timedelta(minutes=179) < diff <= timedelta(minutes=180):
                    if uid_hour not in notified_schedules_hour:
                        for chat_id in user_ids:
                            try:
                                await application.bot.send_message(chat_id=chat_id, text=f"🔔 [3시간 전 알림]\n일정: {description}\n시간: {formatted}")
                            except Exception as e:
                                print(f"❌ 3h notify failed: {chat_id}, {e}")
                        notified_schedules_hour.add(uid_hour)

                # 하루 전
                if timedelta(hours=23) < diff <= timedelta(days=1):
                    if uid_day not in notified_schedules_day:
                        for chat_id in user_ids:
                            try:
                                await application.bot.send_message(chat_id=chat_id, text=f"🔔 [하루 전 알림]\n일정: {description}\n시간: {formatted}")
                            except Exception as e:
                                print(f"❌ 1d notify failed: {chat_id}, {e}")
                        notified_schedules_day.add(uid_day)

                # 일주일 전
                if timedelta(days=6) < diff <= timedelta(days=7):
                    if uid_week not in notified_schedules_week:
                        for chat_id in user_ids:
                            try:
                                await application.bot.send_message(chat_id=chat_id, text=f"🔔 [일주일 전 알림]\n일정: {description}\n시간: {formatted}")
                            except Exception as e:
                                print(f"❌ 1w notify failed: {chat_id}, {e}")
                        notified_schedules_week.add(uid_week)

            await asyncio.sleep(60)
        except Exception as e:
            print(f"❌ notify_schedules loop error: {e}")
            await asyncio.sleep(60)

async def start_scheduler(application: Application):
    asyncio.create_task(notify_schedules(application))

async def shutdown(application: Application):
    print("🔄 종료 처리 중...")
    admins = load_admins()
    save_admins(admins)
    print("✅ 관리자 목록 저장 완료.")
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    print("✅ 태스크 종료 완료.")

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.bot_data["user_ids"] = load_user_ids()

    # 기본
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # 일정
    application.add_handler(CommandHandler("list", list_schedules))
    application.add_handler(CommandHandler("add", add_schedule))
    application.add_handler(CommandHandler("edit", edit_schedule))
    application.add_handler(CommandHandler("del", delete_schedule))
    application.add_handler(CommandHandler("history", view_history))
    application.add_handler(CommandHandler("history365", view_history_365))
    application.add_handler(CommandHandler("mute", mute_schedule))
    application.add_handler(CommandHandler("unmute", unmute_schedule))

    # 공지/관리
    application.add_handler(CommandHandler("user", user_count_command))
    application.add_handler(CommandHandler("noti", notice))
    application.add_handler(CommandHandler("adminnoti", admin_notice))

    application.add_handler(CommandHandler("delall", delall_confirm_prompt))
    application.add_handler(CommandHandler("delhistory", delhistory_confirm_prompt))
    application.add_handler(CommandHandler("ok", ok_handler))

    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("adminroom", adminroom_command))
    application.add_handler(CommandHandler("adminlist", admin_list_command))
    application.add_handler(CommandHandler("admindel", admin_delete_command))

    # 사용자 입력/기타
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_input))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_handler))

    application.post_init = start_scheduler

    try:
        application.run_polling()
    except KeyboardInterrupt:
        asyncio.run(shutdown(application))

if __name__ == "__main__":
    main()
