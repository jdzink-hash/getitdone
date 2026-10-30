import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

import db
import mailer

log = logging.getLogger("getitdone.scheduler")

# If the server was down at someone's send time, still send if we're within this window.
CATCH_UP_MINUTES = 180


def _minutes(hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m


def send_due_digests():
    conn = db.connect()
    try:
        now = datetime.now(timezone.utc)
        users = conn.execute("SELECT * FROM users WHERE digest_enabled = 1 AND email != ''").fetchall()
        for user in users:
            try:
                local = now.astimezone(ZoneInfo(user["timezone"]))
            except Exception:
                local = now
            today = local.date().isoformat()
            late_by = local.hour * 60 + local.minute - _minutes(user["send_time"])
            if user["last_sent_date"] == today or not 0 <= late_by <= CATCH_UP_MINUTES:
                continue

            # Claim today's send atomically so concurrent processes never send twice.
            claimed = conn.execute(
                "UPDATE users SET last_sent_date = ? WHERE id = ? "
                "AND (last_sent_date IS NULL OR last_sent_date != ?)",
                (today, user["id"], today),
            ).rowcount
            conn.commit()
            if not claimed:
                continue

            try:
                mailer.send_email(
                    user["email"], mailer.digest_subject(local.date()), mailer.compose_digest(conn, user, local.date())
                )
            except Exception:
                log.exception("Failed to send morning email to user %s", user["id"])
    finally:
        conn.close()


def start():
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(send_due_digests, "cron", minute="*", max_instances=1, coalesce=True)
    scheduler.start()
    log.info("Morning-email scheduler started")
    return scheduler
