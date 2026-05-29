"""Recent-sprint history. Persisted to ~/.fortyfive_history_<user>.json.

Stores the last N completed sprint tasks so the user can click to reuse them.
Deduplicates so re-running the same task doesn't crowd out other recent ones."""

import json
import os
from datetime import datetime

import auth

MAX_ENTRIES = 12


def _history_file():
    """Return a per-account history file path based on the signed-in email."""
    e = auth.email()
    if e:
        safe = "".join(c if c.isalnum() else "_" for c in e)
        return os.path.join(os.path.expanduser("~"), f".fortyfive_history_{safe}.json")
    return os.path.join(os.path.expanduser("~"), ".fortyfive_history.json")


def _load():
    try:
        with open(_history_file()) as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _save(entries):
    try:
        with open(_history_file(), "w") as f:
            json.dump(entries, f)
    except OSError:
        pass


def add(task):
    task = (task or "").strip()
    if not task:
        return
    entries = _load()
    # Remove any existing identical entry so the new one moves to the top.
    entries = [e for e in entries if e.get("task") != task]
    entries.insert(0, {"task": task, "at": datetime.now().isoformat(timespec="seconds")})
    entries = entries[:MAX_ENTRIES]
    _save(entries)


def recent(n=5):
    return [e["task"] for e in _load()[:n] if e.get("task")]


def remove(task):
    """Remove a specific task from history (archive)."""
    task = (task or "").strip()
    if not task:
        return
    entries = [e for e in _load() if e.get("task") != task]
    _save(entries)
