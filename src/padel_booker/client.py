from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, time

import requests
from bs4 import BeautifulSoup

from .webflow import extract_success_message, parse_step

logger = logging.getLogger("padel_booker.client")

PADEL_MODULE_ID = 4

# Exact text confirmed live for "someone already holds this slot" rejections
# (as opposed to e.g. the 7-day-advance-limit message, which reads
# differently). Used to classify a BookingError so the scheduler can back off
# quickly on a confirmed collision without needing repeated confirmation.
_COLLISION_MARKER = "konflikt mit einem bestehenden termin"

# Confirmed live: the calendar can mark a cell "bookable" slightly before the
# site's own submission-time check actually allows it (the real unlock
# apparently lands at the target slot's own clock time, not a moment before).
# A rejection with this text just means "not yet, try again shortly" - it
# should never count toward giving up for the week the way a real collision
# does.
_TOO_EARLY_MARKER = "maximal 7 tage im voraus"

# Confirmed live: a Web Flow step can "exit" (no more execution token) by
# landing on an error/expired-session route with no scraped .alert-danger
# text at all - e.g. "/flow-not-found". Without this check that was
# misread as success. Generic enough to catch similar error routes even if
# the exact path differs from the one observed.
_ERROR_URL_MARKERS = ("not-found", "notfound", "/error", "?error")


def _looks_like_error_url(url: str) -> bool:
    return any(marker in url.lower() for marker in _ERROR_URL_MARKERS)


class LoginError(RuntimeError):
    pass


class BookingError(RuntimeError):
    def __init__(self, reason: str, html: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.html = html
        reason_lower = reason.lower()
        self.is_collision = _COLLISION_MARKER in reason_lower
        self.is_too_early = _TOO_EARLY_MARKER in reason_lower


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

    def _flow_headers(self) -> dict[str, str]:
        # Confirmed live for the flow's "next" transition (same AJAX markers
        # as the login endpoint): without these the site may treat the
        # request as a plain (non-XHR) navigation and behave differently.
        headers = self._auth_headers()
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["X-Ajax-Call"] = "true"
        return headers

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
    ) -> str:
        """Walks the booking dialog through its two confirmed transitions:
        "next" (details -> confirmation) and "commit" (confirmation -> done).

        Both events and their exact required form fields were confirmed live
        against two separate real successful bookings. A generic "try several
        guessed events until the state advances" walker was attempted first,
        but turned out to be structurally unsafe: Spring Web Flow's
        `execution` key is single-use - once one request has been made
        against it, a second request reusing the same key always kills the
        flow ("Der gewaehlte Vorgang wurde bereits beendet"), regardless of
        which event is sent. Sending exactly one, known-correct event per
        step avoids that failure mode entirely.

        Returns a human-readable success description. Raises BookingError
        (or FlowStuckError) on failure.
        """
        from_time = slot.begin
        end_dt = add_minutes(from_time, duration_minutes)

        courts_param = ",".join(str(c) for c in all_courts)
        flow_url = self._url("/court-single-booking-flow")
        resp = self.session.get(
            flow_url,
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
        details_step = parse_step(resp.text, resp.url)

        if details_step.error_message:
            raise BookingError(details_step.error_message, html=resp.text)
        if not details_step.execution:
            raise FlowStuckError(
                "Konnte keinen execution-Status aus der Flow-Startseite lesen.",
                html=resp.text,
            )

        confirm_resp, confirm_step = self._submit_flow_event(
            flow_url, details_step, "next"
        )
        if confirm_step.error_message:
            raise BookingError(confirm_step.error_message, html=confirm_resp.text)
        if not confirm_step.execution:
            # Flow already exited after just one step - not expected for a
            # real success (that needs "commit" too), but handle it the same
            # defensive way as the final step in case the site's flow ever
            # collapses details+confirmation into one transition.
            return self._finalize(confirm_step, confirm_resp.url)

        final_resp, final_step = self._submit_flow_event(
            flow_url, confirm_step, "commit"
        )

        # Confirmed live: "commit" answers 200 OK with no redirect even on a
        # real success, so the request URL we sent (execution param
        # included) is echoed back unchanged - the execution/URL-based
        # "did the flow advance" heuristic below can't tell success from
        # stuck for this specific step. Check the positive success-dialog
        # marker first, before ever consulting that heuristic.
        success_message = extract_success_message(final_resp.text)
        if success_message:
            return f"Buchung erfolgreich bestaetigt: {success_message}"

        if final_step.error_message:
            raise BookingError(final_step.error_message, html=final_resp.text)
        if final_step.execution:
            logger.warning(
                "Unerwarteter dritter Flow-Schritt nach 'commit' (execution=%s) - "
                "Buchungsdialog hat sich vermutlich geaendert. Body (erste 4000 "
                "Zeichen): %.4000s",
                final_step.execution,
                final_resp.text,
            )
            raise FlowStuckError(
                "Buchungs-Flow nach 'commit' immer noch nicht abgeschlossen "
                f"(execution={final_step.execution}). Vermutlich hat sich der "
                "Buchungsdialog geaendert - bitte manuell im Browser pruefen.",
                html=final_step.html,
            )

        return self._finalize(final_step, final_resp.url)

    def _submit_flow_event(self, flow_url: str, step, event_id: str):
        """Resubmits the current step's own rendered form fields (mirroring a
        real browser) together with the single confirmed event for this
        transition. Logs the full raw request/response when the flow exits
        without a new execution token, lands on a suspicious-looking error
        URL, and has no recognized error message either, so a failure can be
        diagnosed from the journal without a fresh live browser capture -
        deliberately at WARNING, not DEBUG, since the systemd unit runs
        without --verbose. A real success also exits without an execution
        token (that's the expected outcome after "commit"), so this is
        scoped to the error-URL case specifically to avoid logging noise on
        every successful booking."""
        post_data = _extract_form_fields(step.html)
        post_data[self._csrf_param] = self._csrf_token
        resp = self.session.post(
            flow_url,
            params={"execution": step.execution, "_eventId": event_id},
            data=post_data,
            headers=self._flow_headers(),
            timeout=15,
            allow_redirects=True,
        )
        resp.raise_for_status()
        next_step = parse_step(resp.text, resp.url)

        if (
            not next_step.execution
            and not next_step.error_message
            and _looks_like_error_url(resp.url)
        ):
            logger.warning(
                "Flow-Schritt ohne execution-Token zurueckgekommen. "
                "Angefragt: %s?execution=%s&_eventId=%s | Gesendete Felder: %s | "
                "Antwort: status=%s endg.-URL=%s | Body (erste 4000 Zeichen): %.4000s",
                flow_url,
                step.execution,
                event_id,
                post_data,
                resp.status_code,
                resp.url,
                resp.text,
            )
        return resp, next_step

    def _finalize(self, step, final_url: str) -> str:
        if step.error_message:
            raise BookingError(step.error_message, html=step.html)
        if _looks_like_error_url(final_url):
            raise FlowStuckError(
                f"Flow endete auf einer verdaechtig aussehenden URL ({final_url}) "
                "ohne erkennbare Fehlermeldung im HTML - vermutlich eine "
                "abgelaufene/ungueltige Flow-Session, keine echte Bestaetigung. "
                "Bitte manuell im Browser pruefen, ob wirklich gebucht wurde.",
                html=step.html,
            )
        return f"Buchungs-Flow abgeschlossen, Endseite: {final_url}"


def _extract_form_fields(html: str) -> dict[str, str]:
    """Collects name->value pairs for every form field present in a Web Flow
    view, mirroring what a real browser resubmits on the next transition -
    confirmed live: different steps require different fields (the details
    step needs purchaseTemplate.person/court/repetition.*, the confirmation
    step needs purchaseTemplate.comment), so resubmitting whatever the
    current page actually renders is far more robust than hardcoding a
    field set per step."""
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}

    for tag in soup.find_all(["input", "textarea", "select"]):
        name = tag.get("name")
        if not name:
            continue

        if tag.name == "textarea":
            fields[name] = tag.text or ""
        elif tag.name == "select":
            selected = tag.find("option", selected=True) or tag.find("option")
            fields[name] = selected.get("value", "") if selected else ""
        else:
            input_type = (tag.get("type") or "text").lower()
            if input_type in ("submit", "button", "image", "reset"):
                continue  # the button click itself is expressed via _eventId
            if input_type in ("checkbox", "radio"):
                if tag.has_attr("checked"):
                    fields[name] = tag.get("value", "on")
                # unchecked boxes/radios are simply omitted, like a real form
            else:
                fields[name] = tag.get("value", "")

    return fields


def add_minutes(hhmm: str, minutes: int) -> str:
    match = re.match(r"^(\d{1,2}):(\d{2})$", hhmm)
    if not match:
        raise ValueError(f"Unerwartetes Zeitformat: {hhmm}")
    h, m = int(match.group(1)), int(match.group(2))
    total = h * 60 + m + minutes
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"
