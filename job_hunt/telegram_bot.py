import os
import re
import sys
import time
import json
from pathlib import Path
import requests

from job_hunt.main import load_config
from job_hunt.drafter import draft_application
from job_hunt.log import get_logger

logger = get_logger()


def send_message(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logger.error(f"Failed to send message: {e}")


def send_document(bot_token: str, chat_id: str, filepath: Path, caption: str = "") -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    if not filepath.exists():
        logger.error(f"File not found: {filepath}")
        return False
    try:
        with open(filepath, "rb") as f:
            files = {"document": f}
            data = {"chat_id": chat_id, "caption": caption}
            resp = requests.post(url, data=data, files=files, timeout=30)
            return resp.status_code == 200
    except Exception as e:
        logger.error(f"Failed to send document {filepath.name}: {e}")
        return False


def listen_telegram_bot() -> None:
    config = load_config()
    tg = config.get("telegram", {})
    bot_token = tg.get("token")
    chat_id = tg.get("chat_id")

    if not bot_token or not chat_id or "YOUR_" in bot_token.upper():
        logger.error("Telegram token and chat_id are not fully configured in config.json or .env.")
        sys.exit(1)

    logger.info("=== Telegram Bot Listener Started ===")
    logger.info("Listening for messages: 'apply to #N', 'apply to N', or simply 'N'")
    
    offset = 0
    # Retrieve the latest updates to skip old messages on startup
    try:
        r = requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates", params={"limit": 1, "timeout": 0}, timeout=10)
        if r.status_code == 200:
            updates = r.json().get("result", [])
            if updates:
                offset = updates[0]["update_id"] + 1
    except Exception as e:
        logger.warning(f"Failed to fetch initial update offset: {e}")

    while True:
        try:
            params = {"offset": offset, "timeout": 30}
            r = requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates", params=params, timeout=35)
            if r.status_code != 200:
                logger.error(f"Error fetching updates from Telegram: HTTP {r.status_code}")
                time.sleep(10)
                continue

            updates = r.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                
                message = update.get("message")
                if not message:
                    continue

                from_chat_id = str(message.get("chat", {}).get("id"))
                # Only reply to the configured chat_id for safety and privacy
                if from_chat_id != str(chat_id):
                    logger.warning(f"Ignored message from unauthorized chat_id: {from_chat_id}")
                    continue

                text = message.get("text", "").strip()
                if not text:
                    continue

                # Match patterns like: "apply to 1", "apply to #1", "/draft 1", "1"
                match = re.search(r"(?:apply\s+to\s+#?|/draft\s+|#)?(\d+)", text, re.IGNORECASE)
                if match:
                    job_num = match.group(1)
                    logger.info(f"Received request to tailor resume for Job #{job_num}")
                    send_message(bot_token, chat_id, f"⏳ <b>Processing Job #{job_num}...</b>\nFetching details and tailoring your resume. Please wait...")
                    
                    try:
                        # Call draft_application to generate files
                        out_dir = draft_application(config, f"#{job_num}")
                        
                        send_message(bot_token, chat_id, f"✅ <b>Job #{job_num} tailoring complete!</b>\nSending files now...")

                        # Locate files inside out_dir
                        files_to_send = list(out_dir.glob("*"))
                        if not files_to_send:
                            send_message(bot_token, chat_id, "⚠️ No files were generated. Check local logs.")
                            continue

                        # Send files back to Telegram
                        for filepath in files_to_send:
                            send_document(bot_token, chat_id, filepath, caption=f"Job #{job_num} - {filepath.name}")

                    except Exception as err:
                        logger.error(f"Error drafting application: {err}")
                        send_message(bot_token, chat_id, f"❌ <b>Error:</b> Failed to tailor resume for Job #{job_num}.\nDetails: <code>{err}</code>")
                else:
                    logger.debug(f"Ignored message: {text}")

        except KeyboardInterrupt:
            logger.info("Bot listener stopped.")
            break
        except Exception as e:
            logger.error(f"Unexpected error in listener loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    listen_telegram_bot()
