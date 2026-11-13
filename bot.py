"""Rain bot: checks Open-Meteo every run and posts to a Telegram chat
when rain starts / is coming soon, and when it stops."""
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")
LAT = os.environ["LAT"]
LON = os.environ["LON"]
TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
THRESHOLD = float(os.getenv("THRESHOLD_MM", "0.1"))  # mm per 15 min
QUIET_FROM = int(os.getenv("QUIET_FROM", "23"))       # no messages from 23:00
QUIET_TO = int(os.getenv("QUIET_TO", "7"))            # ...until 07:00
STATE_FILE = "state.json"
TEST = os.getenv("TEST") == "true"


def http_json(url, data=None):
    with urllib.request.urlopen(url, data=data, timeout=20) as r:
        return json.load(r)


def send(text):
    body = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    http_json(f"https://api.telegram.org/bot{TOKEN}/sendMessage", body)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"alerted": False}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def forecast():
    q = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON,
        "minutely_15": "precipitation",
        "past_minutely_15": 1, "forecast_minutely_15": 6,
        "timezone": "Europe/Kyiv",
    })
    m = http_json(f"https://api.open-meteo.com/v1/forecast?{q}")["minutely_15"]
    return [(dt.datetime.fromisoformat(t).replace(tzinfo=TZ), p or 0.0)
            for t, p in zip(m["time"], m["precipitation"])]


def main():
    now = dt.datetime.now(TZ)
    slots = forecast()
    past = [p for t, p in slots if t <= now]
    future = [(t, p) for t, p in slots if t > now][:4]   # next ~hour
    rain_now = bool(past) and past[-1] >= THRESHOLD
    soon = next((t for t, p in future if p >= THRESHOLD), None)

    if TEST:
        status = "идёт дождь" if rain_now else (
            f"дождь ожидается около {soon:%H:%M}" if soon else "в ближайший час сухо")
        send(f"💜 PurpleRain на связи! Сейчас: {status}.")
        return

    quiet = now.hour >= QUIET_FROM or now.hour < QUIET_TO
    if quiet:
        return  # don't touch state at night, so a morning rain still gets announced

    state = load_state()
    if (rain_now or soon) and not state["alerted"]:
        if rain_now:
            send("💜🌧 PurpleRain: на улице идёт дождь! Зонт и дождевик для собаки не забудьте.")
        else:
            send(f"💜☔ PurpleRain: около {soon:%H:%M} начнётся дождь. Хотите гулять — выходите сейчас или переждите.")
        state["alerted"] = True
    elif not rain_now and not soon and state["alerted"]:
        send("💜🌤 PurpleRain: дождь закончился, можно идти гулять!")
        state["alerted"] = False
    save_state(state)


if __name__ == "__main__":
    main()
