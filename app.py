#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A Telegram bot that detects new posters in specified X (Twitter) communities and user pages.
Designed to run on Google Cloud with a webhook.
"""

import os
import sys
import re
import json
import time
import asyncio
import logging
import pathlib
from typing import Any, Dict, List, Optional, Set

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout, # Log to stdout for Google Cloud Logging
)
log = logging.getLogger("poster-bot")

# -----------------------------------------------------------------------------
# Env & Config
# -----------------------------------------------------------------------------
# REQUIRED: Get your bot token from BotFather
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
if not TG_TOKEN:
    raise SystemExit("Missing TG_TOKEN. Set it as an environment variable.")

# REQUIRED: The URL of your deployed Google Cloud service
# Google Cloud Run will provide this via the $URL environment variable
PUBLIC_URL = os.getenv("URL", "").strip()
if not PUBLIC_URL:
    raise SystemExit("Missing URL. Set it as an environment variable (e.g., from Google Cloud Run).")
PORT = int(os.environ.get('PORT', 8080))

# OPTIONAL: A chat ID to send alerts to by default
DEFAULT_CHAT_ID = int(os.getenv("DEFAULT_CHAT_ID", "0"))

# OPTIONAL: How often to check for new posters (in seconds). Default is 5 minutes.
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "300"))

# OPTIONAL: How many recent posts to check on each page. Default is 20.
POSTS_TO_CHECK = int(os.getenv("POSTS_TO_CHECK", "20"))

# Directory to store bot data
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "/app/data")) # Use /app/data for Cloud Run
DATA_DIR.mkdir(parents=True, exist_ok=True)

# File paths for storing data
SUBS_FILE = DATA_DIR / "subscribers.txt"
COMMUNITIES_FILE = DATA_DIR / "communities.json"
SEEN_POSTERS_FILE = DATA_DIR / "seen_posters.json"

# A list of public Nitter mirrors to try.
NITTER_MIRRORS = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.l5.ca",
]

# -----------------------------------------------------------------------------
# Data Persistence
# -----------------------------------------------------------------------------
SUBS: Set[int] = set()
MONITORED_HANDLES: Set[str] = set() # Now using a generic term "handles"
SEEN_POSTERS: Dict[str, Set[str]] = {} # { "handle": {"poster1", "poster2"} }

def _load_data():
    """Loads all persistent data from files."""
    global SUBS, MONITORED_HANDLES, SEEN_POSTERS
    try:
        if SUBS_FILE.exists():
            SUBS = {int(line.strip()) for line in SUBS_FILE.read_text().splitlines() if line.strip()}
        if COMMUNITIES_FILE.exists():
            MONITORED_HANDLES = set(json.loads(COMMUNITIES_FILE.read_text()))
        if SEEN_POSTERS_FILE.exists():
            data = json.loads(SEEN_POSTERS_FILE.read_text())
            SEEN_POSTERS = {k: set(v) for k, v in data.items()}
        log.info(f"Loaded {len(SUBS)} subs, {len(MONITORED_HANDLES)} handles.")
    except Exception as e:
        log.error(f"Failed to load data: {e}")

def _save_data():
    """Saves all persistent data to files."""
    try:
        SUBS_FILE.write_text("\n".join(map(str, sorted(SUBS))))
        COMMUNITIES_FILE.write_text(json.dumps(sorted(list(MONITORED_HANDLES)), indent=2))
        data_to_save = {k: sorted(list(v)) for k, v in SEEN_POSTERS.items()}
        SEEN_POSTERS_FILE.write_text(json.dumps(data_to_save, indent=2))
    except Exception as e:
        log.error(f"Failed to save data: {e}")

# -----------------------------------------------------------------------------
# Nitter Scraper
# -----------------------------------------------------------------------------
def _get_nitter_html(handle: str) -> Optional[str]:
    """Tries to fetch the Nitter page for a given handle."""
    path = f"/{handle}"
    for mirror in NITTER_MIRRORS:
        try:
            url = f"{mirror}{path}"
            log.debug(f"Fetching {url}")
            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                return response.text
            log.warning(f"Failed {url}. Status: {response.status_code}")
        except requests.RequestException as e:
            log.warning(f"Error fetching {url}: {e}")
        time.sleep(1)
    return None

def _is_community_page(html: str) -> bool:
    """Checks if the fetched HTML is for a Community page."""
    if not html:
        return False
    # Nitter community pages have a specific header structure
    return "Community" in html

def _extract_posters(html: str, limit: int) -> Set[str]:
    """Parses the HTML to find usernames of posters."""
    if not html:
        return set()
    soup = BeautifulSoup(html, 'html.parser')
    posters = set()
    timeline_items = soup.find_all('div', class_='timeline-item', limit=limit)
    for item in timeline_items:
        username_link = item.find('a', class_='username')
        if username_link and username_link.find('bdi'):
            username = username_link.find('bdi').get_text(strip=True).lstrip('@')
            if username:
                posters.add(username.lower())
    return posters

# -----------------------------------------------------------------------------
# Bot Logic
# -----------------------------------------------------------------------------
def _format_alert(handle: str, new_posters: List[str], is_community: bool) -> str:
    """Formats the alert message for Telegram."""
    page_type = "Community" if is_community else "User"
    poster_mentions = ", ".join(f"@{poster}" for poster in new_posters)
    return (
        f"🔔 <b>New posters on @{handle} ({page_type} Page)</b>\n\n"
        f"👤 {len(new_posters)} new user(s):\n{poster_mentions}"
    )

async def _send_alert_to_all(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Sends a message to all subscribed chats."""
    for chat_id in SUBS:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
        except Exception as e:
            log.error(f"Failed to send to chat {chat_id}: {e}")

async def check_for_new_posters(context: ContextTypes.DEFAULT_TYPE):
    """The main periodic job to check for new posters."""
    if not MONITORED_HANDLES:
        log.info("No handles to monitor. Skipping check.")
        return

    log.info(f"Starting check for {len(MONITORED_HANDLES)} handles...")
    for handle in sorted(list(MONITORED_HANDLES)):
        log.info(f"Checking @{handle}")
        if handle not in SEEN_POSTERS:
            SEEN_POSTERS[handle] = set()

        html = _get_nitter_html(handle)
        if not html:
            log.warning(f"Could not fetch HTML for @{handle}. Skipping.")
            continue

        is_community = _is_community_page(html)
        current_posters = _extract_posters(html, limit=POSTS_TO_CHECK)
        new_posters = list(current_posters - SEEN_POSTERS[handle])
        
        if new_posters:
            log.info(f"Found {len(new_posters)} new posters for @{handle}.")
            SEEN_POSTERS[handle].update(new_posters)
            alert_message = _format_alert(handle, new_posters, is_community)
            await _send_alert_to_all(context, alert_message)
            await asyncio.sleep(1)
        else:
            log.info(f"No new posters for @{handle}.")
    
    log.info("Check complete. Saving data.")
    _save_data()

# -----------------------------------------------------------------------------
# Telegram Bot Commands
# ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    SUBS.add(chat_id)
    _save_data()
    await update.message.reply_html(f"✅ Hello {user.mention_html()}! You are now subscribed.")

async def cmd_add_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add_handle <handle>\nExample: /add_handle solana")
        return
    handle = context.args[0].lower().lstrip('@')
    if not re.match(r"^[a-zA-Z0-9_]{1,15}$", handle):
        await update.message.reply_text("Invalid handle.")
        return
    if handle in MONITORED_HANDLES:
        await update.message.reply_text(f"✅ '@{handle}' is already being monitored.")
        return
    html = _get_nitter_html(handle)
    if not html:
        await update.message.reply_text(f"❌ Could not find a page for '@{handle}'.")
        return
    MONITORED_HANDLES.add(handle)
    _save_data()
    await update.message.reply_text(f"✅ Added '@{handle}' to the monitor list.")

async def cmd_remove_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove_handle <handle>")
        return
    handle = context.args[0].lower().lstrip('@')
    if handle not in MONITORED_HANDLES:
        await update.message.reply_text(f"'@{handle}' is not being monitored.")
        return
    MONITORED_HANDLES.remove(handle)
    if handle in SEEN_POSTERS:
        del SEEN_POSTERS[handle]
    _save_data()
    await update.message.reply_text(f"✅ Removed '@{handle}' from the monitor list.")

async def cmd_list_handles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not MONITORED_HANDLES:
        await update.message.reply_text("Not monitoring any handles yet.")
        return
    handle_list = "\n".join(f"• @{handle}" for handle in sorted(list(MONITORED_HANDLES)))
    await update.message.reply_html(f"<b>Monitored Handles ({len(MONITORED_HANDLES)}):</b>\n\n{handle_list}")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    community_count = len(MONITORED_HANDLES)
    subscriber_count = len(SUBS)
    total_seen = sum(len(posters) for posters in SEEN_POSTERS.values())
    status_text = (
        f"<b>Bot Status</b>\n\n"
        f"🔍 <b>Handles Monitored:</b> {community_count}\n"
        f"👥 <b>Subscribers:</b> {subscriber_count}\n"
        f"📝 <b>Total Posters Seen:</b> {total_seen}"
    )
    await update.message.reply_html(status_text)

# -----------------------------------------------------------------------------
# Flask Web Server for Webhook
# -----------------------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route('/healthz', methods=['GET'])
def health_check():
    """Simple health check endpoint for Google Cloud."""
    return 'OK', 200

@flask_app.route(f'/{TG_TOKEN}', methods=['POST'])
def telegram_webhook():
    """Receives updates from Telegram."""
    if request.headers.get('content-type') != 'application/json':
        abort(403)
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.process_update(update)
    return 'OK', 200

# -----------------------------------------------------------------------------
# Main Function
# -----------------------------------------------------------------------------
def main():
    log.info("Starting bot...")
    _load_data()
    if DEFAULT_CHAT_ID > 0 and DEFAULT_CHAT_ID not in SUBS:
        SUBS.add(DEFAULT_CHAT_ID)
        _save_data()

    global application
    application = Application.builder().token(TG_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("add_handle", cmd_add_handle))
    application.add_handler(CommandHandler("remove_handle", cmd_remove_handle))
    application.add_handler(CommandHandler("list_handles", cmd_list_handles))
    application.add_handler(CommandHandler("status", cmd_status))

    # Set the webhook
    application.bot.set_webhook(url=f"{PUBLIC_URL}/{TG_TOKEN}")
    
    # Start the background job
    application.job_queue.run_repeating(
        check_for_new_posters,
        interval=CHECK_INTERVAL_SEC,
        first=10,
        name="poster_check"
    )
    log.info("Webhook set and background job started. Bot is ready.")

if __name__ == '__main__':
    main()
    # Run the Flask app
    flask_app.run(host='0.0.0.0', port=PORT)
