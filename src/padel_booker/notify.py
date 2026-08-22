from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import NotificationConfig, SmtpConfig

logger = logging.getLogger("padel_booker.notify")


def notify(notification: NotificationConfig, smtp: SmtpConfig, subject: str, body: str) -> None:
    logger.info("%s: %s", subject, body)

    if not notification.email_enabled:
        return
    if not notification.email_to or not smtp.host or not smtp.user or not smtp.password:
        logger.warning(
            "E-Mail-Benachrichtigung aktiviert, aber SMTP-Konfiguration/Empfaenger "
            "unvollstaendig - ueberspringe Versand."
        )
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp.from_addr or smtp.user
    msg["To"] = notification.email_to
    msg.set_content(body)

    try:
        with smtplib.SMTP(smtp.host, smtp.port, timeout=15) as server:
            if smtp.use_tls:
                server.starttls()
            server.login(smtp.user, smtp.password)
            server.send_message(msg)
        logger.info("Benachrichtigung per E-Mail an %s gesendet.", notification.email_to)
    except Exception:
        logger.exception("Konnte Benachrichtigungs-E-Mail nicht senden.")
