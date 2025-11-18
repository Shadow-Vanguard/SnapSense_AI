"""
SnapMind - Local screenshot -> AI reminder -> desktop notification -> Telegram push
Run: python snapmind.py

Environment variables required:
- OPENAI_API_KEY        (OpenAI API key)
- TELEGRAM_BOT_TOKEN   (Telegram bot token)  [optional, to push to Telegram]
- TELEGRAM_CHAT_ID     (Telegram chat id to receive messages) [optional]
- SCREENSHOT_DIR       (Folder to monitor; defaults to ~/Screenshots or OS defaults)
- MIN_IMAGE_AGE_SEC    (optional, default 1) - ignore files younger than this to avoid partial writes
"""

import os
import sys
import time
import threading
import logging
import mimetypes
import hashlib
import subprocess
import uuid
from pathlib import Path
from datetime import datetime, timedelta

from dotenv import load_dotenv
import google.generativeai as genai
import requests
import dateparser
from dateutil import tz
from dateparser.search import search_dates

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Telegram bot (for button callbacks)
try:
    from telegram import Update
    from telegram.ext import Application, CallbackQueryHandler, ContextTypes
except Exception:
    Update = None
    Application = None
    CallbackQueryHandler = None
    ContextTypes = None

# Notification libraries with fallbacks
try:
    from plyer import notification as plyer_notify
except Exception:
    plyer_notify = None

# Optional Windows-specific
try:
    from win10toast import ToastNotifier
except Exception:
    ToastNotifier = None

# Optional macOS pync
try:
    import pync
except Exception:
    pync = None

# Load local .env if present (for dev convenience)
load_dotenv()

# ========== Configuration ==========
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR")
MIN_IMAGE_AGE_SEC = int(os.environ.get("MIN_IMAGE_AGE_SEC", "1"))
DUPLICATE_WINDOW_SEC = int(os.environ.get("DUPLICATE_WINDOW_SEC", "3600"))  # 1 hour default

if not GEMINI_API_KEY:
    print("ERROR: Set GEMINI_API_KEY environment variable.")
    sys.exit(1)

genai.configure(api_key=GEMINI_API_KEY)

# Defaults for screenshot dir
if not SCREENSHOT_DIR:
    # Common defaults
    home = str(Path.home())
    candidates = [
        os.path.join(home, "Screenshots"),
        os.path.join(home, "Pictures", "Screenshots"),
        os.path.join(home, "Desktop"),
        home
    ]
    for c in candidates:
        if os.path.isdir(c):
            SCREENSHOT_DIR = c
            break
    if not SCREENSHOT_DIR:
        SCREENSHOT_DIR = str(Path.home())

print(f"Monitoring folder: {SCREENSHOT_DIR}")

telegram_app = None  # will hold Application instance if available
recent_hashes = {}   # sha256 -> last_seen timestamp (epoch seconds)

# ================= Utilities =================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

def is_image_file(path: str) -> bool:
    mimet, _ = mimetypes.guess_type(path)
    if mimet and mimet.startswith("image"):
        return True
    lower = path.lower()
    return any(lower.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"])

def send_desktop_notification(title: str, message: str):
    # Try plyer
    try:
        if plyer_notify:
            plyer_notify.notify(title=title, message=message, timeout=8)
            return
    except Exception as e:
        logging.debug("plyer notify failed: %s", e)

    # Windows fallback
    try:
        if ToastNotifier:
            toaster = ToastNotifier()
            toaster.show_toast(title, message, duration=6)
            return
    except Exception as e:
        logging.debug("win10toast failed: %s", e)

    # macOS fallback
    try:
        if pync:
            pync.Notifier.notify(message, title=title)
            return
    except Exception as e:
        logging.debug("pync failed: %s", e)

    # Last resort: print
    print(f"[NOTIFICATION] {title} - {message}")


def push_telegram(text: str, add_reminder_buttons: bool = True):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.debug("Telegram not configured.")
        return False
    try:
        import json as _json
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}

        # Optional inline keyboard for reminder buttons
        if add_reminder_buttons:
            keyboard = [
                [
                    {"text": "🧪 In 10 sec", "callback_data": "REMIND_IN_10S"},
                ],
                [
                    {"text": "🔁 In 1 hour", "callback_data": "REMIND_IN_1H"},
                    {"text": "📅 Tomorrow", "callback_data": "REMIND_IN_1D"},
                    {"text": "🗓 In 1 week", "callback_data": "REMIND_IN_1W"},
                ],
            ]
            payload["reply_markup"] = _json.dumps({"inline_keyboard": keyboard})

        resp = requests.post(url, data=payload, timeout=10)
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return True
        else:
            logging.warning("Telegram push failed: %s %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        logging.exception("Telegram push exception: %s", e)
        return False

# ========== AI helpers (Gemini) ==========
def analyze_image_with_openai(image_path: str) -> dict:
    """
    Send image to Gemini multimodal model to extract reminder details directly.
    Returns dict with keys: ok(boolean), text, title, datetime (iso or None), category, notes
    """
    logging.info("Analyzing image: %s", image_path)

    # Step 1: read bytes
    with open(image_path, "rb") as f:
        img_bytes = f.read()

    # Prompt Gemini to both read the image and output structured JSON.
    system_prompt = """
You read an image (e.g., screenshot, note, ticket, bill, calendar, notification, chat) and extract reminder details.
Return a JSON object ONLY, no extra text, with keys:
- title: short, meaningful title for the reminder (string) – include event/topic + key qualifier if helpful (e.g. "AI Genesis Hackathon – Dubai")
- datetime: ISO-8601 datetime string in local time if an exact date/time is visible, else null
- relative_time_suggestion: string like "in 2 hours" or "tomorrow 9am" if helpful and no exact datetime, else null
- category: one of ["meeting","shopping","bill","travel","package","appointment","note","task","event","other"]
- items: list of only the MOST important pieces of information from the screenshot (max 5 items). Focus on:
  - unique codes or IDs
  - dates/times
  - prices/amounts
  - key short phrases (e.g., "Spotify Premium offer: 3 months for ₹99")
- links: list of HTTP/HTTPS URLs found in the screenshot text (e.g., "https://example.com"). If none, use [].
- confidence: number 0.0–1.0 for how confident you are
- notes: a concise, information-dense summary (1–2 sentences) that includes only the essentials:
  - what this is
  - any key number/price/date/time/location
  - any important constraint (e.g., "new users only") if explicitly stated
Rules:
- Do NOT make up dates or times. Only use what's clearly in the image text.
- Ignore generic or boilerplate text such as "Terms apply", "See details", long legal disclaimers, long privacy notices, etc.
- Prefer being concise over exhaustive: skip anything that is not useful to remember later.
- Output STRICT JSON only.
"""
    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(
            [
                system_prompt,
                {"mime_type": "image/png", "data": img_bytes},
            ],
            generation_config={"temperature": 0.0, "max_output_tokens": 400},
        )
        text_response = (response.text or "").strip()
    except Exception as e:
        logging.exception("Gemini vision call failed: %s", e)
        text_response = ""

    # Try to parse JSON from response
    import json
    out = {"ok": False, "raw_text": text_response, "title": None, "datetime": None, "relative_time_suggestion": None,
           "category": "other", "items": [], "links": [], "confidence": 0.0, "notes": ""}

    if text_response:
        try:
            # The model said "Return strictly JSON only", but sometimes it wraps in backticks; clean:
            cleaned = text_response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("` \n")
            # Find first { ... } substring
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                json_text = cleaned[start:end+1]
            else:
                json_text = cleaned
            data = json.loads(json_text)
            out.update({
                "ok": True,
                "title": data.get("title"),
                "datetime": data.get("datetime"),
                "relative_time_suggestion": data.get("relative_time_suggestion"),
                "category": data.get("category", "other"),
                "items": data.get("items", []),
                "links": data.get("links", []),
                "confidence": float(data.get("confidence", 0.0)),
                "notes": data.get("notes", "")
            })
        except Exception as e:
            logging.exception("Failed to parse JSON from model output: %s; output was: %s", e, text_response)
            out["notes"] = "Failed to parse structured JSON. Raw output: " + text_response[:500]

    return out

# ========== Reminder scheduling (very simple) ==========
def schedule_reminder_and_notify(info: dict, source_image: str):
    """
    info: dict from analyze_image_with_openai
    Show desktop notification now, and push Telegram message.
    If info['datetime'] is ISO, parse and schedule (simple sleep thread).
    """
    title = info.get("title") or "Snapshot Reminder"
    cat = (info.get("category") or "other").capitalize()
    items = info.get("items", [])
    links = info.get("links", []) or []
    conf = float(info.get("confidence", 0.0))
    notes = info.get("notes", "")
    dt_iso = info.get("datetime")
    rel = info.get("relative_time_suggestion")

    message_lines = []

    # Time information first
    if dt_iso:
        message_lines.append(f"Time: {dt_iso}")
    elif rel:
        message_lines.append(f"Time: {rel}")

    # Items as bullet list
    if items:
        if message_lines:
            message_lines.append("")  # blank line
        message_lines.append("Items:")
        for item in items:
            message_lines.append(f"• {item}")

    # Short notes / summary
    if notes:
        if message_lines:
            message_lines.append("")
        message_lines.append("Notes:")
        message_lines.append(notes)

    # Links, each on its own line
    if links:
        if message_lines:
            message_lines.append("")
        message_lines.append("Links:")
        for idx, url in enumerate(links, start=1):
            message_lines.append(f"{idx}. {url}")

    # Category / confidence
    if cat or conf:
        if message_lines:
            message_lines.append("")
        message_lines.append(f"Category: {cat} ")

    message = "\n".join(message_lines)

    # Immediate desktop notify
    send_desktop_notification(title, message)

    # Telegram push (short text) with reminder buttons
    tg_text = f"📌 <b>{title}</b>\n{message}\n\nSource: {os.path.basename(source_image)}"
    push_telegram(tg_text, add_reminder_buttons=True)

    # Calendar event hook (flight/meeting detection)
    maybe_create_calendar_event(info, source_image)

    # If there's a precise datetime in ISO, attempt to schedule another notification at that time
    if dt_iso:
        try:
            target = dateparser.parse(dt_iso)
            if target:
                now = datetime.now(tz=target.tzinfo or tz.tzlocal())
                delay = (target - now).total_seconds()
                if delay > 5:
                    logging.info("Scheduling follow-up notification in %.0f seconds at %s", delay, target.isoformat())
                    t = threading.Timer(delay, lambda: (send_desktop_notification(f"Reminder: {title}", message),
                                                       push_telegram(f"⏰ Reminder: <b>{title}</b>\n{message}")))
                    t.daemon = True
                    t.start()
                else:
                    logging.info("Target time is in the past or too soon; no scheduled follow-up.")
        except Exception as e:
            logging.debug("Failed to schedule follow-up: %s", e)


def detect_event_kind(info: dict) -> tuple[str | None, timedelta | None]:
    """
    Return ("flight" or "meeting", duration) if the reminder looks like a calendar-worthy event.
    """
    cat = (info.get("category") or "").lower()
    title = (info.get("title") or "").lower()
    notes = (info.get("notes") or "").lower()
    items_text = " ".join(info.get("items") or []).lower()
    blob = " ".join([cat, title, notes, items_text])

    flight_keywords = ["flight", "airline", "boarding", "departure", "gate", "terminal", "ticket", "airways", "seat"]
    meeting_keywords = ["meeting", "call", "conference", "appointment", "webinar", "event", "sync", "review", "standup"]

    if cat == "travel" or any(kw in blob for kw in flight_keywords):
        return "flight", timedelta(hours=2)
    if cat in {"meeting", "event", "appointment", "note", "task"} or any(kw in blob for kw in meeting_keywords):
        return "meeting", timedelta(hours=1)
    return None, None


def fallback_datetime_from_info(info: dict):
    """
    Attempt to extract a datetime from notes/items/raw text when the structured datetime is missing.
    """
    texts = []
    for key in ("notes", "raw_text"):
        val = info.get(key)
        if isinstance(val, str) and val.strip():
            texts.append(val)
    for item in info.get("items") or []:
        if isinstance(item, str):
            texts.append(item)

    if not texts:
        return None

    combined = " | ".join(texts)
    try:
        matches = search_dates(
            combined,
            settings={
                "RETURN_AS_TIMEZONE_AWARE": False,
                "PREFER_DAY_OF_MONTH": "first",
                "PREFER_DATES_FROM": "future",
            },
        )
    except Exception as e:
        logging.debug("search_dates failed: %s", e)
        return None

    if not matches:
        return None

    now = datetime.now()
    for text, dt in matches:
        if dt.year < 2000 or dt.year > 2100:
            continue
        # Ensure future-oriented by bumping year if necessary
        if dt < now:
            continue
        return dt
    return None


def extract_location_hint(info: dict) -> str:
    candidates = info.get("items") or []
    location_keywords = ["room", "hall", "office", "center", "centre", "arena", "airport", "terminal", "building", "street", "road"]
    for text in candidates:
        lower = text.lower()
        if any(kw in lower for kw in location_keywords):
            return text[:200]
    notes = info.get("notes") or ""
    for kw in location_keywords:
        idx = notes.lower().find(kw)
        if idx != -1:
            return notes[:200]
    return ""


def maybe_create_calendar_event(info: dict, source_image: str):
    event_kind, duration = detect_event_kind(info)
    if not event_kind:
        return

    dt_iso = info.get("datetime")
    if not dt_iso:
        logging.info("Event-like reminder detected but missing datetime; skipping calendar entry.")
        return

    target = dateparser.parse(dt_iso)
    if not target:
        logging.info("Unable to parse datetime for calendar entry: %s", dt_iso)
        return

    if not duration:
        duration = timedelta(hours=1)

    local_tz = tz.tzlocal()
    if target.tzinfo is None:
        target = target.replace(tzinfo=local_tz)

    # If the datetime came from a date-only string (no explicit time), default to 07:00.
    def _has_time_component(text: str) -> bool:
        lowered = text.lower()
        return (":" in lowered) or (" am" in lowered) or (" pm" in lowered) or ("t" in text)

    if isinstance(dt_iso, str) and not _has_time_component(dt_iso):
        target = target.replace(hour=7, minute=0, second=0, microsecond=0)
    elif target.hour == 0 and target.minute == 0 and isinstance(dt_iso, str) and not _has_time_component(dt_iso):
        target = target.replace(hour=7, minute=0, second=0, microsecond=0)

    dt_start_local = target.astimezone(local_tz)
    dt_end_local = dt_start_local + duration

    def fmt(dt):
        return dt.astimezone(tz.tzutc()).strftime("%Y%m%dT%H%M%SZ")

    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    uid = f"{uuid.uuid4().hex}@snapmind"
    summary = info.get("title") or ("Flight" if event_kind == "flight" else "Meeting")
    description_parts = []
    if info.get("notes"):
        description_parts.append(info["notes"])
    if info.get("items"):
        description_parts.append("Items: " + "; ".join(info["items"]))
    if info.get("links"):
        description_parts.append("Links: " + ", ".join(info["links"]))
    description_parts.append(f"Source image: {os.path.basename(source_image)}")
    description = "\\n".join(description_parts)
    location = extract_location_hint(info)

    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//SnapMind//EN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{dtstamp}
DTSTART:{fmt(dt_start_local)}
DTEND:{fmt(dt_end_local)}
SUMMARY:{summary}
DESCRIPTION:{description}
LOCATION:{location}
END:VEVENT
END:VCALENDAR
"""
    events_dir = Path("calendar_events")
    events_dir.mkdir(exist_ok=True)
    ics_path = events_dir / f"{uid}.ics"
    ics_path.write_text(ics, encoding="utf-8")

    logging.info("Calendar event (%s) saved to %s", event_kind, ics_path)

    try:
        if sys.platform.startswith("win"):
            os.startfile(str(ics_path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(ics_path)])
        else:
            subprocess.Popen(["xdg-open", str(ics_path)])
    except Exception as e:
        logging.debug("Failed to auto-open calendar event: %s", e)


# ========== Telegram button callbacks ==========
async def handle_reminder_button(update: "Update", context: "ContextTypes.DEFAULT_TYPE"):
    """
    Handle inline keyboard button clicks from Telegram.
    Schedules a new Telegram reminder based on which button was pressed.
    """
    if update is None or update.callback_query is None:
        return

    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if data == "REMIND_IN_10S":
        delay = 10
        label = "after 10 seconds"
    elif data == "REMIND_IN_1H":
        delay = 60 * 60
        label = "in 1 hour"
    elif data == "REMIND_IN_1D":
        delay = 60 * 60 * 24
        label = "tomorrow"
    elif data == "REMIND_IN_1W":
        delay = 60 * 60 * 24 * 7
        label = "in 1 week"
    else:
        return

    msg = query.message
    if not msg:
        return

    # Use the original message text as the reminder content
    reminder_text = msg.text_html or msg.text or ""

    def _send_followup():
        try:
            push_telegram(f"⏰ Reminder ({label}):\n\n{reminder_text}", add_reminder_buttons=True)
        except Exception as exc:
            logging.debug("Failed to send follow-up reminder: %s", exc)

    t = threading.Timer(delay, _send_followup)
    t.daemon = True
    t.start()


def start_telegram_listener():
    """
    Start a background Telegram Application to listen for button callbacks.
    """
    global telegram_app
    if not TELEGRAM_BOT_TOKEN or Application is None:
        return

    try:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        telegram_app.add_handler(CallbackQueryHandler(handle_reminder_button))

        def _run():
            telegram_app.run_polling(allowed_updates=["callback_query"])

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        logging.info("Telegram callback listener started.")
    except Exception as e:
        logging.warning("Failed to start Telegram listener: %s", e)

# ========== Watchdog handler ==========
class ImageHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self.cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self.cleanup_thread.start()

    def _cleanup_loop(self):
        """Periodically remove old hashes outside the duplicate window."""
        while True:
            cutoff = time.time() - DUPLICATE_WINDOW_SEC
            to_delete = [h for h, ts in list(recent_hashes.items()) if ts < cutoff]
            for h in to_delete:
                recent_hashes.pop(h, None)
            time.sleep(60)

    def on_created(self, event):
        if event.is_directory:
            return
        path = event.src_path
        if not is_image_file(path):
            return
        # Wait briefly to ensure file write complete
        try:
            time.sleep(MIN_IMAGE_AGE_SEC)
            # Check file size not zero
            if os.path.getsize(path) == 0:
                logging.debug("File size zero, skipping: %s", path)
                return

            # Compute hash to deduplicate
            with open(path, "rb") as f:
                data = f.read()
            sha = hashlib.sha256(data).hexdigest()
            now_ts = time.time()
            last_seen = recent_hashes.get(sha)
            if last_seen and (now_ts - last_seen) < DUPLICATE_WINDOW_SEC:
                logging.info("Skipping duplicate image (hash match within window): %s", path)
                return
            recent_hashes[sha] = now_ts

            logging.info("Detected image: %s", path)
            try:
                info = analyze_image_with_openai(path)
            except Exception as e:
                logging.exception("Analysis failed for %s: %s", path, e)
                return
            schedule_reminder_and_notify(info, path)
        except Exception as e:
            logging.exception("Error handling created file: %s", e)

# ========== Main ==========
def main():
    event_handler = ImageHandler()
    observer = Observer()
    observer.schedule(event_handler, path=SCREENSHOT_DIR, recursive=False)
    observer.start()

    # Start Telegram callback listener (for inline buttons) in background
    start_telegram_listener()
    try:
        logging.info("SnapMind running. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
