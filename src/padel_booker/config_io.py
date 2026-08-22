from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def load_raw(config_path: str | Path) -> Any:
    with open(config_path, "r", encoding="utf-8") as f:
        return _yaml.load(f)


def save_raw(config_path: str | Path, data: Any) -> None:
    with open(config_path, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)


def to_form_dict(config_path: str | Path) -> dict:
    data = load_raw(config_path)
    polling = data.get("polling", {}) or {}
    notification = data.get("notification", {}) or {}
    return {
        "weekday": data["target"]["weekday"],
        "time": data["target"]["time"],
        "duration_minutes": int(data["target"]["duration_minutes"]),
        "courts": [int(c) for c in data["target"]["courts"]],
        "search_start": data["search_window"]["start_time"],
        "search_end": data["search_window"]["end_time"],
        "interval_seconds": float(polling.get("interval_seconds", 3)),
        "idle_check_seconds": float(polling.get("idle_check_seconds", 30)),
        "email_enabled": bool(notification.get("email_enabled", False)),
        "email_to": notification.get("email_to") or "",
    }


def update_config(
    config_path: str | Path,
    *,
    weekday: str,
    time_str: str,
    duration_minutes: int,
    courts: list[int],
    search_start: str,
    search_end: str,
    interval_seconds: float,
    idle_check_seconds: float,
    email_enabled: bool,
    email_to: str,
) -> None:
    if weekday not in WEEKDAYS:
        raise ValueError(f"Ungueltiger Wochentag: {weekday}")
    _validate_time(time_str, "Uhrzeit")
    _validate_time(search_start, "Suchfenster-Start")
    _validate_time(search_end, "Suchfenster-Ende")
    if not (1 <= duration_minutes <= 90):
        raise ValueError("Dauer muss zwischen 1 und 90 Minuten liegen (Platzregel).")
    if not courts:
        raise ValueError("Mindestens ein Court muss ausgewaehlt sein.")
    if interval_seconds < 1:
        raise ValueError("Poll-Intervall muss mindestens 1 Sekunde sein (Fair Use).")
    if idle_check_seconds < 5:
        raise ValueError("Ruhe-Intervall muss mindestens 5 Sekunden sein.")
    if email_enabled and not email_to.strip():
        raise ValueError("E-Mail-Adresse fehlt, obwohl Benachrichtigung aktiviert ist.")

    data = load_raw(config_path)
    data["target"]["weekday"] = weekday
    data["target"]["time"] = time_str
    data["target"]["duration_minutes"] = duration_minutes
    data["target"]["courts"] = courts
    data["search_window"]["start_time"] = search_start
    data["search_window"]["end_time"] = search_end
    data.setdefault("polling", {})
    data["polling"]["interval_seconds"] = interval_seconds
    data["polling"]["idle_check_seconds"] = idle_check_seconds
    data.setdefault("notification", {})
    data["notification"]["email_enabled"] = email_enabled
    data["notification"]["email_to"] = email_to.strip()
    save_raw(config_path, data)


def _validate_time(value: str, label: str) -> None:
    if not _TIME_RE.match(value):
        raise ValueError(f"Ungueltiges Zeitformat bei {label}: '{value}' (erwartet HH:MM)")
