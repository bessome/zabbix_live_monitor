"""SMTP notification for registrations; credentials come only from the environment."""
import os
import smtplib
import ssl
from email.message import EmailMessage


def notify_registration(username, email):
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not host or not sender:
        raise RuntimeError("SMTP_HOST and SMTP_FROM are required")
    security = os.environ.get("SMTP_SECURITY", "starttls").lower()
    if security not in ("starttls", "ssl", "none"):
        raise RuntimeError("Invalid SMTP_SECURITY")
    port = int(os.environ.get("SMTP_PORT") or (465 if security == "ssl" else 587))
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "maksim@stv.ee"
    message["Subject"] = "Zabbix Live Monitoring: заявка на регистрацию"
    message.set_content(
        f"Новая заявка на регистрацию.\nЛогин: {username}\nEmail: {email}\n\n"
        "Для активации войдите в приложение под администратором, откройте "
        "Настройки → Пользователи, выберите «Активен» и сохраните учётную запись.\n"
    )
    context = ssl.create_default_context()
    client = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    kwargs = {"context": context} if security == "ssl" else {}
    with client(host, port, timeout=15, **kwargs) as smtp:
        if security == "starttls":
            smtp.starttls(context=context)
        login = os.environ.get("SMTP_USERNAME", "")
        if login:
            smtp.login(login, os.environ.get("SMTP_PASSWORD", ""))
        if smtp.send_message(message):
            raise RuntimeError("Registration notification recipient refused")
