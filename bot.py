"""Rain bot: checks Open-Meteo (backup: MET Norway) every run and posts to a Telegram chat
when rain starts / is coming soon / stops, sends walk summaries at 9:00 and 17:00,
and warns about poor air quality."""
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
SUMMARY_HOURS = [int(h) for h in os.getenv("SUMMARY_HOURS", "9,17").split(",")]
AQI_BAD = 60    # European AQI: 60+ = poor air
AQI_OK = 40     # back below 40 = fair again
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


WEATHER = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
           45: "fog", 48: "fog", 51: "drizzle", 53: "drizzle", 55: "drizzle",
           56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
           65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
           73: "snow", 75: "heavy snow", 77: "snow grains", 80: "showers", 81: "showers",
           82: "heavy showers", 85: "snow showers", 86: "snow showers",
           95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm"}


def walk_weather():
    """Current conditions + next 2 hours. Returns a dict or None."""
    try:
        q = urllib.parse.urlencode({
            "latitude": LAT, "longitude": LON, "timezone": "Europe/Kyiv",
            "current": "temperature_2m,apparent_temperature,weather_code,"
                       "wind_speed_10m,wind_gusts_10m",
            "hourly": "precipitation_probability,precipitation,snowfall",
            "forecast_hours": 3,
        })
        d = http_json(f"https://api.open-meteo.com/v1/forecast?{q}")
        c, h = d["current"], d["hourly"]
        hours = list(zip(h["time"], h["precipitation_probability"],
                         h["precipitation"], h["snowfall"]))[1:3]   # next 2 hours
        wet = [(t, pr) for t, pr, mm, sn in hours if (pr or 0) >= 50 or (mm or 0) >= 0.2]
        return {
            "temp": c["temperature_2m"], "feels": c["apparent_temperature"],
            "sky": WEATHER.get(c["weather_code"], ""), "code": c["weather_code"],
            "wind": c["wind_speed_10m"], "gusts": c["wind_gusts_10m"],
            "rain_at": wet[0][0][-5:] if wet else None,
            "rain_prob": wet[0][1] if wet else None,
            "snow": any((sn or 0) > 0 for *_, sn in hours) or c["weather_code"] in (71, 73, 75, 77, 85, 86),
        }
    except Exception as e:
        print(f"open-meteo walk weather failed: {e}")
    try:  # backup: MET Norway (no gusts / feels-like there)
        q = urllib.parse.urlencode({"lat": round(float(LAT), 4), "lon": round(float(LON), 4)})
        d = http_json(f"https://api.met.no/weatherapi/locationforecast/2.0/compact?{q}",
                      headers={"User-Agent": "PurpleRain-bot github.com/h3llm1nd/PurpleBot"})
        ts = d["properties"]["timeseries"]
        now = ts[0]["data"]
        det = now["instant"]["details"]
        wet = None
        for item in ts[:2]:
            mm = item["data"].get("next_1_hours", {}).get("details", {}).get("precipitation_amount", 0)
            if mm >= 0.2:
                wet = dt.datetime.fromisoformat(item["time"].replace("Z", "+00:00")).astimezone(TZ)
                break
        symbol = now.get("next_1_hours", {}).get("summary", {}).get("symbol_code", "")
        return {
            "temp": det["air_temperature"], "feels": det["air_temperature"],
            "sky": symbol.split("_")[0].replace("partlycloudy", "partly cloudy"), "code": None,
            "wind": det["wind_speed"] * 3.6, "gusts": None,
            "rain_at": f"{wet:%H:%M}" if wet else None, "rain_prob": None,
            "snow": "snow" in symbol or "sleet" in symbol,
        }
    except Exception as e:
        print(f"met.no walk weather failed: {e}")
    return None


def air_quality():
    """European AQI (0-20 good, 20-40 fair, 40-60 moderate, 60-80 poor, 80+ very poor)."""
    try:
        q = urllib.parse.urlencode({"latitude": LAT, "longitude": LON,
                                    "current": "european_aqi", "timezone": "Europe/Kyiv"})
        aqi = http_json(f"https://air-quality-api.open-meteo.com/v1/air-quality?{q}")["current"]["european_aqi"]
        return None if aqi is None else round(aqi)
    except Exception as e:
        print(f"air quality failed: {e}")
        return None


def aqi_label(aqi):
    for limit, name in ((20, "good"), (40, "fair"), (60, "moderate"), (80, "poor"), (100, "very poor")):
        if aqi < limit:
            return name
    return "extremely poor"


def walk_tips(w, aqi):
    tips = []
    if w["rain_at"]:
        tips.append("☔ Take the dog's raincoat")
    if w["snow"]:
        tips.append("❄️ Snow: paw wax or boots, wipe paws after (road salt)")
    elif -2 <= w["temp"] <= 1 and (w["rain_at"] or w["code"] in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67)):
        tips.append("🧊 Possible ice: watch your step")
    if w["feels"] <= -5:
        tips.append("🥾 Cold: boots for paws, keep the walk shorter")
    elif w["temp"] >= 27:
        tips.append("💧 Hot: avoid hot asphalt, bring water")
    if (w["gusts"] or w["wind"]) >= 50:
        tips.append("💨 Strong wind")
    if aqi is not None and aqi >= AQI_BAD:
        tips.append("😷 Poor air: make the walk shorter")
    return tips or ["🐾 Great weather for a walk!"]


def what_to_wear(w):
    f = w["feels"]
    if f >= 24:
        outfit = ["T-shirt", "shorts", "cap"]
    elif f >= 17:
        outfit = ["T-shirt or light long sleeve"]
    elif f >= 10:
        outfit = ["hoodie or light jacket"]
    elif f >= 3:
        outfit = ["warm jacket", "light hat"]
    elif f >= -7:
        outfit = ["winter jacket", "hat", "gloves", "scarf"]
    else:
        outfit = ["thermal layer", "winter jacket", "warm hat", "gloves", "scarf"]
    if w["rain_at"]:
        outfit.append("umbrella or raincoat")
    if w["snow"] or (w["temp"] <= 1 and w["rain_at"]):
        outfit.append("non-slip winter boots")
    elif w["rain_at"]:
        outfit.append("waterproof shoes")
    if (w["gusts"] or w["wind"]) >= 40 and f < 17:
        outfit.append("something windproof")
    return ", ".join(outfit)


def walk_summary(title):
    w = walk_weather()
    if w is None:
        return None
    aqi = air_quality()
    wind = f"{w['wind']:.0f} km/h" + (f", gusts {w['gusts']:.0f}" if w["gusts"] else "")
    if w["rain_at"]:
        rain = f"rain likely around {w['rain_at']}" + (f" ({w['rain_prob']}%)" if w["rain_prob"] else "")
    else:
        rain = "dry"
    lines = [
        f"💜 PurpleRain · {title}",
        f"🌡 {w['temp']:.0f}°C" + (f" (feels like {w['feels']:.0f}°C)" if round(w["feels"]) != round(w["temp"]) else "")
        + (f", {w['sky']}" if w["sky"] else ""),
        f"💨 Wind {wind}",
        f"🌧 Next 2 hours: {rain}",
    ]
    if aqi is not None:
        lines.append(f"🌫 Air: {aqi_label(aqi)} (AQI {aqi})")
    lines.append(f"👕 Wear: {what_to_wear(w)}")
    lines.append("")
    lines += walk_tips(w, aqi)
    return "\n".join(lines)


def check_air(state):
    aqi = air_quality()
    if aqi is None:
        return
    if aqi >= AQI_BAD and not state.get("aqi_alerted"):
        send(f"💜😷 PurpleRain: the air in your area is {aqi_label(aqi)} (AQI {aqi}). "
             "Keep the walk short today.")
        state["aqi_alerted"] = True
    elif aqi < AQI_OK and state.get("aqi_alerted"):
        send(f"💜🌿 PurpleRain: the air is {aqi_label(aqi)} again (AQI {aqi}).")
        state["aqi_alerted"] = False


def maybe_summary(state, now):
    for hour in SUMMARY_HOURS:
        key = f"{now:%Y-%m-%d}-{hour:02d}"
        if now.hour == hour and state.get("summary") != key:
            title = "Morning walk" if hour < 12 else "Evening walk"
            text = walk_summary(f"{title} {hour:02d}:00")
            if text:
                send(text)
                state["summary"] = key


def main():
    now = dt.datetime.now(TZ)
    slots = forecast()
    if slots is None:
        print("Rain data unavailable, skipping the rain check.")
        if TEST:
            send(walk_summary("Test summary") or "💜 PurpleRain is online! (No weather data right now.)")
            return
    slots = slots or []
    past = [p for t, p in slots if t <= now]
    future = [(t, p) for t, p in slots if t > now][:4]   # next ~hour
    rain_now = bool(past) and past[-1] >= THRESHOLD
    soon = next((t for t, p in future if p >= THRESHOLD), None)

    if TEST:
        send(walk_summary("Test summary") or "💜 PurpleRain is online! (No weather data right now.)")
        return

    quiet = now.hour >= QUIET_FROM or now.hour < QUIET_TO
    if quiet:
        return  # don't touch state at night, so a morning rain still gets announced

    state = load_state()
    maybe_summary(state, now)
    check_air(state)
    if not slots:
        save_state(state)
        return
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
