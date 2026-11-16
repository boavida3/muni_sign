#!/usr/bin/env python3
"""muni_sign: real-time SF Muni arrivals on a 64x32 RGB LED panel driven by a Raspberry Pi.

Shows the next three trains of one line at one stop, using the 511.org real-time API.
The text rotates between Korean, Portuguese, Spanish and English. An optional short screen
can show where the code lives (GITHUB_USER, off by default).

Normally runs as the muni-board systemd service (see README.md):
    sudo systemctl restart muni-board     # after editing this file
    sudo systemctl stop muni-board        # before running anything else on the panel
Test without the panel (prints to the terminal instead; safe while the service runs):
    venv/bin/python muni_board.py --print

Private settings (511 API key, stop code) are read from .env next to this file, never from
the code. See .env.example.
Fonts: Galmuri pixel fonts (SIL Open Font License, see fonts/Galmuri-LICENSE.txt) and the
tom-thumb font that ships with rpi-rgb-led-matrix.
"""
import argparse
import collections
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---- Settings you might want to change ----------------------------------
LINE = "J"                   # only show this line (stop code comes from .env)
REFRESH_SECONDS = 75         # 511 allows 60 requests/hour per key; 75 s = 48/hour
LANGUAGE_SECONDS = 8         # how long each language stays on screen
ARROW = "left"               # direction arrow in the top row: "left", "right" or None
# GitHub screen, once per rotation: where the code lives.
GITHUB_USER = None           # e.g. "boavida3": adds a github.com/<user> screen; None = off
GITHUB_SECONDS = 5           # how long it shows (it hides the train times meanwhile)
GITHUB_SKIP_WITHIN = 1       # skip it while a train is this many minutes away or less
MAX_ARRIVALS = 3
BRIGHTNESS = 50              # 1-100
LINE_COLOR = (250, 160, 30)  # J Church orange
LABEL_COLOR = (255, 255, 255)
TIME_COLOR = (255, 170, 0)   # amber, like the Muni station signs
DIM_COLOR = (110, 110, 110)
DIVIDER_COLOR = (40, 40, 40)

# Every piece of text on the board, per language, in the order they rotate.
LANGUAGES = [
    {"code": "ko", "label": "시내 방향", "unit": "분", "loading": "불러오는 중",
     "no_data": "데이터 없음", "no_trains": "운행 없음", "key_error": "키 오류"},
    {"code": "pt", "label": "Centro", "unit": "min", "loading": "Carregando",
     "no_data": "Sem dados", "no_trains": "Sem trens", "key_error": "Erro de chave"},
    {"code": "es", "label": "Centro", "unit": "min", "loading": "Cargando",
     "no_data": "Sin datos", "no_trains": "Sin trenes", "key_error": "Error de clave"},
    {"code": "en", "label": "Downtown", "unit": "min", "loading": "Loading",
     "no_data": "No data", "no_trains": "No trains", "key_error": "Key error"},
]
# -------------------------------------------------------------------------

HOME = Path(__file__).resolve().parent      # the folder this file is in
ENV_FILE = HOME / ".env"
FONT_DIR = HOME / "fonts"
LIB_FONT_DIR = HOME / "rpi-rgb-led-matrix" / "fonts"
API_URL = "https://api.511.org/transit/StopMonitoring"
PANEL_W, PANEL_H = 64, 32
# Accuracy safeguards: people rely on this board, so show "no data" rather than doubtful times.
STALE_AFTER = 3 * 60          # seconds without a successful fetch before hiding the times
MAX_CLOCK_SKEW = 60           # seconds the Pi clock may differ from 511's server clock
VEHICLE_REPORT_MAX_AGE = 5 * 60  # a vehicle position older than this no longer counts as tracked
RATE_LIMIT_BACKOFF = 10 * 60  # wait this long after the API says we sent too many requests
HOURLY_REQUEST_BUDGET = 58    # stay under 511's limit of 60 requests/hour per key
PASSED_REFETCH_DELAY = 20     # seconds after a train's time passes before fetching again
BADGE = 11                    # square line badge (rows 0-10) with the line letter cut out
# Direction arrow, pointing left (mirrored automatically for ARROW = "right"), 8x9 px,
# drawn on rows 1-9 so it is centred on the badge ('#' = lit).
ARROW_LEFT = [
    "....#...",
    "...##...",
    "..###...",
    ".#######",
    "########",
    ".#######",
    "..###...",
    "...##...",
    "....#...",
]

# Baselines (y of the row just below the letters), measured from the glyphs:
BADGE_LETTER_BASELINE = 10    # Galmuri9 "J" inside the badge: rows 1-9
HEADER_BASELINE = 9           # Galmuri7 destination: rows 2-8, centred on the badge
# GitHub screen: github.com/<user> in the tiny tom-thumb font, two centred lines
# in capitals (one line is 76 px, wider than the panel): rows 10-14 and rows 18-22
GITHUB_BASELINES = (15, 23)
NUMBER_BASELINE = 24          # Galmuri11 digits: rows 13-23
UNIT_BASELINE = 32            # Galmuri7 "min"/"분": rows 25-31
STATUS_BASELINE = 24          # status messages, centred in the lower area


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def load_settings():
    """Read the 511 API key and the stop code from the private .env file."""
    values = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    name, value = line.split("=", 1)
                    values[name.strip()] = value.strip()
    except OSError as e:
        sys.exit(f"Cannot read {ENV_FILE}: {e} (copy .env.example to .env and fill it in)")
    key = values.get("API_511_KEY", "")
    if not key or key == "paste-your-key-here":
        sys.exit(f"API_511_KEY is not set in {ENV_FILE}")
    stop = values.get("STOP_CODE", "")
    if not stop.isdigit():
        sys.exit(f"STOP_CODE (the 5-digit Muni stop number) is not set in {ENV_FILE}")
    return key, stop


class ClockSkewError(Exception):
    """The Pi's clock disagrees with 511's server clock, so countdowns would be wrong."""


def parse_time(stamp):
    """511 timestamp ('2026-11-16T04:19:27Z') -> aware UTC datetime, or None."""
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_arrivals(key, stop_code):
    """Return upcoming arrivals for LINE at stop_code, sorted by time, as
    [{"time": UTC datetime, "tracked": bool}, ...].

    tracked=True only when the prediction comes from a vehicle that is actually being
    followed right now (a vehicle is assigned and its last position report is recent).
    Otherwise the time is a schedule-based estimate and is marked untracked.
    """
    query = urllib.parse.urlencode(
        {"api_key": key, "agency": "SF", "stopCode": stop_code, "format": "json"})
    with urllib.request.urlopen(f"{API_URL}?{query}", timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8-sig"))

    # The live API has no "Siri" wrapper, although the 2022 spec shows one: accept both.
    service = data.get("Siri", data)["ServiceDelivery"]

    # Countdowns are computed with the Pi's clock, so refuse to show times if it is off.
    server_now = parse_time(service.get("ResponseTimestamp"))
    if server_now:
        skew = abs((datetime.now(timezone.utc) - server_now).total_seconds())
        if skew > MAX_CLOCK_SKEW:
            raise ClockSkewError(f"Pi clock differs from 511 server by {skew:.0f}s")
    reference = server_now or datetime.now(timezone.utc)

    delivery = service["StopMonitoringDelivery"]
    if isinstance(delivery, list):
        delivery = delivery[0] if delivery else {}
    visits = delivery.get("MonitoredStopVisit") or []
    if isinstance(visits, dict):
        visits = [visits]

    arrivals = []
    for visit in visits:
        journey = visit.get("MonitoredVehicleJourney") or {}
        if journey.get("LineRef") != LINE:
            continue
        call = journey.get("MonitoredCall") or {}
        expected = parse_time(call.get("ExpectedArrivalTime"))
        when = expected or parse_time(call.get("AimedArrivalTime"))  # timetable fallback
        if not when:
            continue
        reported = parse_time(visit.get("RecordedAtTime"))  # 1970-01-01 when no vehicle yet
        tracked = bool(
            expected
            and journey.get("VehicleRef")
            and reported
            and (reference - reported).total_seconds() <= VEHICLE_REPORT_MAX_AGE
        )
        arrivals.append({"time": when, "tracked": tracked})
    return sorted(arrivals, key=lambda a: a["time"])


def upcoming(arrivals):
    """The next trains as [{"min": whole minutes, "tracked": bool}, ...].

    Minutes are rounded DOWN, like Muni's own signs ("3" means 3:00-3:59 away), so the
    board never says a train is further away than it is. 0 = arriving now.
    """
    now = datetime.now(timezone.utc)
    trains = []
    for a in arrivals:
        m = int((a["time"] - now).total_seconds() // 60)
        if m >= 0:
            trains.append({"min": m, "tracked": a["tracked"]})
    return trains[:MAX_ARRIVALS]


def rotation():
    """The screens to cycle through, as (screen, seconds): every language, then a short
    screen with the GitHub URL on its own (no train times)."""
    slots = [(lang, LANGUAGE_SECONDS) for lang in LANGUAGES]
    if GITHUB_USER:
        slots.append((dict(LANGUAGES[-1], label=f"github.com/{GITHUB_USER}", github=True),
                      GITHUB_SECONDS))
    return slots


def current_screen(slots, elapsed, trains):
    """Which screen to show `elapsed` seconds after start. The GitHub screen is replaced
    by the English one while a train is about to arrive, so nobody misses it."""
    t = elapsed % sum(seconds for _, seconds in slots)
    for screen, seconds in slots:
        if t < seconds:
            break
        t -= seconds
    if screen.get("github") and trains and trains[0]["min"] <= GITHUB_SKIP_WITHIN:
        return LANGUAGES[-1]
    return screen


def describe(lang, trains, status):
    """One-line text version of the board, for the --print terminal output.
    Untracked (schedule-only) times are shown with a leading ~."""
    label = lang["label"]
    if lang.get("github"):
        return f"{label} (GitHub screen, no times)"
    if status:
        return f"{label}: {lang[status]}"
    return f"{label}: " + "  ".join(
        f"{'' if t['tracked'] else '~'}{t['min']}{lang['unit']}" for t in trains)


class Panel:
    """Draws the board on the LED matrix."""

    def __init__(self):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

        self.g = graphics
        # Load fonts BEFORE creating the matrix: the library drops root privileges
        # when it starts and may then no longer be able to read files in your home folder.
        self.fonts = {}
        for name, path in (("Galmuri7", FONT_DIR / "Galmuri7.bdf"),
                           ("Galmuri9", FONT_DIR / "Galmuri9.bdf"),
                           ("Galmuri11", FONT_DIR / "Galmuri11.bdf"),
                           ("tom-thumb", LIB_FONT_DIR / "tom-thumb.bdf")):  # tiny GitHub text
            font = graphics.Font()
            font.LoadFont(str(path))
            self.fonts[name] = font

        # Settings for this panel (P5 64x32 1/8-scan on the Adafruit Bonnet)
        options = RGBMatrixOptions()
        options.rows = PANEL_H
        options.cols = PANEL_W
        options.hardware_mapping = "adafruit-hat"
        options.gpio_slowdown = 3
        options.multiplexing = 1
        options.brightness = BRIGHTNESS
        self.matrix = RGBMatrix(options=options)
        self.canvas = self.matrix.CreateFrameCanvas()

    def width(self, font, text):
        return sum(font.CharacterWidth(ord(ch)) for ch in text)

    def text(self, font_name, x, baseline, color, text):
        """Draw text whose letters sit on the row just above `baseline`."""
        # DrawText draws a glyph with height h and BDF y-offset 0 on rows y-h .. y-1,
        # so passing the "row just below the letters" lines everything up exactly.
        self.g.DrawText(self.canvas, self.fonts[font_name], x, baseline, self.g.Color(*color), text)

    def centred(self, font_name, left, width, baseline, color, text):
        x = left + (width - self.width(self.fonts[font_name], text)) // 2
        self.text(font_name, x, baseline, color, text)

    def bitmap(self, x0, y0, rows, color, mirror=False):
        """Draw a small picture given as strings of '#' (lit) and '.' (off)."""
        for y, row in enumerate(rows):
            for x, cell in enumerate(row[::-1] if mirror else row):
                if cell == "#":
                    self.canvas.SetPixel(x0 + x, y0 + y, *color)

    def draw_header(self, lang):
        """Rows 0-10: [<-] [J] destination, or [J] destination [->], or no arrow."""
        badge_x = 0
        if ARROW == "left":                        # arrow at the left edge, then the badge
            self.bitmap(0, 1, ARROW_LEFT, LINE_COLOR)
            badge_x = len(ARROW_LEFT[0]) + 2
        elif ARROW == "right":                     # arrow at the right edge
            self.bitmap(PANEL_W - len(ARROW_LEFT[0]), 1, ARROW_LEFT, LINE_COLOR, mirror=True)
        self.draw_badge(badge_x)
        self.text("Galmuri7", badge_x + BADGE + 2, HEADER_BASELINE, LABEL_COLOR, lang["label"])

    def draw_badge(self, badge_x):
        """Square badge in the line colour with the letter cut out in black."""
        for y in range(BADGE):
            for x in range(badge_x, badge_x + BADGE):
                self.canvas.SetPixel(x, y, *LINE_COLOR)
        self.centred("Galmuri9", badge_x, BADGE + 1, BADGE_LETTER_BASELINE, (0, 0, 0), LINE)

    def draw_github(self):
        """The GitHub screen: GITHUB.COM/ and the username, tiny and centred. Capitals use
        the font's full 5-px height, so they read better than lowercase on the LEDs (the
        address works either way: domains and GitHub usernames are case-insensitive)."""
        for baseline, line in zip(GITHUB_BASELINES, ("github.com/", GITHUB_USER)):
            self.centred("tom-thumb", 0, PANEL_W, baseline, LINE_COLOR, line.upper())

    def draw(self, lang, trains, status):
        mins = [t["min"] for t in trains]
        c = self.canvas
        c.Clear()

        if lang.get("github"):
            self.draw_github()
            self.canvas = self.matrix.SwapOnVSync(c)
            return

        self.draw_header(lang)

        if status:
            message = lang[status]
            font = "Galmuri9" if self.width(self.fonts["Galmuri9"], message) <= PANEL_W else "Galmuri7"
            self.centred(font, 0, PANEL_W, STATUS_BASELINE, DIM_COLOR, message)
        else:
            self.draw_minutes(lang, mins)

        self.canvas = self.matrix.SwapOnVSync(c)

    def draw_minutes(self, lang, mins):
        """One column per arrival: the number on top, the unit underneath,
        with faint divider lines between the columns."""
        col_w = PANEL_W // MAX_ARRIVALS                    # 21 px per column
        for i in range(1, MAX_ARRIVALS):
            for y in range(13, PANEL_H - 1):
                self.canvas.SetPixel(i * col_w, y, *DIVIDER_COLOR)
        for i, m in enumerate(mins[:MAX_ARRIVALS]):
            left = i * col_w
            number = str(m)
            font = "Galmuri11" if self.width(self.fonts["Galmuri11"], number) < col_w else "Galmuri9"
            self.centred(font, left + 1, col_w, NUMBER_BASELINE, TIME_COLOR, number)
            self.centred("Galmuri7", left + 1, col_w, UNIT_BASELINE, DIM_COLOR, lang["unit"])

    def clear(self):
        self.matrix.Clear()


def main():
    parser = argparse.ArgumentParser(description="Muni arrival board")
    parser.add_argument("--print", action="store_true",
                        help="print to the terminal instead of using the LED panel")
    args = parser.parse_args()

    key, stop_code = load_settings()         # read .env before the panel drops privileges
    panel = None if args.print else Panel()
    slots = rotation()

    arrivals, last_ok, error = [], None, None
    next_fetch, shown = 0.0, None
    shown_after_fetch = 0        # how many trains were on the board right after the last fetch
    fetch_times = collections.deque()  # monotonic times of requests in the last hour
    started = time.monotonic()
    log(f"Watching stop {stop_code}, line {LINE}; refreshing every {REFRESH_SECONDS}s")

    try:
        while True:
            now = time.monotonic()

            # 511 only sends the next 3 trains. When one of them passes, the board would show
            # just 2 until the next regular fetch, so fetch again shortly after a train passes;
            # that also confirms whether it really left.
            if len(upcoming(arrivals)) < shown_after_fetch:
                next_fetch = min(next_fetch, now + PASSED_REFETCH_DELAY)
                shown_after_fetch = len(upcoming(arrivals))

            # Never go over 511's hourly limit, whatever triggered the fetch.
            while fetch_times and now - fetch_times[0] > 3600:
                fetch_times.popleft()
            if now >= next_fetch and len(fetch_times) >= HOURLY_REQUEST_BUDGET:
                next_fetch = fetch_times[0] + 3600 + 1
                log("Hourly request budget used up; waiting before the next fetch")

            if now >= next_fetch:
                next_fetch = now + REFRESH_SECONDS
                fetch_times.append(now)
                try:
                    arrivals = fetch_arrivals(key, stop_code)
                    last_ok, error = now, None
                    shown_after_fetch = len(upcoming(arrivals))
                    log(f"Fetched {len(arrivals)} arrivals")
                except urllib.error.HTTPError as e:   # str(e) does not include the URL/key
                    error = "key_error" if e.code in (401, 403) else "no_data"
                    if e.code == 429:
                        next_fetch = now + RATE_LIMIT_BACKOFF
                    log(f"511 API error: HTTP {e.code}")
                except ClockSkewError as e:
                    # Countdowns would be wrong: drop everything and show "no data" at once.
                    arrivals, last_ok, error = [], None, "no_data"
                    log(f"Not showing times: {e}")
                except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
                    error = "no_data"
                    log(f"Fetch failed: {type(e).__name__}: {e}")

            trains = upcoming(arrivals)
            if last_ok is None:
                status = error or "loading"
            elif now - last_ok > STALE_AFTER:
                status = error or "no_data"   # old data could hide a delay: don't show it
            elif not trains:
                status = "no_trains"
            else:
                status = None

            lang = current_screen(slots, now - started, trains)
            if args.print:
                current = describe(lang, trains, status)
                if current != shown:
                    log(f"[{LINE}] {current}")
                    shown = current
            else:
                panel.draw(lang, trains, status)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if panel:
            panel.clear()


if __name__ == "__main__":
    main()
