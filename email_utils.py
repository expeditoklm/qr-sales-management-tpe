import smtplib
from email.message import EmailMessage

from config import get_settings

cfg = get_settings()


def send_email(*, to_email: str, subject: str, text: str, html: str | None = None) -> dict:
    if not cfg.MAIL_ENABLED or not cfg.MAIL_HOST:
        preview = {
            "to": to_email,
            "subject": subject,
            "text": text,
            "html": html,
            "mode": "preview",
        }
        print(f"[MAIL PREVIEW] {preview}")
        return {"sent": False, "preview": preview}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.MAIL_FROM
    msg["To"] = to_email
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(cfg.MAIL_HOST, cfg.MAIL_PORT, timeout=10) as smtp:
            if cfg.MAIL_USE_TLS:
                smtp.starttls()
            if cfg.MAIL_USERNAME:
                smtp.login(cfg.MAIL_USERNAME, cfg.MAIL_PASSWORD)
            smtp.send_message(msg)
        return {"sent": True, "preview": None}
    except Exception as exc:
        preview = {
            "to": to_email,
            "subject": subject,
            "text": text,
            "html": html,
            "mode": "smtp_error",
            "error": str(exc),
        }
        print(f"[MAIL ERROR] {preview}")
        return {"sent": False, "preview": preview}
