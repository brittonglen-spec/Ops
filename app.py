from flask import Flask, render_template_string, request
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------

# CheckWX – METAR/TAF
CHECKWX_API_KEY = "7a5148aeb55547229be39ed48d36fa7c"
CHECKWX_HEADERS = {"X-API-Key": CHECKWX_API_KEY}

# AeroDataBox via RapidAPI – arrivals/departures
AERODATABOX_API_KEY = "d99b9c4845mshce634357f900db1p153470jsn43e67f5a301e"
AERODATABOX_HOST = "aerodatabox.p.rapidapi.com"

# Default airport & airline settings
DEFAULT_AIRPORT_ICAO = "EGNM"   # Leeds Bradford
JET2_PREFIX = "LS"              # Jet2 flight prefix

app = Flask(__name__)

# ---------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------
def safe(val, default="N/A"):
    if val is None:
        return default
    val = str(val).strip()
    return val if val else default


def _normalise_dt(raw):
    if not raw:
        return None
    s = str(raw).strip()
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return s


def is_today_utc(raw):
    norm = _normalise_dt(raw)
    if not norm:
        return False
    try:
        dt = datetime.fromisoformat(norm)
    except Exception:
        return False
    today_utc = datetime.now(timezone.utc).date()
    return dt.astimezone(timezone.utc).date() == today_utc


def format_time_hhmm(raw):
    if not raw:
        return "----"
    s = str(raw).strip()
    if " " in s:
        timepart = s.split(" ")[1]
    elif "T" in s:
        timepart = s.split("T")[1]
    else:
        return "----"
    hhmm = timepart[:5].replace(":", "")
    return hhmm if hhmm else "----"


def normalise_flight_number(raw_number):
    return safe(raw_number, "").replace(" ", "")


def get_temperature_from_metar(raw_metar):
    if not raw_metar:
        return None
    try:
        parts = raw_metar.split()
        for p in parts:
            if "/" in p and len(p) >= 4 and p[0].upper() in "0123456789M":
                temp = p.split("/")[0]
                if temp.startswith("M"):
                    return -int(temp[1:])
                return int(temp)
    except Exception:
        return None
    return None


def status_class(status):
    """Return CSS class for a given status."""
    s = safe(status, "").lower()
    if "divert" in s or "delay" in s:
        return "status-delayed"
    if "expected" in s:
        return "status-expected"
    if "departed" in s:
        return "status-departed"
    return ""


# ---------------------------------------------------------------------
# API CALLS
# ---------------------------------------------------------------------
def fetch_metar(airport):
    airport = airport.upper()
    url = f"https://api.checkwx.com/metar/{airport}/decoded"
    try:
        r = requests.get(url, headers=CHECKWX_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("results", 0) == 0:
            return "No METAR found."
        return data["data"][0]["raw_text"]
    except Exception as e:
        return f"Error: {e}"


def fetch_taf(airport):
    airport = airport.upper()
    url = f"https://api.checkwx.com/taf/{airport}/decoded"
    try:
        r = requests.get(url, headers=CHECKWX_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("results", 0) == 0:
            return "No TAF found."
        return data["data"][0]["raw_text"]
    except Exception as e:
        return f"Error: {e}"


def fetch_fids(airport_code):
    airport_code = airport_code.upper().strip()

    if len(airport_code) == 4:
        url = f"https://{AERODATABOX_HOST}/flights/airports/icao/{airport_code}"
    else:
        url = f"https://{AERODATABOX_HOST}/flights/airports/iata/{airport_code}"

    querystring = {
        "offsetMinutes": "-120",
        "durationMinutes": "720",
        "withLeg": "true",
        "direction": "Both",
        "withCancelled": "true",
        "withCodeshared": "true",
        "withCargo": "true",
        "withPrivate": "true",
        "withLocation": "false",
    }

    headers = {
        "x-rapidapi-key": AERODATABOX_API_KEY,
        "x-rapidapi-host": AERODATABOX_HOST,
    }

    try:
        r = requests.get(url, headers=headers, params=querystring, timeout=10)
        r.raise_for_status()
        data = r.json()
        departures = data.get("departures", []) or []
        arrivals = data.get("arrivals", []) or []
        return departures, arrivals
    except Exception as e:
        return f"Error: {e}", None


def fetch_squawks():
    url = "https://opensky-network.org/api/states/all"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        emergencies = []
        for s in data.get("states", []) or []:
            if not s or len(s) < 15:
                continue
            icao24 = safe(s[0])
            callsign = safe(s[1], "").strip()
            squawk = safe(s[14], "")
            if callsign.startswith("") and squawk in {"7700", "7600", "7500"}:
                emergencies.append(
                    {
                        "callsign": callsign,
                        "squawk": squawk,
                        "icao24": icao24,
                    }
                )
        return emergencies
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------
# DATA SHAPING FOR TEMPLATE
# ---------------------------------------------------------------------
def build_departures_rows(departures):
    if isinstance(departures, str):
        return [], departures

    rows = []
    seen = set()
    for flight in departures:
        number_raw = flight.get("number", "")
        number_norm = normalise_flight_number(number_raw)
        if not number_norm.startswith(JET2_PREFIX):
            continue

        dep = (flight.get("departure") or {})
        arr = (flight.get("arrival") or {})

        sched_obj = (dep.get("scheduledTime") or {})
        rev_obj = (dep.get("revisedTime") or {})

        sched_utc = sched_obj.get("utc") or rev_obj.get("utc")
        if not is_today_utc(sched_utc):
            continue

        status = safe(flight.get("status"), "")
        if status.lower() in ("landed", "cancelled"):
            continue

        airport = arr.get("airport") or {}
        dest_iata = safe(airport.get("iata"), "----")

        sched_local = (
            sched_obj.get("local")
            or sched_obj.get("utc")
            or rev_obj.get("local")
            or rev_obj.get("utc")
        )
        hhmm = format_time_hhmm(sched_local)

        key = (number_norm, dest_iata, hhmm, status)
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            {
                "flight": number_norm,
                "to": dest_iata,
                "time": hhmm,
                "status": status,
                "status_class": status_class(status),
            }
        )

    return rows, (None if rows else "No Jet2 departures today.")


def build_arrivals_rows(arrivals, airport_code):
    if isinstance(arrivals, str):
        return [], arrivals

    rows = []
    seen = set()
    for flight in arrivals:
        number_raw = flight.get("number", "")
        number_norm = normalise_flight_number(number_raw)
        if not number_norm.startswith(JET2_PREFIX):
            continue

        dep = (flight.get("departure") or {})
        arr = (flight.get("arrival") or {})

        sched_obj = (arr.get("scheduledTime") or {})
        rev_obj = (arr.get("revisedTime") or {})

        sched_utc = sched_obj.get("utc") or rev_obj.get("utc")
        if not is_today_utc(sched_utc):
            continue

        status = safe(flight.get("status"), "")
        if status.lower() in ("landed", "cancelled"):
            continue

        airport = dep.get("airport") or {}
        origin_iata = safe(airport.get("iata"), "----")

        sched_local = (
            sched_obj.get("local")
            or sched_obj.get("utc")
            or rev_obj.get("local")
            or rev_obj.get("utc")
        )
        hhmm = format_time_hhmm(sched_local)

        key = (number_norm, origin_iata, hhmm, status)
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            {
                "flight": number_norm,
                "origin": origin_iata,
                "time": hhmm,
                "status": status,
                "status_class": status_class(status),
            }
        )

    return rows, (None if rows else f"No Jet2 arrivals into {airport_code} today.")


# ---------------------------------------------------------------------
# ROUTE + TEMPLATE
# ---------------------------------------------------------------------
TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Airport Operations Monitor (Flask)</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background: #111827;
            color: #e5e7eb;
            margin: 0;
            padding: 0;
        }
        .container {
            max-width: 1300px;
            margin: 0 auto;
            padding: 20px;
        }
        h1, h2, h3 {
            margin-top: 0.4em;
        }
        .card {
            background: #1f2937;
            border-radius: 10px;
            padding: 15px 20px;
            margin-bottom: 15px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.5);
        }
        .flex {
            display: flex;
            gap: 15px;
        }
        .flex > .card {
            flex: 1;
        }
        label {
            font-weight: bold;
        }
        input[type=text] {
            padding: 6px 10px;
            border-radius: 6px;
            border: 1px solid #4b5563;
            background: #111827;
            color: #e5e7eb;
        }
        button {
            padding: 6px 14px;
            border-radius: 6px;
            background: #3b82f6;
            border: none;
            color: white;
            font-weight: 600;
            cursor: pointer;
        }
        button:hover {
            background: #2563eb;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 8px;
        }
        th, td {
            padding: 6px 8px;
            border-bottom: 1px solid #374151;
            text-align: left;
            font-family: "Courier New", monospace;
        }
        th {
            background: #111827;
        }
        .status-departed {
            color: #22c55e; /* green */
        }
        .status-expected {
            color: #fbbf24; /* amber */
        }
        .status-delayed {
            color: #f87171; /* red */
        }
        .tag {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 999px;
            font-size: 0.8em;
            background: #374151;
        }
        pre {
            white-space: pre-wrap;
            word-wrap: break-word;
            font-family: "Courier New", monospace;
        }
        .small {
            font-size: 0.85em;
            color: #9ca3af;
        }
    </style>
</head>
<body>
<div class="container">
    <h1>✈️Airport Operations Monitor (Flask)</h1>
    <p class="small">
        METAR/TAF from CheckWX · FIDS from AeroDataBox · Emergency squawks from OpenSky.
    </p>

    <form method="get">
        <label for="airport">Airport ICAO: </label>
        <input type="text" id="airport" name="airport" value="{{ airport }}" maxlength="4">
        <button type="submit">Get Weather & Flights</button>
    </form>

    <div class="flex" style="margin-top: 15px;">
        <div class="card">
            <h2>METAR</h2>
            {% if metar %}
                <pre>{{ metar }}</pre>
            {% else %}
                <p>No METAR.</p>
            {% endif %}
        </div>
        <div class="card">
            <h2>TAF</h2>
            {% if taf %}
                <pre>{{ taf }}</pre>
            {% else %}
                <p>No TAF.</p>
            {% endif %}
        </div>
    </div>

    <div class="flex">
        <div class="card">
            <h2>Departures ({{ airport }})</h2>
            {% if dep_error %}
                <p>{{ dep_error }}</p>
            {% elif departures %}
                <table>
                    <thead>
                    <tr>
                        <th>FLIGHT</th>
                        <th>TO</th>
                        <th>TIME</th>
                        <th>STATUS</th>
                    </tr>
                    </thead>
                    <tbody>
                    {% for row in departures %}
                        <tr class="{{ row.status_class }}">
                            <td>{{ row.flight }}</td>
                            <td>{{ row.to }}</td>
                            <td>{{ row.time }}</td>
                            <td>{{ row.status }}</td>
                        </tr>
                    {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <p>No departures today.</p>
            {% endif %}
        </div>

        <div class="card">
            <h2>Arrivals ({{ airport }})</h2>
            {% if arr_error %}
                <p>{{ arr_error }}</p>
            {% elif arrivals %}
                <table>
                    <thead>
                    <tr>
                        <th>FLIGHT</th>
                        <th>FROM</th>
                        <th>TIME</th>
                        <th>STATUS</th>
                    </tr>
                    </thead>
                    <tbody>
                    {% for row in arrivals %}
                        <tr class="{{ row.status_class }}">
                            <td>{{ row.flight }}</td>
                            <td>{{ row.origin }}</td>
                            <td>{{ row.time }}</td>
                            <td>{{ row.status }}</td>
                        </tr>
                    {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <p>No arrivals today.</p>
            {% endif %}
        </div>
    </div>

    <div class="card">
        <h2>Emergency Squawks (OpenSky)</h2>
        {% if squawk_error %}
            <p>{{ squawk_error }}</p>
        {% elif squawks %}
            <table>
                <thead>
                <tr>
                    <th>CALLSIGN</th>
                    <th>SQUAWK</th>
                    <th>ICAO24</th>
                </tr>
                </thead>
                <tbody>
                {% for s in squawks %}
                    <tr>
                        <td>{{ s.callsign }}</td>
                        <td>{{ s.squawk }}</td>
                        <td>{{ s.icao24 }}</td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
        {% else %}
            <p>No emergencies detected.</p>
        {% endif %}
    </div>
</div>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    airport = request.args.get("airport", DEFAULT_AIRPORT_ICAO).upper() or DEFAULT_AIRPORT_ICAO

    # Weather
    metar = fetch_metar(airport)
    taf = fetch_taf(airport)

    # Append temperature & freezing emoji to METAR if possible
    temp = get_temperature_from_metar(metar)
    if temp is not None and "Error" not in metar:
        icon = " 🥶" if temp < 3 else ""
        metar = f"{metar}\n\nTemp: {temp}°C{icon}"

    # FIDS
    deps, arrs = fetch_fids(airport)
    if arrs is None:  # error in fetching FIDS
        departures = []
        arrivals = []
        dep_error = deps
        arr_error = deps
    else:
        departures, dep_error = build_departures_rows(deps)
        arrivals, arr_error = build_arrivals_rows(arrs, airport)

    # Squawks
    squawks_data = fetch_squawks()
    squawk_error = None
    squawks = []
    if isinstance(squawks_data, str):
        squawk_error = squawks_data
    else:
        squawks = squawks_data

    return render_template_string(
        TEMPLATE,
        airport=airport,
        metar=metar,
        taf=taf,
        departures=departures,
        dep_error=dep_error,
        arrivals=arrivals,
        arr_error=arr_error,
        squawks=squawks,
        squawk_error=squawk_error,
    )


if __name__ == "__main__":
    app.run(debug=True)
