"""App configuration (API keys, etc.).

Locally this persists to a JSON file next to this script (kept out of git via
.gitignore since it can hold a secret). When deployed (e.g. on Render), the
filesystem is ephemeral and there's no dashboard for a hosted user to type a
key into, so OPENROUTER_API_KEY is read from the environment first -- set it
as a secret env var in the Render dashboard. The env var always wins over
whatever is in config.json, so a deployed instance can't be reconfigured by
whatever ends up in the (ephemeral) settings UI.
"""

import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load():
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_openrouter_api_key():
    env_key = os.environ.get("OPENROUTER_API_KEY")
    if env_key:
        return env_key
    return _load().get("openrouter_api_key") or None


def set_openrouter_api_key(key):
    data = _load()
    data.pop("google_api_key", None)  # superseded -- was the old Gemini-direct key
    if key:
        data["openrouter_api_key"] = key
    else:
        data.pop("openrouter_api_key", None)
    _save(data)
