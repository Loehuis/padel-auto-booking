from __future__ import annotations

import logging
import time as time_module
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .client import BookingError, EbusyClient, FlowStuckError, LoginError
from .config import AppConfig
from .notify import notify

logger = logging.getLogger("padel_booker.scheduler")

BERLIN_TZ = ZoneInfo("Europe/Berlin")
BOOKING_HORIZON_DAYS = 7


def _now() -> datetime:
    return datetime.now(BERLIN_TZ)


def _in_search_window(cfg: AppConfig, now: datetime) -> bool:
    if now.weekday() != cfg.target.weekday:
        return False
    return cfg.search_window.start_time <= now.time() <= cfg.search_window.end_time


def _target_date(now: datetime) -> datetime:
    return now + timedelta(days=BOOKING_HORIZON_DAYS)


def _state_file(cfg: AppConfig):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg.state_dir / "last_success.txt"


def _already_booked_today_run(cfg: AppConfig, run_key: str) -> bool:
    state_file = _state_file(cfg)
    if not state_file.exists():
        return False
    return state_file.read_text(encoding="utf-8").strip() == run_key


def _mark_booked(cfg: AppConfig, run_key: str) -> None:
    _state_file(cfg).write_text(run_key, encoding="utf-8")


def run_forever(cfg: AppConfig) -> None:
    client = EbusyClient(
        base_url=cfg.credentials.base_url,
        username=cfg.credentials.username,
        password=cfg.credentials.password,
    )

    logger.info(
        "Scheduler gestartet. Ziel: %s um %s (%d Min), Courts %s. Suchfenster %s-%s.",
        list(_WEEKDAY_NAMES)[cfg.target.weekday],
        cfg.target.time,
        cfg.target.duration_minutes,
        cfg.target.courts,
        cfg.search_window.start_time,
        cfg.search_window.end_time,
    )

    while True:
        now = _now()

        if not _in_search_window(cfg, now):
            time_module.sleep(cfg.polling.idle_check_seconds)
            continue

        target_date = _target_date(now)
        run_key = f"{target_date.date().isoformat()}_{cfg.target.time}"

        if _already_booked_today_run(cfg, run_key):
            time_module.sleep(cfg.polling.idle_check_seconds)
            continue

        try:
            success = attempt_booking(client, cfg, target_date, run_key)
        except LoginError as exc:
            logger.error("Login-Fehler: %s", exc)
            notify(
                cfg.notification,
                cfg.smtp,
                "Padel-Buchung: Login fehlgeschlagen",
                str(exc),
            )
            time_module.sleep(cfg.polling.idle_check_seconds)
            continue

        if success:
            time_module.sleep(cfg.polling.idle_check_seconds)
        else:
            time_module.sleep(cfg.polling.interval_seconds)


def attempt_booking(client: EbusyClient, cfg: AppConfig, target_date: datetime, run_key: str) -> bool:
    client.ensure_logged_in()

    slot = client.find_bookable_slot(
        target_date=target_date.date(),
        target_time=cfg.target.time,
        preferred_courts=cfg.target.courts,
    )
    if slot is None:
        logger.debug("Slot noch nicht buchbar (%s).", run_key)
        return False

    logger.info(
        "Freier Slot gefunden: Court %s, %s %s-%s. Versuche zu buchen...",
        slot.court,
        slot.date_us,
        slot.begin,
        slot.end,
    )

    try:
        result = client.book_slot(
            slot=slot,
            duration_minutes=cfg.target.duration_minutes,
            all_courts=cfg.target.courts,
        )
    except FlowStuckError as exc:
        logger.error("Buchungs-Flow feststeckt: %s", exc)
        notify(
            cfg.notification,
            cfg.smtp,
            "Padel-Buchung: Flow-Logik muss geprueft werden",
            f"{exc}\n\nSlot: Court {slot.court}, {slot.date_us} {slot.begin}-{slot.end}\n"
            "Der Buchungsdialog der Seite hat sich vermutlich geaendert. "
            "Bitte manuell im Browser buchen und das Skript anpassen.",
        )
        return False
    except BookingError as exc:
        logger.warning("Buchung abgelehnt: %s", exc.reason)
        notify(
            cfg.notification,
            cfg.smtp,
            "Padel-Buchung: fehlgeschlagen",
            f"Grund: {exc.reason}\nSlot: Court {slot.court}, {slot.date_us} "
            f"{slot.begin}-{slot.end}",
        )
        return False

    logger.info("Buchung erfolgreich: %s", result)
    _mark_booked(cfg, run_key)
    notify(
        cfg.notification,
        cfg.smtp,
        "Padel-Buchung: Erfolg!",
        f"Court {slot.court} am {slot.date_us}, {slot.begin}-{slot.end} Uhr gebucht.\n{result}",
    )
    return True


_WEEKDAY_NAMES = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
]
