"""Local alerting for critical failures.

Important limitation, stated plainly: this runs as an offline Windows
process with no connection to a Claude Code session, so it cannot use the
PushNotification tool that pings your phone during an interactive session --
that tool only exists inside a live session. The alert paths available to a
standalone script are:

  1. A Windows toast/message-box notification (default, no extra
     dependencies or credentials needed) -- you'll see it if you're at the
     machine.
  2. A line in logs/alerts.log, always written regardless of (1)'s success,
     so a remote check of the log (e.g. you SSH in, or Claude reads the log
     next session) surfaces anything missed.
  3. Optional email via SMTP if you set SMTP_* variables in .env -- off by
     default. Forwarding that email to your phone (most mail apps push)
     is the closest thing to the phone alert you've gotten from Claude
     sessions tonight, and is the one this module can't set up for you
     without your mail credentials.

None of this is autonomous order activity -- it only ever informs you.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _write_alert_log(log_dir: Path, message: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "alerts.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")


def _windows_toast(title: str, message: str) -> bool:
    if sys.platform != "win32":
        return False
    ps_script = (
        f'$title = "{title}"; $message = "{message}"; '
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$notify = New-Object System.Windows.Forms.NotifyIcon; "
        "$notify.Icon = [System.Drawing.SystemIcons]::Warning; "
        "$notify.Visible = $true; "
        '$notify.ShowBalloonTip(15000, $title, $message, '
        "[System.Windows.Forms.ToolTipIcon]::Warning)"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            timeout=10, capture_output=True, check=False,
        )
        return True
    except Exception:
        return False


def _email_alert(subject: str, body: str) -> bool:
    host = os.environ.get("SMTP_HOST")
    if not host:
        return False
    import smtplib
    from email.mime.text import MIMEText

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = os.environ["SMTP_FROM"]
        msg["To"] = os.environ["SMTP_TO"]
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587))) as server:
            server.starttls()
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            server.send_message(msg)
        return True
    except Exception:
        return False


def alert_critical(log_dir: Path, logger: logging.Logger, title: str, message: str) -> None:
    """Fire every available alert channel and log the attempt. Never raises
    -- a failed alert must not crash the supervisor loop."""
    logger.critical(f"{title}: {message}")
    _write_alert_log(log_dir, f"CRITICAL {title}: {message}")
    toast_ok = _windows_toast(title, message)
    email_ok = _email_alert(f"[Medik] {title}", message)
    if not toast_ok and not email_ok:
        logger.warning(
            "No alert channel succeeded (not on Windows and SMTP not configured) -- "
            "this alert exists only in the log files."
        )
