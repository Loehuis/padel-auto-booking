from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.padel_booker.client import BookingError, EbusyClient, FlowStuckError
from src.padel_booker.config import load_config
from src.padel_booker.scheduler import BOOKING_HORIZON_DAYS, run_forever

BERLIN_TZ = ZoneInfo("Europe/Berlin")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    run_forever(cfg, config_path=args.config)
    return 0


def cmd_serve_ui(args: argparse.Namespace) -> int:
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("UI_USERNAME") or not os.environ.get("UI_PASSWORD"):
        print(
            "UI_USERNAME/UI_PASSWORD fehlen in .env - bitte setzen, bevor die "
            "Weboberflaeche gestartet wird (siehe .env.example)."
        )
        return 1

    os.environ["PADEL_CONFIG_PATH"] = args.config
    uvicorn.run("src.padel_booker.web:app", host="0.0.0.0", port=args.port, log_level="info")
    return 0


def cmd_test_login(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = EbusyClient(
        cfg.credentials.base_url, cfg.credentials.username, cfg.credentials.password
    )
    client.login()
    print("Login OK.")
    return 0


def cmd_find_slot(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = EbusyClient(
        cfg.credentials.base_url, cfg.credentials.username, cfg.credentials.password
    )
    client.login()

    target_date = datetime.now(BERLIN_TZ) + timedelta(days=BOOKING_HORIZON_DAYS)
    slot = client.find_bookable_slot(
        target_date=target_date.date(),
        target_time=cfg.target.time,
        preferred_courts=cfg.target.courts,
    )
    if slot is None:
        print(
            f"Kein buchbarer Slot fuer {target_date.date()} {cfg.target.time} "
            f"(Courts {cfg.target.courts}) gefunden."
        )
        return 1

    print(f"Buchbarer Slot gefunden: Court {slot.court}, {slot.date_us} {slot.begin}-{slot.end}")
    return 0


def cmd_test_booking(args: argparse.Namespace) -> int:
    """Manuelle einmalige Verifikation des echten Buchungs-Flows.

    Bucht WIRKLICH, wenn ein passender Slot frei ist und --confirm gesetzt
    ist. Ohne --confirm wird nur bis zur letzten Bestaetigungsseite
    gegangen und der/die erkannte(n) Event-Kandidat(en) angezeigt, OHNE
    final abzuschicken.
    """
    cfg = load_config(args.config)
    client = EbusyClient(
        cfg.credentials.base_url, cfg.credentials.username, cfg.credentials.password
    )
    client.login()

    target_date = datetime.now(BERLIN_TZ) + timedelta(days=BOOKING_HORIZON_DAYS)
    slot = client.find_bookable_slot(
        target_date=target_date.date(),
        target_time=cfg.target.time,
        preferred_courts=cfg.target.courts,
    )
    if slot is None:
        print("Kein passender freier Slot gefunden - Test kann jetzt nicht laufen.")
        return 1

    print(f"Gefundener Slot: Court {slot.court}, {slot.date_us} {slot.begin}-{slot.end}")

    if not args.confirm:
        print(
            "Kein --confirm angegeben: Es wird NICHT wirklich gebucht.\n"
            "Fuehre 'python main.py test-booking --confirm' aus, um diesen "
            "Slot testweise WIRKLICH zu buchen (verbraucht deine Vorausbuchung!)."
        )
        return 0

    answer = input(
        f"Slot Court {slot.court}, {slot.date_us} {slot.begin}-{slot.end} JETZT WIRKLICH "
        "buchen? Tippe 'ja' zum Bestaetigen: "
    )
    if answer.strip().lower() != "ja":
        print("Abgebrochen.")
        return 0

    try:
        result = client.book_slot(
            slot=slot,
            duration_minutes=cfg.target.duration_minutes,
            all_courts=cfg.target.courts,
        )
    except FlowStuckError as exc:
        print(f"Flow feststeckt: {exc}")
        print(
            "-> Bitte den in der Fehlermeldung enthaltenen HTML-Ausschnitt bzw. "
            "die Seite manuell im Browser pruefen, dann webflow.py/client.py anpassen."
        )
        return 2
    except BookingError as exc:
        print(f"Buchung abgelehnt: {exc.reason}")
        return 2

    print(f"Erfolg: {result}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Padel-Auto-Booking fuer bsbb.ebusy.de")
    parser.add_argument("--config", default="config.yaml", help="Pfad zur config.yaml")
    parser.add_argument("--verbose", action="store_true", help="Debug-Logging")

    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Dauerhaft laufender Scheduler (Standard-Betriebsmodus)")
    p_run.set_defaults(func=cmd_run)

    p_login = sub.add_parser("test-login", help="Nur Login testen")
    p_login.set_defaults(func=cmd_test_login)

    p_find = sub.add_parser(
        "find-slot", help="Prueft einmalig, ob der Ziel-Slot (heute+7 Tage) buchbar ist"
    )
    p_find.set_defaults(func=cmd_find_slot)

    p_test = sub.add_parser(
        "test-booking",
        help="Einmalige manuelle Verifikation des echten Buchungs-Flows (siehe README)",
    )
    p_test.add_argument(
        "--confirm", action="store_true", help="Tatsaechlich buchen (sonst nur Trockenlauf)"
    )
    p_test.set_defaults(func=cmd_test_booking)

    p_ui = sub.add_parser(
        "serve-ui", help="Startet die Web-Oberflaeche zum Anpassen des Ziel-Slots"
    )
    p_ui.add_argument("--port", type=int, default=8080, help="Port fuer die Weboberflaeche")
    p_ui.set_defaults(func=cmd_serve_ui)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
