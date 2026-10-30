# Get It Done

A calm, minimal personal organizer:

- **Today**: tasks (unfinished ones carry over), today's appointments, exercise, and what's running low
- **Calendar**: month view with everything on it; tap a day to see or add to it
- **Groceries & supplies**: jot things down as they run out, check them off at the store
- **Appointments**: upcoming and past
- **Exercise**: plan and check off workouts, with a weekly total
- **Email sign-in**: a 6-digit code by email, no passwords
- **Morning email**: each user gets their personalised list at the time they choose, in their own time zone
- **Google & Apple Calendar**: subscribe to a private feed of your list from either app, and bring your
  Google/Apple calendars into the app (Settings → Calendars)

Built with Python (Flask) and SQLite. Email goes out over SMTP.

## Run it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then set SECRET_KEY (see the comment in the file)
.venv/bin/python app.py
```

Open http://localhost:5050.

Without SMTP settings the app runs in **dev mode**: sign-in codes are filled in on the page automatically, and every email is printed to the terminal instead of being sent. Settings → "Email me today's list now" shows a preview of the morning email.

## Turning on real email

Fill in the `SMTP_*` and `EMAIL_FROM` settings in `.env`. Any SMTP provider works:

- **Gmail (quickest for personal use):** `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, your Gmail address as
  `SMTP_USERNAME` and `EMAIL_FROM`, and an [App Password](https://myaccount.google.com/apppasswords) as
  `SMTP_PASSWORD` (requires 2-Step Verification). Gmail caps sending at roughly 500 emails a day.
- **A transactional service (better for other users):** Resend, Postmark, SendGrid and similar give you SMTP
  credentials. Verify your own domain with them so emails land in the inbox, not spam.

## Calendars

**Out of the app:** each user has a private feed link (`/cal/<secret>.ics`) that Google Calendar ("From URL")
or Apple Calendar ("New Calendar Subscription") can subscribe to. Apple refreshes as often as you set it to; Google refreshes on its
own schedule, usually every few hours. Google can only reach the feed once the app is online. Set `PUBLIC_URL`
(e.g. `https://getitdone.onrender.com`) if links come out with the wrong address behind a proxy.

**Into the app:** paste a calendar's secret/public iCal link in Settings. Events (including repeating ones) appear on the
Calendar and Today pages and in the morning email. Calendars are re-downloaded at most every 15 minutes, and only from
public internet addresses. These events are read-only in the app.

## Deploying

Any host that runs a long-lived Python process with a persistent disk works (Render, Railway, Fly.io, a small VPS).

- Start command: `gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT app:app`
- Set `SECRET_KEY`, the email settings, `SECURE_COOKIES=1`, and `DATABASE_PATH` pointing at the persistent disk.
- The morning-email scheduler runs inside the web process and checks every minute. Use **one** worker
  (`-w 1`), or set `RUN_SCHEDULER=0` on all but one process. Sends are de-duplicated in the database either way, so
  nobody gets two emails on the same day.
- If the server is down at someone's send time, their email still goes out once it's back, as long as that's within 3 hours.

Serverless hosts (e.g. Vercel) aren't a good fit as-is, because SQLite and the in-process scheduler need a persistent server.

## Project layout

```
app.py          routes: pages, email sign-in, account settings, JSON API for lists
db.py           SQLite connection + schema setup
schema.sql      tables
mailer.py       sending email (SMTP or dev console) and composing the morning email
calendars.py    calendar feed out, connected Google/Apple calendars in
scheduler.py    once-a-minute job that sends each user's morning email
templates/      login + app shell
static/         style.css, app.js (vanilla JS, no build step)
```
