from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dt_time
from pathlib import Path

import yaml
from dotenv import load_dotenv

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _parse_time(value: str) -> dt_time:
    hour, minute = value.strip().split(":")
    return dt_time(hour=int(hour), minute=int(minute))


def _parse_weekday(value: str) -> int:
    key = value.strip().lower()
    if key not in WEEKDAYS:
        raise ValueError(
            f"Ungueltiger Wochentag '{value}'. Erlaubt: {', '.join(WEEKDAYS)}"
        )
    return WEEKDAYS[key]


@dataclass
class TargetConfig:
    weekday: int
    time: dt_time
    duration_minutes: int
    courts: list[int]


@dataclass
class SearchWindowConfig:
    start_time: dt_time
    end_time: dt_time


@dataclass
class PollingConfig:
    interval_seconds: float
    idle_check_seconds: float
    max_other_rejections: int


@dataclass
class NotificationConfig:
    email_enabled: bool
    email_to: str


@dataclass
class Credentials:
    base_url: str
    username: str
    password: str


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    from_addr: str
    use_tls: bool


@dataclass
class AppConfig:
    target: TargetConfig
    search_window: SearchWindowConfig
    polling: PollingConfig
    notification: NotificationConfig
    credentials: Credentials
    smtp: SmtpConfig
    state_dir: Path = field(default_factory=lambda: Path.home() / ".padel-booker")


def load_config(config_path: str | Path = "config.yaml", env_path: str | Path | None = None) -> AppConfig:
    load_dotenv(dotenv_path=env_path)

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    target_raw = raw["target"]
    target = TargetConfig(
        weekday=_parse_weekday(target_raw["weekday"]),
        time=_parse_time(target_raw["time"]),
        duration_minutes=int(target_raw.get("duration_minutes", 90)),
        courts=[int(c) for c in target_raw.get("courts", [1])],
    )

    window_raw = raw["search_window"]
    search_window = SearchWindowConfig(
        start_time=_parse_time(window_raw["start_time"]),
        end_time=_parse_time(window_raw["end_time"]),
    )

    polling_raw = raw.get("polling", {})
    polling = PollingConfig(
        interval_seconds=float(polling_raw.get("interval_seconds", 3)),
        idle_check_seconds=float(polling_raw.get("idle_check_seconds", 30)),
        max_other_rejections=int(polling_raw.get("max_other_rejections", 5)),
    )

    notif_raw = raw.get("notification", {})
    notification = NotificationConfig(
        email_enabled=bool(notif_raw.get("email_enabled", False)),
        email_to=notif_raw.get("email_to") or os.environ.get("NOTIFY_EMAIL_TO", ""),
    )

    credentials = Credentials(
        base_url=os.environ.get("EBUSY_BASE_URL", "https://bsbb.ebusy.de").rstrip("/"),
        username=_require_env("EBUSY_USERNAME"),
        password=_require_env("EBUSY_PASSWORD"),
    )

    smtp = SmtpConfig(
        host=os.environ.get("SMTP_HOST", ""),
        port=int(os.environ.get("SMTP_PORT", "587")),
        user=os.environ.get("SMTP_USER", ""),
        password=os.environ.get("SMTP_PASSWORD", ""),
        from_addr=os.environ.get("SMTP_FROM", ""),
        use_tls=os.environ.get("SMTP_USE_TLS", "true").lower() != "false",
    )

    state_dir_raw = raw.get("state_dir")
    state_dir = Path(state_dir_raw).expanduser() if state_dir_raw else Path.home() / ".padel-booker"

    return AppConfig(
        target=target,
        search_window=search_window,
        polling=polling,
        notification=notification,
        credentials=credentials,
        smtp=smtp,
        state_dir=state_dir,
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Umgebungsvariable {name} fehlt. Bitte .env anlegen (siehe .env.example)."
        )
    return value
