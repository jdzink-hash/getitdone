import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

import calendars

log = logging.getLogger("getitdone.mailer")


def configured():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("EMAIL_FROM"))


def send_email(to, subject, body):
    """Send a plain-text email. Without SMTP settings, print it instead (development mode)."""
    if not configured():
        print(f"\n----- Email to {to} (dev mode, not sent) -----\nSubject: {subject}\n\n{body}\n" + "-" * 46, flush=True)
        return
    msg = EmailMessage()
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or 587)
    user, password = os.environ.get("SMTP_USERNAME"), os.environ.get("SMTP_PASSWORD")
    context = ssl.create_default_context()
    # Port 465 speaks TLS from the start; everything else upgrades with STARTTLS.
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=context, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        server.starttls(context=context)
    with server:
        if user:
            server.login(user, password or "")
        server.send_message(msg)
    log.info("Sent email to %s", to)


def digest_subject(day):
    return f"Your {day:%A}, {day:%B} {day.day}"


def fmt_time(hhmm):
    if not hhmm:
        return ""
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "pm" if h >= 12 else "am"
    h = h % 12 or 12
    return f"{h}:{m:02d}{suffix}" if m else f"{h}{suffix}"


def compose_digest(conn, user, day):
    """Build the personalised morning email for `user` on local date `day`."""
    uid, d = user["id"], day.isoformat()

    appointments = [dict(a) for a in conn.execute(
        "SELECT title, time, location FROM appointments WHERE user_id = ? AND date = ?", (uid, d)
    )]
    appointments += calendars.events_between(conn, user, day, day)
    # All-day items first, then by time.
    appointments.sort(key=lambda a: (a["time"] is not None, a["time"] or ""))
    tasks = conn.execute(
        "SELECT title, date FROM tasks WHERE user_id = ? AND done = 0 AND (date IS NULL OR date <= ?) "
        "ORDER BY date IS NULL, date, id",
        (uid, d),
    ).fetchall()
    workouts = conn.execute(
        "SELECT activity, duration FROM workouts WHERE user_id = ? AND date = ? AND done = 0 ORDER BY id",
        (uid, d),
    ).fetchall()
    groceries = conn.execute(
        "SELECT name FROM groceries WHERE user_id = ? AND done = 0 ORDER BY id", (uid,)
    ).fetchall()

    first_name = (user["name"] or "").split(" ")[0] or "there"
    lines = [f"Good morning, {first_name}! Here's your {day:%A}, {day:%B} {day.day}."]

    if appointments:
        lines += ["", "Appointments"]
        for a in appointments:
            when = fmt_time(a["time"]) or "All day"
            where = f" @ {a['location']}" if a["location"] else ""
            lines.append(f"• {when} {a['title']}{where}")

    if tasks:
        lines += ["", "To do"]
        for t in tasks[:12]:
            carried = " (carried over)" if t["date"] and t["date"] < d else ""
            lines.append(f"• {t['title']}{carried}")
        if len(tasks) > 12:
            lines.append(f"• …and {len(tasks) - 12} more")

    if workouts:
        lines += ["", "Movement"]
        for w in workouts:
            mins = f" ({w['duration']} min)" if w["duration"] else ""
            lines.append(f"• {w['activity']}{mins}")

    if groceries:
        names = [g["name"] for g in groceries]
        shown = ", ".join(names[:8]) + (f" +{len(names) - 8} more" if len(names) > 8 else "")
        lines += ["", f"Running low on: {shown}"]

    if not (appointments or tasks or workouts):
        lines += ["", "Nothing on the list today. Enjoy the breathing room."]

    lines += ["", "Have a good one."]
    return "\n".join(lines)
