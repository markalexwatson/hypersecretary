#!/usr/bin/env python3
"""
Poll Mastodon and Bluesky for new notifications and forward them
to the Hypersecretary bot via its webhook endpoint.

Run standalone:  python social_poller.py
Run on a cron:   GitHub Actions every 5 minutes (see .github/workflows/social_poll.yml)

Required env vars:
  WEBHOOK_URL        - e.g. https://hypersecretary.fly.dev
  WEBHOOK_SECRET     - shared secret for the bot webhook

  # Mastodon (optional — skip to disable)
  MASTODON_INSTANCE  - e.g. https://mastodon.social
  MASTODON_TOKEN     - access token (Preferences → Development → New Application → read:notifications)

  # Bluesky (optional — skip to disable)
  BLUESKY_HANDLE     - e.g. yourname.bsky.social
  BLUESKY_PASSWORD   - app password (Settings → App Passwords → create one)
"""

import os
import sys
import json
import re
import logging
from pathlib import Path
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

MASTODON_INSTANCE = os.getenv("MASTODON_INSTANCE", "")
MASTODON_TOKEN    = os.getenv("MASTODON_TOKEN", "")

BLUESKY_HANDLE   = os.getenv("BLUESKY_HANDLE", "")
BLUESKY_PASSWORD = os.getenv("BLUESKY_PASSWORD", "")

# Where we persist "last seen" IDs between runs
STATE_FILE = Path(os.getenv("STATE_FILE", "data/social_poller_state.json"))

# ── State persistence ─────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Webhook helper ────────────────────────────────────────────────────
def send_to_bot(notif_type: str, source: str, title: str, body: str = "", metadata: dict = None):
    """POST a notification to the bot's /webhook/notify endpoint."""
    payload = {
        "type": notif_type,
        "source": source,
        "title": title,
        "body": body,
        "notify": True,
    }
    if metadata:
        payload["metadata"] = metadata

    try:
        r = requests.post(
            f"{WEBHOOK_URL}/webhook/notify",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": WEBHOOK_SECRET,
            },
            timeout=10,
        )
        if r.status_code == 200:
            log.info(f"  → sent to bot: {title[:80]}")
        else:
            log.warning(f"  → bot returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"  → failed to send to bot: {e}")

# ── HTML stripping ────────────────────────────────────────────────────
def strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ── Mastodon poller ───────────────────────────────────────────────────
def poll_mastodon(state: dict) -> dict:
    """Fetch new Mastodon notifications and forward to the bot."""
    if not MASTODON_INSTANCE or not MASTODON_TOKEN:
        log.info("Mastodon: skipped (not configured)")
        return state

    log.info(f"Mastodon: polling {MASTODON_INSTANCE}")
    last_id = state.get("mastodon_last_id")

    params = {"limit": 30}
    if last_id:
        params["since_id"] = last_id

    try:
        r = requests.get(
            f"{MASTODON_INSTANCE}/api/v1/notifications",
            headers={"Authorization": f"Bearer {MASTODON_TOKEN}"},
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        notifications = r.json()
    except Exception as e:
        log.error(f"Mastodon: API error: {e}")
        return state

    if not notifications:
        log.info("Mastodon: no new notifications")
        return state

    log.info(f"Mastodon: {len(notifications)} new notification(s)")

    # Process oldest first
    for n in reversed(notifications):
        ntype = n.get("type", "unknown")
        account = n.get("account", {})
        display = account.get("display_name") or account.get("acct", "someone")
        acct = account.get("acct", "")
        status = n.get("status", {})
        content = strip_html(status.get("content", "")) if status else ""
        status_url = status.get("url", "") if status else ""

        type_labels = {
            "mention":        f"🐘 {display} mentioned you",
            "reblog":         f"🐘 {display} boosted your post",
            "favourite":      f"🐘 {display} favourited your post",
            "follow":         f"🐘 {display} followed you",
            "follow_request": f"🐘 {display} requested to follow you",
            "poll":           f"🐘 A poll you voted in has ended",
            "status":         f"🐘 {display} posted",
            "update":         f"🐘 A post you boosted was edited",
        }

        title = type_labels.get(ntype, f"🐘 {display}: {ntype}")

        send_to_bot(
            notif_type="mastodon",
            source=f"@{acct}",
            title=title,
            body=content[:500] if content else "",
            metadata={"url": status_url} if status_url else None,
        )

    # Store the highest ID (first in list = newest)
    state["mastodon_last_id"] = notifications[0]["id"]
    return state

# ── Bluesky poller ────────────────────────────────────────────────────
def bluesky_auth() -> tuple[str, str]:
    """Authenticate with Bluesky and return (access_token, did)."""
    r = requests.post(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"identifier": BLUESKY_HANDLE, "password": BLUESKY_PASSWORD},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return data["accessJwt"], data["did"]

def poll_bluesky(state: dict) -> dict:
    """Fetch new Bluesky notifications and forward to the bot."""
    if not BLUESKY_HANDLE or not BLUESKY_PASSWORD:
        log.info("Bluesky: skipped (not configured)")
        return state

    log.info(f"Bluesky: polling for {BLUESKY_HANDLE}")

    try:
        token, did = bluesky_auth()
    except Exception as e:
        log.error(f"Bluesky: auth failed: {e}")
        return state

    headers = {"Authorization": f"Bearer {token}"}
    last_seen = state.get("bluesky_last_seen")  # ISO timestamp

    try:
        params = {"limit": 30}
        r = requests.get(
            "https://bsky.social/xrpc/app.bsky.notification.listNotifications",
            headers=headers,
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        notifications = data.get("notifications", [])
    except Exception as e:
        log.error(f"Bluesky: API error: {e}")
        return state

    # Filter to only new notifications
    if last_seen:
        notifications = [
            n for n in notifications
            if n.get("indexedAt", "") > last_seen
        ]

    if not notifications:
        log.info("Bluesky: no new notifications")
        return state

    log.info(f"Bluesky: {len(notifications)} new notification(s)")

    # Process oldest first
    for n in sorted(notifications, key=lambda x: x.get("indexedAt", "")):
        reason = n.get("reason", "unknown")
        author = n.get("author", {})
        display = author.get("displayName") or author.get("handle", "someone")
        handle = author.get("handle", "")

        # Extract post text if present
        record = n.get("record", {})
        post_text = record.get("text", "")

        # Build a bsky.app URL if we have the post URI
        post_url = ""
        uri = n.get("uri", "")
        if uri and uri.startswith("at://"):
            # at://did:plc:xxx/app.bsky.feed.post/yyy → profile URL
            parts = uri.split("/")
            if len(parts) >= 5:
                post_url = f"https://bsky.app/profile/{handle}/post/{parts[-1]}"

        type_labels = {
            "like":      f"🦋 {display} liked your post",
            "repost":    f"🦋 {display} reposted your post",
            "follow":    f"🦋 {display} followed you",
            "mention":   f"🦋 {display} mentioned you",
            "reply":     f"🦋 {display} replied to you",
            "quote":     f"🦋 {display} quoted your post",
        }

        title = type_labels.get(reason, f"🦋 {display}: {reason}")

        send_to_bot(
            notif_type="bluesky",
            source=f"@{handle}",
            title=title,
            body=post_text[:500] if post_text else "",
            metadata={"url": post_url} if post_url else None,
        )

    # Store the newest timestamp
    newest = max(n.get("indexedAt", "") for n in notifications)
    if newest:
        state["bluesky_last_seen"] = newest

    return state

# ── Main ──────────────────────────────────────────────────────────────
def main():
    if not WEBHOOK_URL or not WEBHOOK_SECRET:
        log.error("WEBHOOK_URL and WEBHOOK_SECRET are required")
        sys.exit(1)

    state = load_state()
    state = poll_mastodon(state)
    state = poll_bluesky(state)
    save_state(state)
    log.info("Done")

if __name__ == "__main__":
    main()
