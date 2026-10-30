"""Calendar connections: a private iCal feed out of the app, and Google/Apple calendars into it."""

import ipaddress
import logging
import socket
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

log = logging.getLogger("getitdone.calendars")

CACHE_SECONDS = 15 * 60
MAX_BYTES = 5 * 1024 * 1024
FEED_HISTORY_DAYS = 90


class CalendarError(Exception):
    pass


def user_tz(user):
    try:
        return ZoneInfo(user["timezone"])
    except Exception:
        return timezone.utc


# ---------- fetching connected calendars ----------

def normalize_url(raw):
    url = str(raw or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CalendarError("Paste the full calendar link, starting with https:// or webcal://")
    return url


def _check_host(host):
    """Only fetch from public internet addresses, never the server's own network."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        raise CalendarError("We couldn't find that address.")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise CalendarError("That address isn't reachable from the internet.")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme not in ("http", "https"):
            raise CalendarError("That calendar link redirects somewhere unexpected.")
        _check_host(parsed.hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def parse(text):
    try:
        cal = icalendar.Calendar.from_ical(text)
    except Exception:
        raise CalendarError("That link doesn't point to a calendar file.")
    if cal.name != "VCALENDAR":
        raise CalendarError("That link doesn't point to a calendar file.")
    return cal


def fetch(url):
    _check_host(urlparse(url).hostname)
    req = urllib.request.Request(url, headers={"User-Agent": "GetItDone/1.0 (calendar sync)"})
    try:
        with _opener.open(req, timeout=15) as resp:
            data = resp.read(MAX_BYTES + 1)
    except CalendarError:
        raise
    except urllib.error.HTTPError as e:
        raise CalendarError(f"The calendar link returned an error ({e.code}). Make sure it's the secret or public link.")
    except Exception:
        raise CalendarError("We couldn't download that calendar.")
    if len(data) > MAX_BYTES:
        raise CalendarError("That calendar is too large to import.")
    text = data.decode("utf-8", errors="replace")
    parse(text)
    return text


def source_ics(conn, source):
    """The calendar's text, re-downloaded at most every 15 minutes. Falls back to the last good copy."""
    # fetched_at is also stamped on failures, so a broken link isn't retried on every page load.
    if time.time() - (source["fetched_at"] or 0) < CACHE_SECONDS:
        return source["ics_cache"]
    now = int(time.time())
    try:
        text = fetch(source["url"])
    except CalendarError as e:
        conn.execute("UPDATE calendar_sources SET last_error = ?, fetched_at = ? WHERE id = ?", (str(e), now, source["id"]))
        conn.commit()
        return source["ics_cache"]
    conn.execute(
        "UPDATE calendar_sources SET ics_cache = ?, fetched_at = ?, last_error = NULL WHERE id = ?",
        (text, now, source["id"]),
    )
    conn.commit()
    return text


def _occurrence_items(ev, source, tz, start, end):
    if str(ev.get("STATUS", "")).upper() == "CANCELLED" or ev.get("DTSTART") is None:
        return []
    base = {
        "source": source["name"],
        "source_id": source["id"],
        "title": str(ev.get("SUMMARY") or "Busy").strip(),
        "location": str(ev.get("LOCATION") or "").strip() or None,
    }
    dtstart = ev["DTSTART"].dt
    dtend = ev["DTEND"].dt if ev.get("DTEND") is not None else None

    if isinstance(dtstart, datetime):
        local = dtstart.astimezone(tz) if dtstart.tzinfo else dtstart.replace(tzinfo=tz)
        if not start <= local.date() <= end:
            return []
        end_time = None
        if isinstance(dtend, datetime):
            local_end = dtend.astimezone(tz) if dtend.tzinfo else dtend.replace(tzinfo=tz)
            if local_end.date() == local.date():
                end_time = local_end.strftime("%H:%M")
        return [{**base, "date": local.date().isoformat(), "time": local.strftime("%H:%M"),
                 "end_time": end_time, "all_day": False}]

    # All-day: DTEND is exclusive, and the event shows on every day it spans.
    last = dtend - timedelta(days=1) if type(dtend) is date and dtend > dtstart else dtstart
    items, d = [], max(dtstart, start)
    while d <= min(last, end):
        items.append({**base, "date": d.isoformat(), "time": None, "end_time": None, "all_day": True})
        d += timedelta(days=1)
    return items


def events_between(conn, user, start, end):
    """Events from the user's connected calendars on local dates start..end (inclusive)."""
    tz = user_tz(user)
    items = []
    sources = conn.execute("SELECT * FROM calendar_sources WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()
    for source in sources:
        text = source_ics(conn, source)
        if not text:
            continue
        try:
            # Widen by a day each side so time-zone shifts can't drop events at the edges.
            occurrences = recurring_ical_events.of(parse(text)).between(
                start - timedelta(days=1), end + timedelta(days=2)
            )
            for ev in occurrences:
                items.extend(_occurrence_items(ev, source, tz, start, end))
        except Exception:
            log.exception("Could not read calendar source %s", source["id"])
    items.sort(key=lambda e: (e["date"], not e["all_day"], e["time"] or ""))
    return items


# ---------- the app's own feed ----------

def build_feed(conn, user):
    tz = user_tz(user)
    since = (datetime.now(tz).date() - timedelta(days=FEED_HISTORY_DAYS)).isoformat()
    uid = user["id"]
    stamp = datetime.now(timezone.utc)

    cal = icalendar.Calendar()
    cal.add("prodid", "-//Get It Done//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Get It Done")
    cal.add("x-published-ttl", "PT30M")
    cal.add("refresh-interval", timedelta(minutes=30), parameters={"VALUE": "DURATION"})

    def add_event(key, summary, day, start_time=None, location=None, description=None):
        ev = icalendar.Event()
        ev.add("uid", f"{key}@getitdone")
        ev.add("dtstamp", stamp)
        ev.add("summary", summary)
        d = date.fromisoformat(day)
        if start_time:
            h, m = (int(x) for x in start_time.split(":"))
            begin = datetime.combine(d, dtime(h, m), tzinfo=tz).astimezone(timezone.utc)
            ev.add("dtstart", begin)
            ev.add("dtend", begin + timedelta(hours=1))
        else:
            ev.add("dtstart", d)
            ev.add("dtend", d + timedelta(days=1))
            ev.add("transp", "TRANSPARENT")  # all-day reminders shouldn't mark you as busy
        if location:
            ev.add("location", location)
        if description:
            ev.add("description", description)
        cal.add_component(ev)

    for a in conn.execute("SELECT * FROM appointments WHERE user_id = ? AND date >= ?", (uid, since)):
        add_event(f"appointment-{a['id']}", a["title"], a["date"], a["time"], a["location"])
    for t in conn.execute("SELECT * FROM tasks WHERE user_id = ? AND date >= ?", (uid, since)):
        add_event(f"task-{t['id']}", ("✓ " if t["done"] else "☐ ") + t["title"], t["date"], description="To do")
    for w in conn.execute("SELECT * FROM workouts WHERE user_id = ? AND date >= ?", (uid, since)):
        mins = f" · {w['duration']} min" if w["duration"] else ""
        add_event(f"workout-{w['id']}", ("✓ " if w["done"] else "") + w["activity"] + mins, w["date"], description="Exercise")

    return cal.to_ical()
