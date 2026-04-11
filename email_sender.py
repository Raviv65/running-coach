"""Gmail delivery via SMTP + app password; HTML from Markdown briefing."""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import markdown


def markdown_to_html(md: str) -> str:
    return markdown.markdown(md, extensions=["extra", "nl2br"])


def wrap_html(inner: str, title: str = "Training briefing") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.55; color: #111; background: #f6f7f9; margin: 0; padding: 24px; }}
    .card {{ max-width: 720px; margin: 0 auto; background: #fff; border-radius: 12px;
             padding: 28px 32px; box-shadow: 0 8px 30px rgba(0,0,0,0.06); }}
    h1 {{ font-size: 1.35rem; margin: 0 0 16px; }}
    h2 {{ font-size: 1.1rem; margin-top: 1.4em; color: #1a1a1a; }}
    code {{ background: #f0f1f4; padding: 2px 6px; border-radius: 4px; }}
    a {{ color: #2563eb; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{escape(title)}</h1>
    {inner}
  </div>
</body>
</html>"""


def send_briefing_email(
    subject: str,
    markdown_body: str,
    *,
    to_addr: str | None = None,
) -> None:
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to_addr = to_addr or os.environ.get("RECIPIENT_EMAIL") or user
    if not user or not password or not to_addr:
        raise RuntimeError("GMAIL_USER, GMAIL_APP_PASSWORD, and RECIPIENT_EMAIL (or GMAIL_USER) required")

    html_inner = markdown_to_html(markdown_body)
    html_full = wrap_html(html_inner, title=subject)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(markdown_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_full, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, [to_addr], msg.as_string())
