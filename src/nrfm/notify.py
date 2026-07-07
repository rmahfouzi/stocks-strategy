"""Email notifications (trade signals, validation alerts).

Credentials are never stored in the repo. They are read from, in order:

1. Environment variables: NRFM_SMTP_HOST, NRFM_SMTP_PORT, NRFM_SMTP_USER,
   NRFM_SMTP_PASSWORD, NRFM_EMAIL_TO
2. A key=value file at ~/.config/nrfm/email.env (chmod 600), same keys.

For Gmail: NRFM_SMTP_HOST=smtp.gmail.com, NRFM_SMTP_PORT=465,
NRFM_SMTP_USER=<address>, NRFM_SMTP_PASSWORD=<app password -- requires 2FA,
create at https://myaccount.google.com/apppasswords>.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

CONFIG_FILE = Path.home() / ".config" / "nrfm" / "email.env"

_KEYS = ("NRFM_SMTP_HOST", "NRFM_SMTP_PORT", "NRFM_SMTP_USER",
         "NRFM_SMTP_PASSWORD", "NRFM_EMAIL_TO")


class EmailConfigError(RuntimeError):
    pass


@dataclass
class EmailConfig:
    host: str
    port: int
    user: str
    password: str
    to: str


def _read_env_file(path: Path = CONFIG_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def load_config(env_file: Path = CONFIG_FILE) -> EmailConfig:
    file_values = _read_env_file(env_file)
    values = {k: os.environ.get(k) or file_values.get(k) for k in _KEYS}
    missing = [k for k in _KEYS if not values[k]]
    if missing:
        raise EmailConfigError(
            f"missing email settings {missing}; set them as environment "
            f"variables or in {env_file} (see src/nrfm/notify.py)"
        )
    return EmailConfig(
        host=values["NRFM_SMTP_HOST"],
        port=int(values["NRFM_SMTP_PORT"]),
        user=values["NRFM_SMTP_USER"],
        password=values["NRFM_SMTP_PASSWORD"],
        to=values["NRFM_EMAIL_TO"],
    )


def send_email(subject: str, body: str,
               config: EmailConfig | None = None) -> None:
    cfg = config or load_config()
    msg = EmailMessage()
    msg["From"] = cfg.user
    msg["To"] = cfg.to
    msg["Subject"] = subject
    msg.set_content(body)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg.host, cfg.port, context=context,
                          timeout=30) as smtp:
        smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)


def try_send_email(subject: str, body: str) -> str | None:
    """Send without raising; returns an error string on failure.

    The nightly job must never die because email is down -- the local
    log remains the source of truth.
    """
    try:
        send_email(subject, body)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"
