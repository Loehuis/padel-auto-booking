from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, time

import requests
from bs4 import BeautifulSoup

from .webflow import parse_step

logger = logging.getLogger("padel_booker.client")

PADEL_MODULE_ID = 4
MAX_FLOW_STEPS = 6

# Exact text confirmed live for "someone already holds this slot" rejections
# (as opposed to e.g. the 7-day-advance-limit message, which reads
# differently). Used to classify a BookingError so the scheduler can back off
# quickly on a confirmed collision without needing repeated confirmation.
_COLLISION_MARKER = "konflikt mit einem bestehenden termin"


class LoginError(RuntimeError):
    pass


class BookingError(RuntimeError):
    def __init__(self, reason: str, html: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.html = html
        self.is_collision = _COLLISION_MARKER in reason.lower()


class FlowStuckError(BookingError):
    pass


@dataclass
class Slot:
    court: int
    date_us: str
    begin: str


class EbusyClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "padel-auto-booking/1.0 (+privates Automatisierungsskript)"}
        )
        self._csrf_token: str | None = None
        self._csrf_header: str = "X-CSRF-TOKEN"
        self._csrf_param: str = "_csrf"

    # ------------------------------------------------------------------ #
    # Login / CSRF
    # ------------------------------------------------------------------ #

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _read_csrf_from_html(self, html: str) -> None:
        soup = BeautifulSoup(html, "html.parser")

        def meta(name: str) -> str | None:
            tag = soup.find("meta", attrs={"name": name})
            return tag.get("content") if tag else None

        token = meta("_csrf")
        header = meta("_csrf_header")
        param = meta("_csrf_parameter")

        if not token:
            hidden = soup.find("input", attrs={"name": "_csrf"})
            token = hidden.get("value") if hidden else None

        if token:
            self._csrf_token = token
        if header:
            self._csrf_header = header
        if param:
            self._csrf_param = param

    def login(self) -> None:
        # Login is not a dedicated page - it's an AJAX modal on the home
        # page, and the CSRF meta tags live there, not on any /login GET.
        home_html = self._fetch_home()
        self._read_csrf_from_html(home_html)

        if not self._csrf_token:
            raise LoginError("Kein CSRF-Token auf der Startseite gefunden.")

        data = {
            "username": self.username,
            "password": self.password,
            self._csrf_param: self._csrf_token,
        }
        headers = {
            self._csrf_header: self._csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "X-Ajax-Call": "true",
        }
        resp = self.session.post(
            self._url("/login"), data=data, headers=headers, timeout=15
        )
        resp.raise_for_status()

        # The AJAX endpoint answers 200 with a small fragment either way -
        # no redirect to key success off of - so verify by re-checking the
        # actual session state afterwards.
        home_after = self._fetch_home()
        if not self._is_authenticated(home_after):
            logger.debug(
                "Login-Statuscheck fehlgeschlagen, POST-Antwort: status=%s len=%d. "
                "Home-HTML-Anfang: %.300s",
                resp.status_code,
                len(resp.text),
                home_after,
            )
            raise LoginError(
                "Login fehlgeschlagen (Session nach dem Login-Request weiterhin "
                "nicht authentifiziert). Bitte EBUSY_USERNAME/EBUSY_PASSWORD pruefen. "
                "Mit --verbose ausfuehren fuer einen Diagnose-Ausschnitt."
            )

        logger.info("Login erfolgreich (%s)", self.username)

    def _fetch_home(self) -> str:
        resp = self.session.get(self._url("/"), timeout=15)
        resp.raise_for_status()
        self._read_csrf_from_html(resp.text)
        return resp.text

    @staticmethod
    def _is_authenticated(home_html: str) -> bool:
        return "/logout" in home_html

    def _auth_headers(self) -> dict[str, str]:
        if not self._csrf_token:
            raise LoginError("Kein CSRF-Token vorhanden - erst login() aufrufen.")
        return {self._csrf_header: self._csrf_token}

    def ensure_logged_in(self) -> None:
        if not self._is_authenticated(self._fetch_home()):
            self.login()

    # ------------------------------------------------------------------ #
    # Slot-Suche
    # ------------------------------------------------------------------ #

    def find_bookable_slots(
        self, target_date: date, target_time: time, preferred_courts: list[int]
    ) -> list[Slot]:
        """Returns bookable slots matching target_time, ordered by the caller's
        court preference - so on a collision the next-preferred court can be
        tried without a fresh page fetch."""
        date_str = target_date.strftime("%m/%d/%Y")
        resp = self.session.get(
            self._url("/padel"),
            params={"selectedDate": date_str},
            timeout=15,
        )
        resp.raise_for_status()
        logger.debug(
            "Kalender-Response Header fuer %s: Cache-Control=%s ETag=%s "
            "Last-Modified=%s Date=%s Age=%s",
            date_str,
            resp.headers.get("Cache-Control"),
            resp.headers.get("ETag"),
            resp.headers.get("Last-Modified"),
            resp.headers.get("Date"),
            resp.headers.get("Age"),
        )

        soup = BeautifulSoup(resp.text, "html.parser")
        target_str = target_time.strftime("%H:%M")

        matches: dict[int, Slot] = {}
        for cell in soup.select("td.slot"):
            classes = cell.get("class", [])
            if "bookable" not in " ".join(classes):
                label = cell.select_one(".slot-label")
                if not label or "bookable" not in label.get("class", []):
                    continue

            if cell.get("data-major-begin") != target_str:
                continue

            court_raw = cell.get("data-court")
            if court_raw is None:
                continue
            court = int(court_raw)
            if court not in preferred_courts:
                continue

            logger.debug("Gematchte Kalenderzelle (Court %s): %s", court, cell)
            matches[court] = Slot(
                court=court,
                date_us=cell.get("data-date", date_str),
                begin=cell.get("data-major-begin", target_str),
            )

        return [matches[court] for court in preferred_courts if court in matches]

    # ------------------------------------------------------------------ #
    # Buchungs-Flow (Spring Web Flow)
    # ------------------------------------------------------------------ #

    def book_slot(
        self,
        slot: Slot,
        duration_minutes: int,
        all_courts: list[int],
        max_steps: int = MAX_FLOW_STEPS,
    ) -> str:
        """Walks the multi-step Web Flow booking dialog to completion.

        Returns a human-readable success description. Raises BookingError
        (or FlowStuckError) on failure.
        """
        from_time = slot.begin
        end_dt = add_minutes(from_time, duration_minutes)

        courts_param = ",".join(str(c) for c in all_courts)
        resp = self.session.get(
            self._url("/court-single-booking-flow"),
            params={
                "module": PADEL_MODULE_ID,
                "court": slot.court,
                "courts": courts_param,
                "fromTime": from_time,
                "toTime": end_dt,
                "date": slot.date_us,
            },
            headers=self._auth_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        step = parse_step(resp.text, resp.url)

        if step.error_message:
            raise BookingError(step.error_message, html=resp.text)
        if not step.execution:
            raise FlowStuckError(
                "Konnte keinen execution-Status aus der Flow-Startseite lesen.",
                html=resp.text,
            )

        flow_url = self._url("/court-single-booking-flow")
        seen_executions: list[str] = [step.execution]

        for _ in range(max_steps):
            tried: list[str] = []
            advanced = False
            for event_id in _ordered_candidates(step.candidate_event_ids):
                tried.append(event_id)
                next_resp = self.session.post(
                    flow_url,
                    params={"execution": step.execution, "_eventId": event_id},
                    headers=self._auth_headers(),
                    timeout=15,
                    allow_redirects=True,
                )
                next_resp.raise_for_status()
                next_step = parse_step(next_resp.text, next_resp.url)

                if next_step.error_message:
                    raise BookingError(next_step.error_message, html=next_resp.text)

                if not next_step.execution:
                    # Flow exited (redirected out) without an error -> done.
                    return self._finalize(next_step, next_resp.url)

                if next_step.execution not in seen_executions:
                    seen_executions.append(next_step.execution)
                    step = next_step
                    resp = next_resp
                    advanced = True
                    break

            if not advanced:
                raise FlowStuckError(
                    "Flow-Zustand hat sich trotz "
                    f"{tried} nicht veraendert (Zustand {step.execution}). "
                    "Vermutlich hat sich die Seite geaendert - bitte manuell "
                    "im Browser pruefen und Flow-Logik anpassen.",
                    html=step.html,
                )

        raise FlowStuckError(
            f"Buchungs-Flow nach {max_steps} Schritten nicht abgeschlossen.",
            html=step.html,
        )

    def _finalize(self, step, final_url: str) -> str:
        if step.error_message:
            raise BookingError(step.error_message, html=step.html)
        return f"Buchungs-Flow abgeschlossen, Endseite: {final_url}"

# "next" is the one _eventId confirmed by the manual browser analysis (used
# for the details->confirmation transition). Spring Web Flow wizards very
# commonly reuse the same event name for every "Weiter"-style transition in
# a linear flow, so it is tried first at every step, including the final
# confirm - if that assumption is wrong for the last step, these generic
# names are tried next before giving up.
_STATIC_FALLBACK_EVENTS = ["next", "confirm", "submit", "finish", "book", "save"]


def _ordered_candidates(discovered: list[str]) -> list[str]:
    ordered: list[str] = []
    for event_id in ["next"] + discovered + _STATIC_FALLBACK_EVENTS:
        if event_id not in ordered:
            ordered.append(event_id)
    return ordered


def add_minutes(hhmm: str, minutes: int) -> str:
    match = re.match(r"^(\d{1,2}):(\d{2})$", hhmm)
    if not match:
        raise ValueError(f"Unerwartetes Zeitformat: {hhmm}")
    h, m = int(match.group(1)), int(match.group(2))
    total = h * 60 + m + minutes
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"
