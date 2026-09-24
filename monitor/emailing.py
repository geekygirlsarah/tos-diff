"""
Email delivery for TosDiff.

Transactional mail (OTP login codes, suggestion replies, daily/weekly
digests) is sent through Mailgun's HTTP Messages API via the ``default``
mailer in ``settings.MAILERS``, while admin/ops mail (Django error reports
and ``mail_admins()`` calls) goes over SMTP via the ``admin`` mailer.

No third-party mail library is required: Mailgun is called directly with
`requests` (already a project dependency), keeping the dependency tree and
CI's ``pip-audit --strict`` unchanged.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import MIMEBase

logger = logging.getLogger("monitor.emailing")

MAILGUN_API_URL = "https://api.mailgun.net"
MAILGUN_TIMEOUT = 30


class MailgunBackend(BaseEmailBackend):
    """
    Django email backend that delivers messages through Mailgun's HTTP API.

    Configuration is read from ``django.conf.settings`` at send time so that
    ``override_settings()`` works in tests and an unconfigured local machine
    degrades cleanly:

    - ``MAILGUN_API_KEY`` — Mailgun private API key.
    - ``MAILGUN_DOMAIN``  — sending domain (e.g. ``mg.example.com``).
    - ``MAILGUN_API_URL`` — API base URL (default ``https://api.mailgun.net``;
      ``https://api.eu.mailgun.net`` for the EU region).
    - ``MAILGUN_TIMEOUT`` — request timeout in seconds (default 30).
    """

    def __init__(self, fail_silently: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_silently = fail_silently

    def _config(self) -> dict:
        return {
            "api_key": getattr(settings, "MAILGUN_API_KEY", ""),
            "domain": getattr(settings, "MAILGUN_DOMAIN", ""),
            "base_url": getattr(settings, "MAILGUN_API_URL", MAILGUN_API_URL).rstrip("/"),
            "timeout": float(getattr(settings, "MAILGUN_TIMEOUT", MAILGUN_TIMEOUT)),
        }

    def send_messages(self, email_messages: list) -> int:
        """POST each *email_messages* entry to Mailgun; return the sent count."""
        cfg = self._config()
        if not cfg["api_key"] or not cfg["domain"]:
            logger.error("MailgunBackend: MAILGUN_API_KEY or MAILGUN_DOMAIN not set; email dropped")
            if not self.fail_silently:
                raise RuntimeError("MAILGUN_API_KEY and MAILGUN_DOMAIN must be set.")
            return 0

        sent = 0
        for message in email_messages:
            try:
                self._send_one(message, cfg)
            except Exception:  # noqa: BLE001
                if not self.fail_silently:
                    raise
                logger.exception("MailgunBackend: failed to send message")
                continue
            sent += 1
        return sent

    def _send_one(self, message, cfg: dict) -> None:
        data = {
            "from": str(message.from_email or settings.DEFAULT_FROM_EMAIL),
            "to": ", ".join(str(address) for address in message.to),
            "subject": message.subject or "",
            "text": message.body or "",
        }
        if message.cc:
            data["cc"] = ", ".join(str(address) for address in message.cc)
        if message.bcc:
            data["bcc"] = ", ".join(str(address) for address in message.bcc)
        if message.reply_to:
            data["h:Reply-To"] = ", ".join(str(address) for address in message.reply_to)

        for alternative in getattr(message, "alternatives", []):
            content, mimetype = alternative
            if mimetype == "text/html":
                data["html"] = content
                break

        for name, value in (message.extra_headers or {}).items():
            data[f"h:{name}"] = value

        url = f"{cfg['base_url']}/v3/{cfg['domain']}/messages"
        response = requests.post(
            url,
            auth=("api", cfg["api_key"]),
            data=data,
            files=self._attachments(message),
            timeout=cfg["timeout"],
        )
        response.raise_for_status()

    def _attachments(self, message) -> list | None:
        files = []
        for attachment in message.attachments:
            if isinstance(attachment, MIMEBase):
                name = attachment.get_filename() or "attachment"
                content = attachment.get_payload(decode=True)
                mimetype = attachment.get_content_type()
            else:
                name, content, mimetype = attachment
            files.append(("attachment", (name, content, mimetype)))
        return files or None
