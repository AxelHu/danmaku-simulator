from __future__ import annotations
import json
import math
from pathlib import Path
from urllib.parse import urlsplit


def check_endpoint(provider):
    url = urlsplit(provider["base_url"])
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("base_url must not contain credentials, query or fragment")
    if not url.hostname or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1"))):
        raise ValueError("Providers require HTTPS except local development endpoints")
    if any(key in provider for key in ("api_key", "key", "token", "password")):
        raise ValueError("Use api_key_env; do not store credentials in configuration")


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config.get("providers"), dict):
        raise ValueError("Configuration needs a providers object")
    for alias, provider in config["providers"].items():
        for field in ("type", "base_url", "model", "api_key_env"):
            if not isinstance(provider.get(field), str) or not provider[field]:
                raise ValueError(f"Provider {alias}: missing {field}")
        check_endpoint(provider)
        for key, value in provider.get("pricing", {}).items():
            if key.endswith("_per_million") and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError(f"Invalid price {alias}.{key}")
    stt = config.get("stt")
    if stt:
        if not isinstance(stt, dict) or not stt.get("base_url") or not stt.get("api_key_env"):
            raise ValueError("STT requires base_url and api_key_env")
        check_endpoint(stt)
        price = stt.get("price_per_hour")
        if price is not None and (type(price) not in (int, float) or not math.isfinite(price) or price < 0):
            raise ValueError("Invalid STT price_per_hour")
    audience = config.get("audience", [])
    if not audience:
        raise ValueError("Configure at least one audience persona")
    seen = set()
    for persona in audience:
        if not persona.get("id") or persona["id"] in seen:
            raise ValueError("Audience IDs must be present and unique")
        seen.add(persona["id"])
        if persona.get("provider") not in config["providers"]:
            raise ValueError(f"Unknown provider for audience {persona['id']}")
    for section in ("generation", "scheduler", "analysis"):
        config.setdefault(section, {})
    return config
