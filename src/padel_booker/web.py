from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import config_io

CONFIG_PATH = os.environ.get("PADEL_CONFIG_PATH", "config.yaml")
BERLIN_TZ = ZoneInfo("Europe/Berlin")
BOOKING_HORIZON_DAYS = 7

app = FastAPI(title="Padel Booking Config")
security = HTTPBasic()

# Order matters here: index 0..6 matches Python's date.weekday() (Monday=0).
WEEKDAY_LABELS = {
    "monday": "Montag",
    "tuesday": "Dienstag",
    "wednesday": "Mittwoch",
    "thursday": "Donnerstag",
    "friday": "Freitag",
    "saturday": "Samstag",
    "sunday": "Sonntag",
}


def _next_target_date(weekday_index: int, today):
    """Next calendar date on/after today matching weekday_index, plus the
    booking horizon - i.e. the actual date that would be searched for once
    this weekday rule next becomes active."""
    days_ahead = (weekday_index - today.weekday()) % 7
    next_occurrence = today + timedelta(days=days_ahead)
    return next_occurrence + timedelta(days=BOOKING_HORIZON_DAYS)


def _check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    expected_user = os.environ.get("UI_USERNAME", "")
    expected_pass = os.environ.get("UI_PASSWORD", "")
    if not expected_user or not expected_pass:
        raise HTTPException(500, "UI_USERNAME/UI_PASSWORD nicht konfiguriert (.env pruefen).")
    user_ok = secrets.compare_digest(credentials.username, expected_user)
    pass_ok = secrets.compare_digest(credentials.password, expected_pass)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Falsche Zugangsdaten",
            headers={"WWW-Authenticate": "Basic"},
        )


def _render(form: dict, message: str | None = None, error: str | None = None) -> str:
    today = datetime.now(BERLIN_TZ).date()
    weekday_options = "".join(
        f'<option value="{w}" {"selected" if form["weekday"] == w else ""}>'
        f'{label} (→ bucht {_next_target_date(idx, today).strftime("%d.%m.%Y")})</option>'
        for idx, (w, label) in enumerate(WEEKDAY_LABELS.items())
    )
    court1_checked = "checked" if 1 in form["courts"] else ""
    court2_checked = "checked" if 2 in form["courts"] else ""
    email_checked = "checked" if form["email_enabled"] else ""
    banner = ""
    if message:
        banner = f'<div class="banner ok">{message}</div>'
    if error:
        banner = f'<div class="banner err">{error}</div>'

    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Padel Booking</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 480px;
          margin: 2rem auto; padding: 0 1rem; background:#0b0f14; color:#e6edf3; }}
  h1 {{ font-size: 1.3rem; }}
  label {{ display:block; margin-top: 1rem; font-weight: 600; }}
  input, select {{ width: 100%; padding: 0.5rem; margin-top: 0.25rem; border-radius: 6px;
                    border: 1px solid #333; background:#151b23; color:#e6edf3; box-sizing: border-box; }}
  .row {{ display:flex; gap:1rem; }}
  .row > div {{ flex:1; }}
  .checks label {{ display:inline-flex; align-items:center; gap:0.4rem; font-weight:400; margin-right:1rem; }}
  .checks input {{ width:auto; }}
  button {{ margin-top:1.5rem; width:100%; padding:0.75rem; background:#2f81f7; color:white;
            border:none; border-radius:6px; font-size:1rem; cursor:pointer; }}
  .banner {{ padding:0.75rem; border-radius:6px; margin-bottom:1rem; }}
  .banner.ok {{ background:#123d24; color:#7ee2a8; }}
  .banner.err {{ background:#3d1212; color:#ff9b9b; }}
  fieldset {{ border:1px solid #333; border-radius:6px; margin-top:1.5rem; }}
  legend {{ padding:0 0.5rem; color:#9aa7b2; }}
  small {{ color:#9aa7b2; }}
</style></head>
<body>
<h1>&#127954; Padel-Buchung: Ziel-Slot</h1>
{banner}
<form method="post">
  <label>Wochentag</label>
  <select name="weekday">{weekday_options}</select>

  <div class="row">
    <div><label>Uhrzeit</label><input type="time" name="time" value="{form['time']}" required></div>
    <div><label>Dauer (Min)</label><input type="number" name="duration_minutes" min="1" max="90" value="{form['duration_minutes']}" required></div>
  </div>

  <label>Courts</label>
  <div class="checks">
    <label><input type="checkbox" name="court_1" {court1_checked}> Court 1</label>
    <label><input type="checkbox" name="court_2" {court2_checked}> Court 2</label>
  </div>

  <div class="row">
    <div><label>Suchfenster von</label><input type="time" name="search_start" value="{form['search_start']}" required></div>
    <div><label>bis</label><input type="time" name="search_end" value="{form['search_end']}" required></div>
  </div>
  <small>Der Poller läuft nur innerhalb dieses Fensters, am selben Wochentag wie oben gewählt.</small>

  <fieldset>
    <legend>Benachrichtigung</legend>
    <label><input type="checkbox" name="email_enabled" {email_checked}> E-Mail bei Erfolg/Fehlschlag</label>
    <label>E-Mail-Adresse</label>
    <input type="email" name="email_to" value="{form['email_to']}">
  </fieldset>

  <fieldset>
    <legend>Erweitert</legend>
    <div class="row">
      <div><label>Poll-Intervall (Sek)</label><input type="number" step="0.5" min="1" name="interval_seconds" value="{form['interval_seconds']}"></div>
      <div><label>Ruhe-Intervall (Sek)</label><input type="number" step="1" min="5" name="idle_check_seconds" value="{form['idle_check_seconds']}"></div>
    </div>
  </fieldset>

  <button type="submit">Speichern</button>
</form>
<p><small>Änderungen werden vom laufenden Dienst automatisch innerhalb weniger Sekunden übernommen, kein Neustart nötig.</small></p>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index(_: None = Depends(_check_auth)) -> str:
    form = config_io.to_form_dict(CONFIG_PATH)
    return _render(form)


@app.post("/", response_class=HTMLResponse)
def save(
    _: None = Depends(_check_auth),
    weekday: str = Form(...),
    time: str = Form(...),
    duration_minutes: int = Form(...),
    court_1: str | None = Form(None),
    court_2: str | None = Form(None),
    search_start: str = Form(...),
    search_end: str = Form(...),
    interval_seconds: float = Form(...),
    idle_check_seconds: float = Form(...),
    email_enabled: str | None = Form(None),
    email_to: str = Form(""),
) -> str:
    courts = [c for c, checked in [(1, court_1), (2, court_2)] if checked is not None]
    submitted = {
        "weekday": weekday,
        "time": time,
        "duration_minutes": duration_minutes,
        "courts": courts,
        "search_start": search_start,
        "search_end": search_end,
        "interval_seconds": interval_seconds,
        "idle_check_seconds": idle_check_seconds,
        "email_enabled": email_enabled is not None,
        "email_to": email_to,
    }
    try:
        config_io.update_config(
            CONFIG_PATH,
            weekday=weekday,
            time_str=time,
            duration_minutes=duration_minutes,
            courts=courts,
            search_start=search_start,
            search_end=search_end,
            interval_seconds=interval_seconds,
            idle_check_seconds=idle_check_seconds,
            email_enabled=submitted["email_enabled"],
            email_to=email_to,
        )
    except ValueError as exc:
        return _render(submitted, error=str(exc))

    form = config_io.to_form_dict(CONFIG_PATH)
    return _render(
        form,
        message="Gespeichert. Der laufende Dienst übernimmt die Änderung automatisch.",
    )
