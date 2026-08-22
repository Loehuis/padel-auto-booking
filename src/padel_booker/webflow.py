from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

_EVENT_ID_PATTERN = re.compile(r"_eventId[=_]([A-Za-z0-9_\-]+)")
_EXECUTION_PATTERN = re.compile(r"execution=([A-Za-z0-9]+)")
_ALERT_DANGER_PATTERN = re.compile(
    r'<div[^>]*class="[^"]*alert-danger[^"]*"[^>]*>(.*?)</div>', re.DOTALL
)
_TAG_PATTERN = re.compile(r"<[^>]+>")

# Buttons/links matching these keywords are navigational dead-ends for our
# purpose (going back, cancelling) and must never be auto-selected as the
# "forward" transition, even though they also carry an _eventId.
_NEGATIVE_KEYWORDS = (
    "back",
    "cancel",
    "abbrechen",
    "zurueck",
    "zurück",
    "close",
    "reset",
)


@dataclass
class FlowStep:
    html: str
    execution: str | None
    candidate_event_ids: list[str]
    error_message: str | None


def find_forward_event_ids(html: str) -> list[str]:
    """Best-effort extraction of _eventId candidates from a Web Flow view.

    Scoped per-element (not a fixed character window) so a "Zurück"/"Abbrechen"
    link elsewhere on the page can't accidentally veto an unrelated "Weiter"
    button. This is only a *hint*: the site builds some of these transitions
    dynamically via jQuery, so absence of a match here does not mean no
    forward transition exists - callers should combine this with known/likely
    event names (see client._ordered_candidates)."""
    soup = BeautifulSoup(html, "html.parser")
    seen: list[str] = []

    for tag in soup.find_all(True):
        haystacks: list[str] = []
        for value in tag.attrs.values():
            haystacks.append(value if isinstance(value, str) else " ".join(value))

        found_here: list[str] = []
        for haystack in haystacks:
            for match in _EVENT_ID_PATTERN.finditer(haystack):
                found_here.append(match.group(1))

        if not found_here:
            continue

        own_text = tag.get_text(" ", strip=True).lower()
        own_attrs = " ".join(haystacks).lower()
        blob = f"{own_text} {own_attrs}"
        is_negative_context = any(kw in blob for kw in _NEGATIVE_KEYWORDS)

        for event_id in found_here:
            if event_id in seen:
                continue
            if is_negative_context or any(kw in event_id.lower() for kw in _NEGATIVE_KEYWORDS):
                continue
            seen.append(event_id)

    return seen


def extract_execution(text: str) -> str | None:
    match = _EXECUTION_PATTERN.search(text)
    return match.group(1) if match else None


def extract_error_message(html: str) -> str | None:
    match = _ALERT_DANGER_PATTERN.search(html)
    if not match:
        return None
    text = _TAG_PATTERN.sub(" ", match.group(1))
    return " ".join(text.split()) or None


def parse_step(html: str, response_url: str) -> FlowStep:
    return FlowStep(
        html=html,
        execution=extract_execution(response_url) or extract_execution(html),
        candidate_event_ids=find_forward_event_ids(html),
        error_message=extract_error_message(html),
    )
