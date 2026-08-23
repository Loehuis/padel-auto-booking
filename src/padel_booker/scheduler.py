from __future__ import annotations

import logging
import time as time_module
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .client import BookingError, EbusyClient, FlowStuckError, LoginError, add_minutes
from .config import AppConfig, load_config
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


def _log_unlock_observation(cfg: AppConfig, run_key: str, now: datetime, outcome: str) -> None:
    """Appends a one-line record of when a run_key was first observed as
    bookable at all (regardless of whether the booking itself then succeeded),
    to build empirical data on the actual unlock time across weeks."""
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    log_file = cfg.state_dir / "unlock_observations.csv"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"{now.isoformat()},{run_key},{outcome}\n")


def run_forever(cfg: AppConfig, config_path: str = "config.yaml") -> None:
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

    # Per (run_key, court) rejection counters, so one court's confirmed
    # collision doesn't stop retrying another court that's merely "not yet"
    # (e.g. still hitting the 7-day-advance check moments before the real
    # unlock). A run_key is only fully given up on once every configured
    # court has individually crossed its own threshold.
    rejection_counts: dict[tuple[str, int], int] = {}
    gave_up_courts: dict[str, set[int]] = {}
    gave_up_run_keys: set[str] = set()
    logged_unlock_keys: set[str] = set()

    while True:
        try:
            cfg = load_config(config_path)
        except Exception:
            logger.exception(
                "Konnte config.yaml nicht neu einlesen - verwende vorherigen Stand weiter."
            )

        now = _now()

        if not _in_search_window(cfg, now):
            time_module.sleep(cfg.polling.idle_check_seconds)
            continue

        target_date = _target_date(now)
        run_key = f"{target_date.date().isoformat()}_{cfg.target.time}"

        if _already_booked_today_run(cfg, run_key) or run_key in gave_up_run_keys:
            time_module.sleep(cfg.polling.idle_check_seconds)
            continue

        active_courts = [
            c for c in cfg.target.courts if c not in gave_up_courts.get(run_key, set())
        ]

        try:
            outcome, court_errors = attempt_booking(
                client, cfg, target_date, run_key, active_courts
            )
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

        if outcome != "not_yet" and run_key not in logged_unlock_keys:
            logged_unlock_keys.add(run_key)
            _log_unlock_observation(cfg, run_key, now, outcome)

        if outcome == "success":
            time_module.sleep(cfg.polling.idle_check_seconds)
        elif outcome == "not_yet":
            time_module.sleep(cfg.polling.interval_seconds)
        elif outcome == "rejected":
            newly_given_up = []
            for court, exc in court_errors.items():
                if exc.is_too_early:
                    continue  # never counts - just means "not yet", keep retrying
                key = (run_key, court)
                rejection_counts[key] = rejection_counts.get(key, 0) + 1
                threshold = (
                    cfg.polling.max_collision_rejections
                    if exc.is_collision
                    else cfg.polling.max_other_rejections
                )
                if rejection_counts[key] >= threshold:
                    logger.warning(
                        "Court %s fuer %s war %d mal in Folge abgelehnt (%s) - "
                        "gebe diesen Court fuer diese Woche auf.",
                        court,
                        run_key,
                        rejection_counts[key],
                        "bestaetigte Kollision" if exc.is_collision else "unklarer Grund",
                    )
                    newly_given_up.append(court)

            if newly_given_up:
                gave_up_courts.setdefault(run_key, set()).update(newly_given_up)

            remaining_courts = [
                c for c in cfg.target.courts if c not in gave_up_courts.get(run_key, set())
            ]
            if not remaining_courts:
                logger.warning(
                    "Alle Courts fuer %s aufgegeben - versuche es naechste Woche wieder.",
                    run_key,
                )
                notify(
                    cfg.notification,
                    cfg.smtp,
                    "Padel-Buchung: fuer diese Woche aufgegeben",
                    f"Slot fuer {run_key}: alle konfigurierten Courts einzeln "
                    "belegt/abgelehnt. Versuche es naechste Woche automatisch wieder.",
                )
                gave_up_run_keys.add(run_key)
                time_module.sleep(cfg.polling.idle_check_seconds)
            else:
                time_module.sleep(cfg.polling.interval_seconds)
        else:  # "error" - structural problem, already notified inside attempt_booking
            time_module.sleep(cfg.polling.idle_check_seconds)


def attempt_booking(
    client: EbusyClient,
    cfg: AppConfig,
    target_date: datetime,
    run_key: str,
    active_courts: list[int],
) -> tuple[str, dict[int, BookingError]]:
    """Returns (outcome, court_errors).

    outcome is one of "success", "not_yet", "rejected", "error". court_errors
    maps each attempted-and-rejected court to its BookingError, so the caller
    can maintain a per-court backoff counter (a confirmed collision on one
    court shouldn't stop retrying another court that's merely "not yet"). If
    every active court's rejection was a "too early" (7-day-rule) response -
    the calendar can mark a cell bookable slightly before the site's own
    submission check actually allows it - outcome is "not_yet" rather than
    "rejected", since that's not a real dead end.
    """
    client.ensure_logged_in()

    slots = client.find_bookable_slots(
        target_date=target_date.date(),
        target_time=cfg.target.time,
        preferred_courts=active_courts,
    )
    if not slots:
        logger.debug("Slot noch nicht buchbar (%s).", run_key)
        return "not_yet", {}

    court_errors: dict[int, BookingError] = {}
    for slot in slots:
        requested_end = add_minutes(slot.begin, cfg.target.duration_minutes)
        logger.info(
            "Freier Slot gefunden: Court %s, %s %s-%s (angefragte Dauer: %d Min). "
            "Versuche zu buchen...",
            slot.court,
            slot.date_us,
            slot.begin,
            requested_end,
            cfg.target.duration_minutes,
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
                f"{exc}\n\nSlot: Court {slot.court}, {slot.date_us} {slot.begin}-{requested_end}\n"
                "Der Buchungsdialog der Seite hat sich vermutlich geaendert. "
                "Bitte manuell im Browser buchen und das Skript anpassen.",
            )
            return "error", {}
        except BookingError as exc:
            logger.warning(
                "Buchung fuer Court %s abgelehnt: %s", slot.court, exc.reason
            )
            court_errors[slot.court] = exc
            continue

        logger.info("Buchung erfolgreich: %s", result)
        _mark_booked(cfg, run_key)
        notify(
            cfg.notification,
            cfg.smtp,
            "Padel-Buchung: Erfolg!",
            f"Court {slot.court} am {slot.date_us}, {slot.begin}-{requested_end} Uhr gebucht.\n{result}",
        )
        return "success", {}

    if all(exc.is_too_early for exc in court_errors.values()):
        logger.debug(
            "Alle versuchten Courts fuer %s noch zu frueh (7-Tage-Regel) - "
            "kein echter Ablehnungsgrund, poll weiter.",
            run_key,
        )
        return "not_yet", {}

    logger.warning(
        "Court(s) %s fuer %s abgelehnt.",
        list(court_errors.keys()),
        run_key,
    )
    return "rejected", court_errors


_WEEKDAY_NAMES = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
]
