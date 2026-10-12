"""Outgoing email.

MAIL_BACKEND:
  smtp   - send through SMTP_HOST (default when SMTP_HOST is set)
  file   - write .eml files to instance/outbox (default otherwise; handy for a trial)
  memory - keep messages in app.extensions["outbox"] (tests)
"""
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from flask import current_app


def outbox():
    return current_app.extensions.setdefault("outbox", [])


def send_mail(to, subject, text, html=None):
    config = current_app.config
    msg = EmailMessage()
    msg["From"] = config["MAIL_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=config["MAIL_FROM"].rsplit("@", 1)[-1].strip(">") or "localhost")
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    backend = config["MAIL_BACKEND"]
    if backend == "memory":
        outbox().append(msg)
    elif backend == "file":
        folder = os.path.join(current_app.instance_path, "outbox")
        os.makedirs(folder, exist_ok=True)
        name = f"{datetime.now():%Y%m%d-%H%M%S-%f}-{to.split('@')[0]}.eml"
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(bytes(msg))
    elif backend == "smtp":
        port = int(config["SMTP_PORT"])
        context = ssl.create_default_context()
        if port == 465:
            server = smtplib.SMTP_SSL(config["SMTP_HOST"], port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(config["SMTP_HOST"], port, timeout=30)
            server.starttls(context=context)
        with server:
            if config.get("SMTP_USER"):
                server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
            server.send_message(msg)
    else:
        raise RuntimeError(f"Unknown MAIL_BACKEND {backend!r}")
    return msg
