"""Django email backend that sends through Brevo's HTTPS API.

Used when BREVO_API_KEY is set (see settings.py). Hosts like Render's free plan
block SMTP ports, but normal HTTPS requests go through, so this works there.
No extra packages needed.
"""
import base64
import json
import logging
import urllib.error
import urllib.request
from email.utils import parseaddr

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

log = logging.getLogger(__name__)
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _contact(address):
    name, email = parseaddr(address)
    contact = {"email": email}
    if name:
        contact["name"] = name
    return contact


class BrevoEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages or []:
            try:
                self._send(message)
                sent += 1
            except Exception:
                log.exception("Brevo: could not send email to %s", message.to)
                if not self.fail_silently:
                    raise
        return sent

    def _send(self, message):
        recipients = [r for r in message.to if r]
        if not recipients:
            return
        payload = {
            "sender": _contact(message.from_email or settings.DEFAULT_FROM_EMAIL),
            "to": [_contact(r) for r in recipients],
            "subject": message.subject,
        }
        if message.cc:
            payload["cc"] = [_contact(r) for r in message.cc]
        if message.bcc:
            payload["bcc"] = [_contact(r) for r in message.bcc]
        if message.reply_to:
            payload["replyTo"] = _contact(message.reply_to[0])

        html = None
        for content, mimetype in getattr(message, "alternatives", []) or []:
            if mimetype == "text/html":
                html = content
        if message.content_subtype == "html":
            html = message.body
        else:
            payload["textContent"] = message.body or " "
        if html:
            payload["htmlContent"] = html

        attachments = []
        for att in message.attachments:
            if isinstance(att, tuple) or hasattr(att, "filename"):
                filename, content = att[0], att[1]
                if isinstance(content, str):
                    content = content.encode()
                attachments.append({"name": filename or "attachment",
                                    "content": base64.b64encode(content).decode()})
        if attachments:
            payload["attachment"] = attachments

        request = urllib.request.Request(
            BREVO_URL,
            data=json.dumps(payload).encode(),
            headers={
                "api-key": settings.BREVO_API_KEY,
                "accept": "application/json",
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")
            raise RuntimeError(f"Brevo rejected the email ({err.code}): {detail}") from err
