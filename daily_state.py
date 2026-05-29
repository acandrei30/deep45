"""Tracks how many of today's 4 sprints have been completed.

State persists to ~/.fortyfive_daily.json and resets at midnight (local time).
The cap is locked at 4 — by design, not configurable."""

import json
import os
from datetime import date

DAILY_LIMIT = 4
STATE_FILE = os.path.join(os.path.expanduser("~"), ".fortyfive_daily.json")


def _today():
    return date.today().isoformat()


def _load():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    today = _today()
    if data.get("date") != today:
        data = {"date": today, "completed": 0}
    return data


def _save(data):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def completed_today():
    return _load()["completed"]


def remaining_today():
    return max(0, DAILY_LIMIT - completed_today())


def can_start():
    return completed_today() < DAILY_LIMIT


def mark_completed():
    data = _load()
    data["completed"] = min(DAILY_LIMIT, data["completed"] + 1)
    _save(data)
    return data["completed"]
