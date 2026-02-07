"""
Hypersecretary: Telegram bot + unified inbox.

Usage:
  Just type          → Gemini Flash (fast, cheap)
  /claude <message>  → Claude API (deep reasoning)
  /inbox             → Show recent items (all types)
  /inbox email       → Show only emails
  /inbox calendar    → Show only calendar events
  /ask <question>    → Ask a question about your inbox
  /search <keyword>  → Search inbox by keyword

Webhook endpoints:
  POST /webhook/email    → Cloudflare Email Worker
  POST /webhook/notify   → Generic (Zapier, scripts, anything)
  GET  /health           → Health check

All webhooks require X-Webhook-Secret header.
"""

import os
import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

import anthropic
from google import genai
from aiohttp import web, ClientSession

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me-to-something-random")

ALLOWED_USERS = [int(uid) for uid in os.environ.get("ALLOWED_USERS", "").split(",") if uid.strip()]

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8080"))

CONTEXT_DIR = Path(__file__).parent / "context"
DB_PATH = Path(__file__).parent / "data" / "inbox.db"
ACTIONS_FILE = Path(__file__).parent / "actions.json"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("hypersecretary")

# ---------------------------------------------------------------------------
# Notification type icons
# ---------------------------------------------------------------------------

ICONS = {
    "email":    "📧",
    "calendar": "📅",
    "alert":    "🚨",
    "task":     "✅",
    "payment":  "💰",
    "news":     "📰",
    "deploy":   "🚀",
    "reminder": "⏰",
    "bluesky":  "🦋",
    "mastodon": "🐘",
    "other":    "📌",
}

def icon_for(item_type: str) -> str:
    return ICONS.get(item_type, ICONS["other"])

# ---------------------------------------------------------------------------
# Database — unified inbox
# ---------------------------------------------------------------------------

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))

    # Check if we need to migrate from old email-only schema
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='inbox'")
    if not cursor.fetchone():
        conn.execute("""
            CREATE TABLE inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'other',
                source TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                read INTEGER DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_received ON inbox(received_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_type ON inbox(type)")

        # Migrate old emails table if it exists
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='emails'")
        if cursor.fetchone():
            conn.execute("""
                INSERT INTO inbox (received_at, type, source, title, body, metadata, read)
                SELECT received_at, 'email', sender, subject, body,
                       json_object('to', raw_to, 'message_id', message_id),
                       read
                FROM emails
            """)
            log.info("Migrated existing emails to unified inbox")

    conn.commit()
    conn.close()


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def store_item(item_type: str, source: str, title: str, body: str, metadata: dict = None):
    meta_json = json.dumps(metadata or {})
    with get_db() as db:
        db.execute(
            "INSERT INTO inbox (received_at, type, source, title, body, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), item_type, source, title, body, meta_json),
        )
    log.info(f"{icon_for(item_type)} Stored {item_type} from {source}: {title[:60]}")


def get_recent_items(limit: int = 10, item_type: str = None) -> list[dict]:
    with get_db() as db:
        if item_type:
            rows = db.execute(
                "SELECT * FROM inbox WHERE type = ? ORDER BY received_at DESC LIMIT ?",
                (item_type, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM inbox ORDER BY received_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def search_items(query: str, limit: int = 20) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM inbox WHERE title LIKE ? OR body LIKE ? OR source LIKE ? ORDER BY received_at DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", f"%{query}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_unread_count(item_type: str = None) -> int:
    with get_db() as db:
        if item_type:
            row = db.execute("SELECT COUNT(*) as c FROM inbox WHERE read = 0 AND type = ?", (item_type,)).fetchone()
        else:
            row = db.execute("SELECT COUNT(*) as c FROM inbox WHERE read = 0").fetchone()
    return row["c"]


def mark_all_read(item_type: str = None):
    with get_db() as db:
        if item_type:
            db.execute("UPDATE inbox SET read = 1 WHERE read = 0 AND type = ?", (item_type,))
        else:
            db.execute("UPDATE inbox SET read = 1 WHERE read = 0")


def get_item_type_counts() -> dict:
    with get_db() as db:
        rows = db.execute(
            "SELECT type, COUNT(*) as total, SUM(CASE WHEN read = 0 THEN 1 ELSE 0 END) as unread FROM inbox GROUP BY type"
        ).fetchall()
    return {r["type"]: {"total": r["total"], "unread": r["unread"]} for r in rows}

# ---------------------------------------------------------------------------
# System prompt & context loading
# ---------------------------------------------------------------------------

def load_system_prompt() -> str:
    parts = []
    prompt_file = Path(__file__).parent / "system_prompt.md"
    if prompt_file.exists():
        parts.append(prompt_file.read_text().strip())

    if CONTEXT_DIR.exists():
        for f in sorted(CONTEXT_DIR.glob("*.md")):
            parts.append(f"## {f.stem.replace('_', ' ').title()}\n\n{f.read_text().strip()}")

    return "\n\n---\n\n".join(parts) if parts else "You are a helpful personal assistant."


SYSTEM_PROMPT = load_system_prompt()

# ---------------------------------------------------------------------------
# Outbound actions (Zapier, IFTTT, etc.)
# ---------------------------------------------------------------------------

def load_actions() -> dict:
    """Load action definitions from actions.json.

    Format:
    {
      "action_name": {
        "url": "https://hooks.zapier.com/hooks/catch/...",
        "description": "What this action does",
        "method": "POST",           // optional, default POST
        "fields": ["field1"],       // optional, named args
        "headers": {"key": "val"},  // optional extra headers
        "body_template": {}         // optional, static JSON merged with args
      }
    }
    """
    if not ACTIONS_FILE.exists():
        return {}
    try:
        return json.loads(ACTIONS_FILE.read_text())
    except Exception as e:
        log.error(f"Failed to load actions.json: {e}")
        return {}


ACTIONS = load_actions()
log.info(f"Loaded {len(ACTIONS)} action(s)") if ACTIONS else None


def build_actions_prompt() -> str:
    """Generate the actions section for the system prompt."""
    if not ACTIONS:
        return ""

    lines = [
        "\n\n---\n\n## Available Actions\n",
        "You can trigger real-world actions by including action tags in your response.",
        "Use this format: [ACTION: action_name arg1 arg2 ...]",
        "You may include multiple actions in one response.",
        "Write your conversational response around the tags — the tags will be replaced with results before the user sees it.\n",
        "Actions available:\n",
    ]

    for name, action in sorted(ACTIONS.items()):
        desc = action.get("description", "no description")
        fields = action.get("fields", [])
        if fields:
            usage = f"[ACTION: {name} {'<' + '> <'.join(fields) + '>'}]"
        else:
            usage = f"[ACTION: {name}]"
        lines.append(f"- {name}: {desc}")
        lines.append(f"  Usage: {usage}")

    lines.append("\nExamples:")
    lines.append('User: "Turn off the lights" → [ACTION: lights_off] Done, lights are off.')
    lines.append('User: "Post hello world to mastodon" → [ACTION: toot hello world] Posted to Mastodon for you.')
    lines.append('User: "Log my mood as 8, feeling great" → [ACTION: log_mood 8 feeling great] Logged your mood.')
    lines.append("\nOnly trigger actions when the user clearly intends it. Don't trigger actions for questions about actions.")
    lines.append("If an action fails, tell the user what happened.")

    return "\n".join(lines)


# Append actions to system prompt
SYSTEM_PROMPT_WITH_ACTIONS = SYSTEM_PROMPT + build_actions_prompt()

# Pattern to match action tags in model responses
ACTION_PATTERN = re.compile(r'\[ACTION:\s*(\S+)\s*(.*?)\]')


async def process_actions_in_response(text: str) -> str:
    """Find [ACTION: name args] tags in model output, execute them, replace with results."""
    matches = list(ACTION_PATTERN.finditer(text))
    if not matches:
        return text

    result = text
    for match in reversed(matches):  # reverse so replacements don't shift indices
        action_name = match.group(1).lower()
        action_args = match.group(2).strip()

        log.info(f"  🔧 Executing action: {action_name} {action_args[:80]}")
        action_result = await execute_action(action_name, action_args)
        result = result[:match.start()] + action_result + result[match.end():]

    return result


async def execute_action(name: str, args: str) -> str:
    """Fire an outbound webhook for a named action. Returns status message."""
    action = ACTIONS.get(name)
    if not action:
        available = ", ".join(sorted(ACTIONS.keys())) or "(none configured)"
        return f"❌ Unknown action '{name}'.\nAvailable: {available}"

    url = action["url"]
    method = action.get("method", "POST").upper()
    extra_headers = action.get("headers", {})
    fields = action.get("fields", [])
    body_template = action.get("body_template", {})

    # Build the payload
    payload = dict(body_template)

    if fields and args:
        # Split args by spaces, matching to field names
        parts = args.split(None, len(fields) - 1) if len(fields) > 1 else [args]
        for i, field_name in enumerate(fields):
            if i < len(parts):
                payload[field_name] = parts[i]
    elif args:
        # No named fields — send everything as "value"
        payload["value"] = args

    # For IFTTT Maker webhooks, map to value1/value2/value3
    if "maker.ifttt.com" in url and not fields:
        parts = args.split(None, 2) if args else []
        for i, part in enumerate(parts):
            payload[f"value{i+1}"] = part

    headers = {"Content-Type": "application/json"}
    headers.update(extra_headers)

    try:
        async with ClientSession() as session:
            async with session.request(method, url, json=payload, headers=headers, timeout=15) as resp:
                status = resp.status
                if 200 <= status < 300:
                    return f"✅ {action.get('description', name)} — done"
                else:
                    body = await resp.text()
                    return f"⚠️ {name} returned {status}: {body[:200]}"
    except Exception as e:
        return f"❌ {name} failed: {e}"

# ---------------------------------------------------------------------------
# API clients
# ---------------------------------------------------------------------------

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
gemini_client = genai.Client(api_key=GOOGLE_API_KEY)

# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

MAX_HISTORY = 20
history: dict[int, list[dict]] = {}


def get_history(user_id: int) -> list[dict]:
    return history.setdefault(user_id, [])


def append_history(user_id: int, role: str, text: str):
    h = get_history(user_id)
    h.append({"role": role, "content": text})
    if len(h) > MAX_HISTORY:
        history[user_id] = h[-MAX_HISTORY:]


def clear_history(user_id: int):
    history.pop(user_id, None)

# ---------------------------------------------------------------------------
# Model calls
# ---------------------------------------------------------------------------

async def call_claude(user_message: str, user_id: int, safe: bool = False) -> str:
    """Call Claude. If safe=True, actions are disabled (for untrusted content like inbox)."""
    messages = get_history(user_id) + [{"role": "user", "content": user_message}]
    prompt = SYSTEM_PROMPT if safe else SYSTEM_PROMPT_WITH_ACTIONS
    try:
        response = await asyncio.to_thread(
            claude_client.messages.create,
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=prompt,
            messages=messages,
        )
        reply = response.content[0].text
        if not safe:
            reply = await process_actions_in_response(reply)
        append_history(user_id, "user", user_message)
        append_history(user_id, "assistant", reply)
        return reply
    except Exception as e:
        log.error(f"Claude error: {e}")
        return f"⚠️ Claude error: {e}"


async def call_gemini(user_message: str, user_id: int, safe: bool = False) -> str:
    """Call Gemini Flash. If safe=True, actions are disabled (for untrusted content like inbox)."""
    h = get_history(user_id)
    contents = []
    for msg in h:
        gemini_role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": gemini_role, "parts": [{"text": msg["content"]}]})
    contents.append({"role": "user", "parts": [{"text": user_message}]})

    prompt = SYSTEM_PROMPT if safe else SYSTEM_PROMPT_WITH_ACTIONS
    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=contents,
            config={
                "system_instruction": prompt,
                "max_output_tokens": 4096,
            },
        )
        reply = response.text
        if not safe:
            reply = await process_actions_in_response(reply)
        append_history(user_id, "user", user_message)
        append_history(user_id, "assistant", reply)
        return reply
    except Exception as e:
        log.error(f"Gemini error: {e}")
        return f"⚠️ Gemini error: {e}"

# ---------------------------------------------------------------------------
# Telegram send helper
# ---------------------------------------------------------------------------

async def send_reply(message, text: str, prefix: str = ""):
    full = prefix + text
    if len(full) <= 4096:
        await message.reply_text(full)
    else:
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for i, chunk in enumerate(chunks):
            p = prefix if i == 0 else ""
            await message.reply_text(p + chunk)

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_date(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d %b %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16]


def format_item_line(item: dict) -> str:
    icon = icon_for(item["type"])
    marker = "🔵" if not item["read"] else " "
    date = format_date(item["received_at"])
    source = item["source"].split("@")[0][:20] if "@" in item["source"] else item["source"][:20]
    title = item["title"][:50]
    return f"{marker}{icon} {date} | {source}\n   {title}"

# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

telegram_app: Application = None


def is_authorised(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    return update.effective_user.id in ALLOWED_USERS


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text.lower().startswith("/claude"):
        message = text[7:].strip()
        if not message:
            await update.message.reply_text("Send /claude followed by your message.")
            return
        log.info(f"[{user_id}] → Claude: {message[:80]}...")
        await update.message.chat.send_action("typing")
        reply = await call_claude(message, user_id)
        await send_reply(update.message, reply, "🟠 ")
    else:
        if text.lower().startswith("/flash"):
            message = text[6:].strip()
            if not message:
                await update.message.reply_text("Send /flash followed by your message, or just type normally.")
                return
        else:
            message = text
        log.info(f"[{user_id}] → Flash: {message[:80]}...")
        await update.message.chat.send_action("typing")
        reply = await call_gemini(message, user_id)
        await send_reply(update.message, reply, "⚡ ")


async def cmd_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent inbox items. Optional type filter: /inbox email, /inbox calendar"""
    if not is_authorised(update):
        return

    args = update.message.text.split(maxsplit=1)
    item_type = args[1].strip().lower() if len(args) > 1 else None

    # Validate type filter
    if item_type and item_type not in ICONS:
        types = ", ".join(sorted(ICONS.keys()))
        await update.message.reply_text(f"Unknown type '{item_type}'.\nAvailable: {types}")
        return

    items = get_recent_items(10, item_type)
    if not items:
        label = f" {item_type}" if item_type else ""
        await update.message.reply_text(f"📭 No{label} items yet.")
        return

    unread = get_unread_count(item_type)
    counts = get_item_type_counts()

    if item_type:
        header = f"{icon_for(item_type)} {item_type.title()} ({unread} unread)"
    else:
        summary_parts = [f"{icon_for(t)} {c['unread']}" for t, c in sorted(counts.items()) if c["unread"] > 0]
        summary = " ".join(summary_parts) if summary_parts else "all read"
        header = f"📬 Inbox ({summary})"

    lines = [header, ""]
    for item in items:
        lines.append(format_item_line(item))

    mark_all_read(item_type)
    await update.message.reply_text("\n".join(lines))


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search inbox by keyword. Usage: /search <keyword>"""
    if not is_authorised(update):
        return

    query = update.message.text.split(maxsplit=1)
    if len(query) < 2 or not query[1].strip():
        await update.message.reply_text("Usage: /search <keyword>\n\nExample: /search OpenTable")
        return

    keyword = query[1].strip()
    items = search_items(keyword, 10)

    if not items:
        await update.message.reply_text(f"Nothing found for '{keyword}'.")
        return

    lines = [f"🔍 Found {len(items)} item(s) for '{keyword}':\n"]
    for item in items:
        lines.append(format_item_line(item))
        # Show a snippet of the body for search results
        snippet = item["body"][:120].replace("\n", " ")
        if snippet:
            lines.append(f"   {snippet}...")
        lines.append("")

    await send_reply(update.message, "\n".join(lines))


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask a question about your inbox. Uses Flash to answer. Usage: /ask <question>"""
    if not is_authorised(update):
        return

    query = update.message.text.split(maxsplit=1)
    if len(query) < 2 or not query[1].strip():
        await update.message.reply_text(
            "Usage: /ask <question>\n\n"
            "Examples:\n"
            "/ask what reservations do I have this week?\n"
            "/ask summarise today's notifications\n"
            "/ask any payment confirmations recently?"
        )
        return

    question = query[1].strip()
    all_recent = get_recent_items(30)

    if not all_recent:
        await update.message.reply_text("📭 Nothing in your inbox to search.")
        return

    inbox_context = "\n\n---\n\n".join(
        f"Type: {item['type']}\nFrom: {item['source']}\n"
        f"Date: {item['received_at']}\nTitle: {item['title']}\n\n{item['body'][:2000]}"
        for item in all_recent
    )

    prompt = (
        f"Based on the following items from my inbox, answer this question: {question}\n\n"
        f"Be concise and direct. If the answer isn't in the inbox, say so.\n\n"
        f"---\n\nINBOX ITEMS:\n{inbox_context}"
    )

    await update.message.chat.send_action("typing")
    reply = await call_gemini(prompt, update.effective_user.id, safe=True)
    await send_reply(update.message, reply, "🔍 ")


async def cmd_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger an outbound action. Usage: /do <action_name> [args]"""
    if not is_authorised(update):
        return

    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 2:
        if not ACTIONS:
            await update.message.reply_text("No actions configured. Create an actions.json file.")
            return
        # Show available actions
        lines = ["Usage: /do <action> [args]\n\nAvailable actions:"]
        for name, action in sorted(ACTIONS.items()):
            desc = action.get("description", "")
            fields = action.get("fields", [])
            field_str = " ".join(f"<{f}>" for f in fields) if fields else "[text]"
            lines.append(f"  ⚡ /do {name} {field_str}")
            if desc:
                lines.append(f"     {desc}")
        await update.message.reply_text("\n".join(lines))
        return

    action_name = parts[1].lower()
    args = parts[2] if len(parts) > 2 else ""

    log.info(f"[{update.effective_user.id}] → Action: {action_name} {args[:80]}")
    await update.message.chat.send_action("typing")
    result = await execute_action(action_name, args)
    await update.message.reply_text(result)


async def cmd_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all available outbound actions."""
    if not is_authorised(update):
        return

    if not ACTIONS:
        await update.message.reply_text(
            "No actions configured.\n\n"
            "Create an actions.json file with your webhook URLs. See README for examples."
        )
        return

    lines = ["⚡ Available actions:\n"]
    for name, action in sorted(ACTIONS.items()):
        desc = action.get("description", "no description")
        fields = action.get("fields", [])
        field_str = " ".join(f"<{f}>" for f in fields) if fields else ""
        lines.append(f"  /do {name} {field_str}")
        lines.append(f"  └ {desc}\n")

    await update.message.reply_text("\n".join(lines))


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    clear_history(update.effective_user.id)
    await update.message.reply_text("🧹 History cleared.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    user_id = update.effective_user.id
    h = get_history(user_id)
    ctx_files = list(CONTEXT_DIR.glob("*.md")) if CONTEXT_DIR.exists() else []
    counts = get_item_type_counts()

    inbox_lines = []
    total_all = 0
    unread_all = 0
    for t in sorted(counts.keys()):
        c = counts[t]
        total_all += c["total"]
        unread_all += c["unread"]
        inbox_lines.append(f"  {icon_for(t)} {t}: {c['total']} total, {c['unread']} unread")

    inbox_summary = "\n".join(inbox_lines) if inbox_lines else "  (empty)"

    await update.message.reply_text(
        f"🤖 Hypersecretary online\n"
        f"Flash: {GEMINI_MODEL}\n"
        f"Claude: {CLAUDE_MODEL}\n"
        f"History: {len(h)} messages\n"
        f"Context files: {len(ctx_files)}\n"
        f"Inbox: {total_all} total, {unread_all} unread\n{inbox_summary}"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    types_list = ", ".join(sorted(ICONS.keys()))
    await update.message.reply_text(
        "📋 Hypersecretary\n\n"
        "Just type → Gemini Flash ⚡\n"
        "/claude <msg> → Claude 🟠\n\n"
        "Inbox:\n"
        "/inbox → All recent items\n"
        f"/inbox <type> → Filter ({types_list})\n"
        "/search <keyword> → Search inbox\n"
        "/ask <question> → Ask about your inbox\n\n"
        "Actions:\n"
        "/do <action> [args] → Trigger an action\n"
        "/actions → List available actions\n\n"
        "Other:\n"
        "/clear → Reset conversation history\n"
        "/status → Bot info\n"
        "/help → This message"
    )

# ---------------------------------------------------------------------------
# Webhook HTTP server
# ---------------------------------------------------------------------------

def verify_webhook(request: web.Request) -> bool:
    return request.headers.get("X-Webhook-Secret", "") == WEBHOOK_SECRET


async def notify_telegram(text: str):
    """Send a notification to all allowed Telegram users."""
    if not ALLOWED_USERS or not telegram_app:
        return
    for uid in ALLOWED_USERS:
        try:
            await telegram_app.bot.send_message(chat_id=uid, text=text)
        except Exception as e:
            log.error(f"Failed to notify {uid}: {e}")


async def handle_email_webhook(request: web.Request) -> web.Response:
    """Receive email from Cloudflare Email Worker.

    Expected JSON:
    {
      "from": "sender@example.com",
      "to": "hypersecretary@markwatson.ai",
      "subject": "Your reservation",
      "body": "plain text content",
      "message_id": "optional"
    }
    """
    if not verify_webhook(request):
        return web.Response(status=401, text="Unauthorized")

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="Invalid JSON")

    sender = data.get("from", "unknown")
    subject = data.get("subject", "(no subject)")
    body = data.get("body", "")
    to = data.get("to", "")
    message_id = data.get("message_id", "")

    store_item("email", sender, subject, body, {"to": to, "message_id": message_id})
    await notify_telegram(f"📧 New email\nFrom: {sender}\nSubject: {subject}")

    return web.Response(status=200, text="OK")


async def handle_notify_webhook(request: web.Request) -> web.Response:
    """Generic notification webhook — use from Zapier, scripts, etc.

    Expected JSON:
    {
      "type": "calendar|alert|task|payment|news|deploy|reminder|other",
      "source": "Google Calendar",
      "title": "Meeting with Board at 2pm",
      "body": "Optional longer description",
      "metadata": { ... optional extra fields ... },
      "notify": true
    }

    Only "title" is required. Everything else has sensible defaults.

    Zapier setup:
      Action: Webhooks by Zapier → POST
      URL: https://hypersecretary.fly.dev/webhook/notify
      Headers: X-Webhook-Secret = your-secret
      Body: JSON with the fields above
    """
    if not verify_webhook(request):
        return web.Response(status=401, text="Unauthorized")

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="Invalid JSON")

    title = data.get("title", "")
    if not title:
        return web.Response(status=400, text="Missing required field: title")

    item_type = data.get("type", "other")
    if item_type not in ICONS:
        item_type = "other"

    source = data.get("source", "webhook")
    body = data.get("body", "")
    metadata = data.get("metadata", {})
    should_notify = data.get("notify", True)

    store_item(item_type, source, title, body, metadata)

    if should_notify:
        icon = icon_for(item_type)
        notification = f"{icon} {item_type.title()}\nFrom: {source}\n{title}"
        await notify_telegram(notification)

    return web.Response(status=200, text="OK")


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(status=200, text="OK")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    global telegram_app

    init_db()

    # Telegram bot
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

    telegram_app.add_handler(CommandHandler("clear", cmd_clear))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("help", cmd_help))
    telegram_app.add_handler(CommandHandler("start", cmd_help))
    telegram_app.add_handler(CommandHandler("inbox", cmd_inbox))
    telegram_app.add_handler(CommandHandler("search", cmd_search))
    telegram_app.add_handler(CommandHandler("ask", cmd_ask))
    telegram_app.add_handler(CommandHandler("do", cmd_do))
    telegram_app.add_handler(CommandHandler("actions", cmd_actions))
    telegram_app.add_handler(CommandHandler("claude", handle_message))
    telegram_app.add_handler(CommandHandler("flash", handle_message))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # HTTP webhook server
    http_app = web.Application()
    http_app.router.add_post("/webhook/email", handle_email_webhook)
    http_app.router.add_post("/webhook/notify", handle_notify_webhook)
    http_app.router.add_get("/health", handle_health)

    runner = web.AppRunner(http_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)

    log.info(f"Starting webhook server on port {WEBHOOK_PORT}")
    await site.start()

    log.info("Starting Telegram polling")
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()

    log.info("🤖 Hypersecretary online")

    try:
        await asyncio.Event().wait()
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
