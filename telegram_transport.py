"""Bounded, resumable Telegram follow-up. Pending state contains NO descriptions.

Listing descriptions travel in expiring Actions artifacts, not git history. Only
pending identity/retry/ack metadata is committed. Telegram is the durable store.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time

import requests

from telegram_jobs import digest, job_key, messages, units


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def load(path):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class DeliveryError(Exception):
    pass


def send_plain(token, chat_id, text, *, deadline=None, post=None, sleep=time.sleep, receipt=None, parse_mode=None, disable_preview=True):
    """Retries are bounded; timeout ambiguity may duplicate a wire identity, never
    imply success. No tokens, URLs containing tokens, or response bodies logged.
    """
    if units(text) > 4096:
        raise DeliveryError("Message too large")
    post = post or requests.post
    deadline = deadline or time.monotonic() + 55
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining < 1:
            break
        wait = 2 ** attempt
        try:
            response = post(f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": text,
                                  "disable_web_page_preview": disable_preview,
                                  **({"parse_mode":parse_mode} if parse_mode else {})},
                            timeout=min(15, remaining))
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.ok and body.get("ok") is True and body.get("result", {}).get("message_id"):
                if receipt:
                    receipt(body["result"]["message_id"], body["result"].get("text"))
                return body["result"]["message_id"]
            code = body.get("error_code", response.status_code)
            if code == 429:
                wait = max(1, float(body.get("parameters", {}).get("retry_after", 1)))
            elif code < 500:
                raise DeliveryError(f"Telegram rejected message ({code})")
        except requests.RequestException:
            pass
        if attempt == 2 or time.monotonic() + wait + 1 >= deadline:
            break
        sleep(wait)
    raise DeliveryError("Telegram delivery unconfirmed; pending for retry")
