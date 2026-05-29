"""User-toggleable settings for 45. Persisted to ~/.fortyfive_settings.json."""

import json
import os

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".fortyfive_settings.json")

DEFAULTS = {
    "presence_enabled": True,
    "onboarded": False,
}


def load():
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    return {**DEFAULTS, **data}


def save(data):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def get(key):
    return load().get(key, DEFAULTS.get(key))


def set_value(key, value):
    data = load()
    data[key] = value
    save(data)
