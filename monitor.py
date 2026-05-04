#!/usr/bin/env python3
"""Yellowstone lodging availability monitor.

Polls the (undocumented) Xanterra availability endpoint and prints + notifies
when watched lodges flip from no-availability to available.
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import yaml
from curl_cffi import requests

ENDPOINT = "https://webapi.xanterra.net/v1/api/availability/hotels/yellowstonenationalparklodges"
BOOKING_URL = "https://secure.yellowstonenationalparklodges.com/booking/lodging-flex-search"
HEADERS = {
    "Origin": "https://secure.yellowstonenationalparklodges.com",
    "Referer": "https://secure.yellowstonenationalparklodges.com/",
    "Accept": "application/json, text/plain, */*",
}

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
SECRETS_PATH = ROOT / "secrets.local.yaml"
STATE_PATH = ROOT / "state.json"

LODGE_NAMES = {
    "YLOI": "Old Faithful Inn",
    "YLOS": "Old Faithful Snow Lodge",
    "YLOL": "Old Faithful Lodge Cabins",
    "YLLH": "Lake Yellowstone Hotel",
    "YLLL": "Lake Lodge Cabins",
    "YLCL": "Canyon Lodge",
    "YLMH": "Mammoth Hot Springs Hotel",
    "YLGV": "Grant Village",
    "YLRL": "Roosevelt Lodge Cabins",
}


def to_api_date(iso_date: str) -> str:
    """Config uses ISO (YYYY-MM-DD); API wants DD-MM-YYYY."""
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d-%m-%Y")


def fetch_window(iso_date: str, nights: int, limit: int) -> dict:
    api_date = to_api_date(iso_date)
    params = {"date": api_date, "limit": limit, "nights": nights, "rate_code": "INTERNET"}
    r = requests.get(
        ENDPOINT, params=params, headers=HEADERS, impersonate="chrome120", timeout=30
    )
    if r.status_code == 422:
        raise ValueError(f"API rejected window date={iso_date} (sent {api_date}): {r.text}")
    r.raise_for_status()
    return r.json().get("availability", {})


def availability_for(lodge_data: dict, guests: int) -> tuple[bool, int]:
    """Return (any-room-bookable, max_guests_fit) for the lodge.

    A lodge can still be useful even if no single room fits the full party
    (6 guests usually means 2 rooms), so we report the largest perGuests
    bucket that has rooms left.
    """
    if lodge_data.get("status") != "OPEN":
        return False, 0
    best = 0
    for n_str, info in (lodge_data.get("perGuests") or {}).items():
        if info.get("s") == "closed" or info.get("a", 0) <= 0:
            continue
        try:
            n = int(n_str)
        except ValueError:
            continue
        if n > best:
            best = n
    fits_party = best >= guests
    return best > 0, best


def booking_link(code: str, response_date: str, nights: int, adults: int, children: int) -> str:
    """Build a flex-search URL. response_date comes in MM/DD/YYYY from the API.

    The booking website's URL format is MM-DD-YYYY (matches what we see in browser).
    """
    web_date = response_date.replace("/", "-")
    params = {
        "destination": code,
        "adults": adults,
        "children": children,
        "infants": 0,
        "dateFrom": web_date,
        "nights": nights,
    }
    return f"{BOOKING_URL}?{urlencode(params)}"


def macos_notify(title: str, body: str) -> None:
    safe_title = title.replace('"', "'")
    safe_body = body.replace('"', "'")
    subprocess.run(
        ["osascript", "-e", f'display notification "{safe_body}" with title "{safe_title}"'],
        check=False,
    )


def webhook_notify(url: str, title: str, body: str) -> None:
    requests.post(url, json={"text": f"*{title}*\n{body}"}, impersonate="chrome120", timeout=10)


def ntfy_notify(topic: str, title: str, body: str, server: str = "https://ntfy.sh") -> None:
    requests.post(
        f"{server.rstrip('/')}/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": "tent,bell"},
        impersonate="chrome120",
        timeout=10,
    )


def pushover_notify(token: str, user: str, title: str, body: str) -> None:
    requests.post(
        "https://api.pushover.net/1/messages.json",
        data={"token": token, "user": user, "title": title, "message": body, "priority": 1},
        impersonate="chrome120",
        timeout=10,
    )


def discord_notify(url: str, title: str, body: str) -> None:
    """Discord webhook. Renders as a card-style embed with a clickable link."""
    link = next((tok for tok in body.split() if tok.startswith("http")), None)
    payload = {
        "username": "Yellowstone Monitor",
        "embeds": [
            {
                "title": title,
                "description": body,
                "url": link,
                "color": 0x2F8B2F,
            }
        ],
    }
    requests.post(url, json=payload, impersonate="chrome120", timeout=10)


def notify(cfg: dict, title: str, body: str) -> None:
    n = cfg.get("notify") or {}
    kind = n.get("type", "none")
    print(f"[NOTIFY] {title} — {body}")
    if kind == "macos":
        macos_notify(title, body)
    elif kind == "webhook" and n.get("webhook_url"):
        webhook_notify(n["webhook_url"], title, body)
    elif kind == "ntfy" and n.get("topic"):
        ntfy_notify(n["topic"], title, body, n.get("server", "https://ntfy.sh"))
    elif kind == "pushover" and n.get("token") and n.get("user"):
        pushover_notify(n["token"], n["user"], title, body)
    elif kind == "discord" and n.get("webhook_url"):
        discord_notify(n["webhook_url"], title, body)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    if SECRETS_PATH.exists():
        secrets = yaml.safe_load(SECRETS_PATH.read_text()) or {}
        cfg = deep_merge(cfg, secrets)
    # CI/CD path: let env vars inject the webhook URL so secrets stay out of the repo.
    env_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if env_url:
        cfg.setdefault("notify", {})
        cfg["notify"]["type"] = "discord"
        cfg["notify"]["webhook_url"] = env_url
    return cfg


def main() -> int:
    cfg = load_config()
    nights = cfg["nights"]
    adults = cfg["guests"]["adults"]
    children = cfg["guests"]["children"]
    guests = adults + children
    target_dates = cfg.get("target_dates", "any")
    watch = set(cfg["watch"])

    state = load_state()
    new_state = {}
    new_openings = []
    any_data = False

    for window in cfg["windows"]:
        try:
            avail = fetch_window(window["date"], nights, window.get("limit", 31))
        except Exception as e:
            print(f"[ERROR] window {window['date']}: {e}")
            continue
        any_data = True

        for date in sorted(avail.keys()):
            if target_dates != "any" and date not in target_dates:
                continue
            lodges = avail[date]
            for code in sorted(watch):
                data = lodges.get(code)
                if data is None:
                    continue
                key = f"{code}|{date}"
                available, max_fit = availability_for(data, guests)
                new_state[key] = available
                was = state.get(key, False)
                if available and not was:
                    new_openings.append((code, date, max_fit))
                if available:
                    fits = "fits party" if max_fit >= guests else f"max {max_fit}/room"
                    marker = f"AVAILABLE ({fits})"
                else:
                    marker = "—"
                name = LODGE_NAMES.get(code, code)
                print(f"  {date}  {code:<5}  {name:<32} {marker}")

    if not any_data:
        return 1

    save_state(new_state)

    if new_openings:
        for code, date, max_fit in new_openings:
            name = LODGE_NAMES.get(code, code)
            link = booking_link(code, date, nights, adults, children)
            fits = "fits party" if max_fit >= guests else f"max {max_fit}/room"
            notify(
                cfg,
                f"Yellowstone: {name} available",
                f"{date} • {nights}n • {fits} • {link}",
            )
    else:
        print("\nNo new openings.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
