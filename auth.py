"""Account / token storage for 45. Tokens live in ~/.fortyfive_auth.json."""

import json
import os

AUTH_FILE = os.path.join(os.path.expanduser("~"), ".fortyfive_auth.json")


def load():
    try:
        with open(AUTH_FILE) as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save(token, user_id, email):
    try:
        with open(AUTH_FILE, "w") as f:
            json.dump({"token": token, "user_id": user_id, "email": email}, f)
    except OSError:
        pass


def clear():
    try:
        os.remove(AUTH_FILE)
    except OSError:
        pass


def token():
    return load().get("token", "")


def email():
    return load().get("email", "")


def signed_in():
    return bool(token())
