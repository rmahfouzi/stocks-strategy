"""Email command channel: update holdings/equity by sending an email.

The user sends a message to the strategy's Gmail account with "NRFM"
in the subject and commands in the body, one per line:

    add SOBI          record a holding (".ST" appended automatically)
    rm ERIC-B         remove a holding ("remove" also accepted)
    equity 100000     set portfolio size in SEK
    list              just get the current register back

The nightly run (and `nrfm inbox`) reads UNSEEN matching messages via
IMAP using the same app-password credentials as SMTP, applies commands
from the authorized sender only, and replies with a confirmation.

Security model: commands only mutate the local holdings register /
equity setting -- never orders or money. Sender is checked against the
configured alert recipient (Gmail's SPF/DKIM filtering makes spoofing
into the inbox hard); worst case is a wrong register, which the next
confirmation email exposes.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import re
from dataclasses import dataclass
from datetime import date

from nrfm import config
from nrfm.notify import EmailConfig, load_config, send_email
from nrfm.store import Store

IMAP_HOST = "imap.gmail.com"
SUBJECT_KEYWORD = "NRFM"
COMMAND_RE = re.compile(r"^\s*(add|rm|remove|equity|list)\b[\s:]*(.*)$",
                        re.IGNORECASE)


@dataclass
class CommandResult:
    line: str
    outcome: str


def normalize_ticker(raw: str) -> str:
    t = raw.strip().upper().replace(" ", "-")
    if not t.endswith(".ST"):
        t += ".ST"
    return t


def parse_commands(body: str) -> list[tuple[str, str]]:
    """Extract (verb, argument) pairs; ignores quoted reply lines."""
    commands = []
    for line in body.splitlines():
        if line.lstrip().startswith(">"):
            continue
        m = COMMAND_RE.match(line)
        if m:
            verb = m.group(1).lower()
            commands.append(("rm" if verb == "remove" else verb,
                             m.group(2).strip()))
    return commands


def apply_commands(store: Store,
                   commands: list[tuple[str, str]]) -> list[CommandResult]:
    from nrfm.engine.live import STATE_EQUITY

    results = []
    valid_tickers = {r["yahoo_ticker"] for r in store.active_instruments()}
    for verb, arg in commands:
        line = f"{verb} {arg}".strip()
        try:
            if verb == "add":
                ticker = normalize_ticker(arg)
                if ticker not in valid_tickers:
                    results.append(CommandResult(
                        line, f"REJECTED: {ticker} is not in the universe"))
                    continue
                store.hold_add(ticker, since=date.today().isoformat())
                results.append(CommandResult(line, f"holding added: {ticker}"))
            elif verb == "rm":
                ticker = normalize_ticker(arg)
                if ticker not in store.holdings():
                    results.append(CommandResult(
                        line, f"REJECTED: {ticker} was not held"))
                    continue
                store.hold_remove(ticker)
                results.append(CommandResult(line, f"holding removed: {ticker}"))
            elif verb == "equity":
                value = float(arg.replace(",", "").replace(" ", ""))
                store.state_set(STATE_EQUITY, str(value))
                results.append(CommandResult(
                    line, f"equity set to {value:,.0f} SEK"))
            elif verb == "list":
                results.append(CommandResult(line, "(register below)"))
        except (ValueError, TypeError) as e:
            results.append(CommandResult(line, f"REJECTED: {e}"))
    return results


def _authorized_senders(cfg: EmailConfig) -> set[str]:
    return {cfg.to.lower(), cfg.user.lower()}


def _message_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(
                        part.get_content_charset() or "utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8",
                          "replace") if payload else ""


def process_inbox(store: Store) -> int:
    """Apply pending email commands; returns how many messages handled.

    Never raises for empty/absent mail; raises on transport failures so
    the caller can log them (the nightly run treats that as non-fatal).
    """
    cfg = load_config()
    handled = 0
    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(cfg.user, cfg.password)
        imap.select("INBOX")
        _, data = imap.search(None, "UNSEEN", "SUBJECT", SUBJECT_KEYWORD)
        for num in data[0].split():
            _, msg_data = imap.fetch(num, "(RFC822)")  # marks \Seen
            msg = email.message_from_bytes(msg_data[0][1])
            sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
            if sender not in _authorized_senders(cfg):
                continue  # left read but unprocessed; not a command source
            commands = parse_commands(_message_body(msg))
            if not commands:
                continue
            results = apply_commands(store, commands)
            handled += 1
            register = store.holdings()
            register_lines = ([f"  {t}" for t in register]
                              if register else ["  (empty)"])
            body = "\n".join(
                [f"  {r.line:24} -> {r.outcome}" for r in results]
                + ["", "Current holdings register:"]
                + register_lines
            )
            send_email(f"[NRFM] holdings updated "
                       f"({len(results)} commands)", body, cfg)
    return handled
