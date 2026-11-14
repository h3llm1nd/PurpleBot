"""Rain bot: checks Open-Meteo (backup: MET Norway) every run and posts to a Telegram chat
when rain starts / is coming soon, and when it stops."""
import datetime as dt
import json
import os
import time
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


def http_json(url, data=None, attempts=3, timeout=25, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except OSError as e:  # network hiccup: wait and retry
            if i == attempts - 1:
                raise
            print(f"retry {i + 1}: {e}")
            time.sleep(5 * (i + 1))


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


def forecast_open_meteo():
    q = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON,
        "minutely_15": "precipitation",
        "past_minutely_15": 1, "forecast_minutely_15": 6,
        "timezone": "Europe/Kyiv",
    })
    m = http_json(f"https://api.open-meteo.com/v1/forecast?{q}")["minutely_15"]
    return [(dt.datetime.fromisoformat(t).replace(tzinfo=TZ), p or 0.0)
            for t, p in zip(m["time"], m["precipitation"])]


def forecast_met_norway():
    """Backup source (MET Norway, hourly). Spread each hour over 4 slots."""
    q = urllib.parse.urlencode({"lat": round(float(LAT), 4), "lon": round(float(LON), 4)})
    data = http_json(
        f"https://api.met.no/weatherapi/locationforecast/2.0/compact?{q}",
        headers={"User-Agent": "PurpleRain-bot github.com/h3llm1nd/PurpleBot"})
    slots = []
    for item in data["properties"]["timeseries"][:3]:
        start = dt.datetime.fromisoformat(item["time"].replace("Z", "+00:00")).astimezone(TZ)
        amount = (item["data"].get("next_1_hours", {}).get("details", {})
                  .get("precipitation_amount", 0.0)) / 4
        slots += [(start + dt.timedelta(minutes=15 * (k + 1)), amount) for k in range(4)]
    return slots


def forecast():
    for source in (forecast_open_meteo, forecast_met_norway):
        try:
            return source()
        except Exception as e:  # try the next weather service
            print(f"{source.__name__} failed: {e}")
    return None


def main():
    now = dt.datetime.now(TZ)
    slots = forecast()
    if slots is None:
        print("All weather services are unreachable, skipping this check.")
        if TEST:
            send("💜 PurpleRain is online! (Weather services didn't answer this time, will retry in 15 min.)")
        return
    past = [p for t, p in slots if t <= now]
    future = [(t, p) for t, p in slots if t > now][:4]   # next ~hour
    rain_now = bool(past) and past[-1] >= THRESHOLD
    soon = next((t for t, p in future if p >= THRESHOLD), None)

    if TEST:
        status = "it's raining" if rain_now else (
            f"rain expected around {soon:%H:%M}" if soon else "dry for the next hour")
        send(f"💜 PurpleRain is online! Right now: {status}.")
        return

    quiet = now.hour >= QUIET_FROM or now.hour < QUIET_TO
    if quiet:
        return  # don't touch state at night, so a morning rain still gets announced

    state = load_state()
    if (rain_now or soon) and not state["alerted"]:
        if rain_now:
            send("💜🌧 PurpleRain: it's raining outside! Grab an umbrella and the dog's raincoat.")
        else:
            send(f"💜☔ PurpleRain: rain expected around {soon:%H:%M}. Want a walk? Go now or wait it out.")
        state["alerted"] = True
    elif not rain_now and not soon and state["alerted"]:
        send("💜🌤 PurpleRain: the rain has stopped. Time for a walk!")
        state["alerted"] = False
    save_state(state)


if __name__ == "__main__":
    main()
