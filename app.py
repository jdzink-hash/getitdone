import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, session, url_for  # noqa: E402

import calendars  # noqa: E402
import db  # noqa: E402
import scheduler  # noqa: E402
import mailer  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("getitdone")

app = Flask(__name__)
if os.environ.get("TRUST_PROXY") == "1":
    # Behind a host's proxy (e.g. Render), trust its headers so links use https and the real hostname.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
if not os.environ.get("SECRET_KEY"):
    log.warning("SECRET_KEY not set: using a temporary key, so remembered devices reset when the server restarts.")
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),
)
app.teardown_appcontext(db.close_db)

# The whole site belongs to one person. Opening /?key=<ACCESS_KEY> once remembers the device.
ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
OWNER_NAME = os.environ.get("OWNER_NAME") or "Andrina"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---------- helpers ----------

def error(message, status=400):
    return jsonify(error=message), status


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(raw):
    email = str(raw or "").strip().lower()
    return email if len(email) <= 254 and EMAIL_RE.match(email) else None


def valid_timezone(name):
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def access_fingerprint():
    # Stored in the cookie instead of the key itself; changing ACCESS_KEY forgets every device.
    return hashlib.sha256(f"access:{ACCESS_KEY}".encode()).hexdigest()


def has_access():
    if not ACCESS_KEY:
        return app.debug  # open when running locally; a real server without a key stays locked
    return hmac.compare_digest(session.get("access", ""), access_fingerprint())


def owner():
    conn = db.get_db()
    user = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    if user is None:
        tz = os.environ.get("OWNER_TIMEZONE") or "America/New_York"
        conn.execute(
            "INSERT INTO users (email, name, timezone) VALUES (?, ?, ?)",
            (normalize_email(os.environ.get("OWNER_EMAIL")) or "", OWNER_NAME, tz if valid_timezone(tz) else "UTC"),
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    return user


def access_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not has_access():
            if request.path.startswith("/api/"):
                return error("This list is private.", 403)
            return render_template("private.html"), 403
        g.user = owner()
        return view(*args, **kwargs)

    return wrapped


def feed_url(user):
    token = user["feed_token"]
    if not token:
        token = secrets.token_urlsafe(24)
        conn = db.get_db()
        conn.execute("UPDATE users SET feed_token = ? WHERE id = ?", (token, user["id"]))
        conn.commit()
    base = os.environ.get("PUBLIC_URL") or request.url_root
    return base.rstrip("/") + url_for("calendar_feed", token=token)


def user_json(user):
    return {
        "email": user["email"],
        "name": user["name"],
        "timezone": user["timezone"],
        "send_time": user["send_time"],
        "digest_enabled": bool(user["digest_enabled"]),
        "feed_url": feed_url(user),
    }


@app.before_request
def csrf_guard():
    # Cross-site forms can't set custom headers, so requiring one blocks CSRF on state-changing calls.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Requested-With") != "fetch":
        abort(403)


# ---------- pages ----------

@app.get("/")
def home():
    key = request.args.get("key")
    if key is not None:
        if ACCESS_KEY and hmac.compare_digest(key, ACCESS_KEY):
            session.permanent = True
            session["access"] = access_fingerprint()
        return redirect(url_for("home"))  # drop the key from the address bar either way
    return show_app()


@access_required
def show_app():
    return render_template("app.html", me=user_json(g.user))


# ---------- settings ----------

@app.get("/api/me")
@access_required
def me():
    return jsonify(user_json(g.user))


@app.patch("/api/me")
@access_required
def update_me():
    body = request.get_json(silent=True) or {}
    updates = {}
    if "name" in body:
        updates["name"] = str(body["name"] or "").strip()[:60]
    if "email" in body:
        raw = str(body["email"] or "").strip()
        if raw and not normalize_email(raw):
            return error("That doesn't look like an email address.")
        updates["email"] = normalize_email(raw) or ""
    if "timezone" in body:
        if not valid_timezone(str(body["timezone"])):
            return error("Unknown timezone.")
        updates["timezone"] = body["timezone"]
    if "send_time" in body:
        if not TIME_RE.match(str(body["send_time"])):
            return error("Pick a time like 07:00.")
        updates["send_time"] = body["send_time"]
    if "digest_enabled" in body:
        updates["digest_enabled"] = 1 if body["digest_enabled"] else 0
    if updates:
        conn = db.get_db()
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*updates.values(), g.user["id"]))
        conn.commit()
    return jsonify(user_json(owner()))


@app.post("/api/me/test-email")
@access_required
def test_email():
    if not g.user["email"]:
        return error("Add an email address first.")
    last = session.get("last_test_email", 0)
    if time.time() - last < 60:
        return error("You just sent one. Try again in a minute.", 429)
    session["last_test_email"] = time.time()
    try:
        local_day = datetime.now(timezone.utc).astimezone(ZoneInfo(g.user["timezone"])).date()
    except Exception:
        local_day = datetime.now(timezone.utc).date()
    body = mailer.compose_digest(db.get_db(), g.user, local_day)
    try:
        mailer.send_email(g.user["email"], mailer.digest_subject(local_day), body)
    except Exception:
        log.exception("Test email failed")
        return error("The email couldn't be sent. Check your SMTP settings.", 502)
    return jsonify(ok=True, preview=body, sent=mailer.configured())


# ---------- calendars ----------

@app.get("/cal/<token>.ics")
def calendar_feed(token):
    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE feed_token = ?", (token,)).fetchone()
    if user is None:
        abort(404)
    return Response(
        calendars.build_feed(conn, user),
        mimetype="text/calendar",
        headers={"Content-Disposition": "inline; filename=getitdone.ics", "Cache-Control": "no-cache"},
    )


@app.post("/api/me/feed/reset")
@access_required
def reset_feed():
    conn = db.get_db()
    conn.execute("UPDATE users SET feed_token = ? WHERE id = ?", (secrets.token_urlsafe(24), g.user["id"]))
    conn.commit()
    return jsonify(user_json(owner()))


def source_json(row):
    return {"id": row["id"], "name": row["name"], "last_error": row["last_error"]}


@app.get("/api/calendars")
@access_required
def list_calendars():
    rows = db.get_db().execute(
        "SELECT * FROM calendar_sources WHERE user_id = ? ORDER BY id", (g.user["id"],)
    ).fetchall()
    return jsonify(items=[source_json(r) for r in rows])


@app.post("/api/calendars")
@access_required
def add_calendar():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:60] or "Calendar"
    try:
        url = calendars.normalize_url(body.get("url"))
        text = calendars.fetch(url)
    except calendars.CalendarError as e:
        return error(str(e))
    conn = db.get_db()
    if conn.execute("SELECT COUNT(*) FROM calendar_sources WHERE user_id = ?", (g.user["id"],)).fetchone()[0] >= 10:
        return error("You can connect up to 10 calendars.")
    cur = conn.execute(
        "INSERT INTO calendar_sources (user_id, name, url, ics_cache, fetched_at) VALUES (?, ?, ?, ?, ?)",
        (g.user["id"], name, url, text, int(time.time())),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM calendar_sources WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(source_json(row)), 201


@app.delete("/api/calendars/<int:source_id>")
@access_required
def delete_calendar(source_id):
    conn = db.get_db()
    conn.execute("DELETE FROM calendar_sources WHERE id = ? AND user_id = ?", (source_id, g.user["id"]))
    conn.commit()
    return jsonify(ok=True)


@app.get("/api/events")
@access_required
def list_events():
    start, end = request.args.get("from", ""), request.args.get("to", "")
    if not (DATE_RE.match(start) and DATE_RE.match(end)):
        return error("from and to dates are required.")
    start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
    if not timedelta(0) <= end_d - start_d <= timedelta(days=62):
        return error("Pick a range of up to two months.")
    return jsonify(items=calendars.events_between(db.get_db(), g.user, start_d, end_d))


# ---------- lists: tasks, groceries, appointments, workouts ----------

RESOURCES = {
    "tasks": {
        "fields": {"title": "text", "date": "date", "done": "bool"},
        "required": ["title"],
        "order": "done, date IS NULL, date, id",
    },
    "groceries": {
        "fields": {"name": "text", "done": "bool"},
        "required": ["name"],
        "order": "done, id DESC",
    },
    "appointments": {
        "fields": {"title": "text", "date": "date", "time": "time", "location": "text"},
        "required": ["title", "date"],
        "order": "date, time IS NULL, time, id",
    },
    "workouts": {
        "fields": {"activity": "text", "date": "date", "duration": "int", "done": "bool"},
        "required": ["activity", "date"],
        "order": "date, id",
    },
}


def clean(kind, body, partial):
    spec = RESOURCES[kind]
    out = {}
    for field, ftype in spec["fields"].items():
        if field not in body:
            continue
        v = body[field]
        if ftype == "text":
            v = str(v or "").strip()[:200] or None
        elif ftype == "date":
            v = v or None
            if v is not None and not DATE_RE.match(str(v)):
                raise ValueError(f"Invalid {field}.")
        elif ftype == "time":
            v = v or None
            if v is not None and not TIME_RE.match(str(v)):
                raise ValueError(f"Invalid {field}.")
        elif ftype == "int":
            if v in (None, ""):
                v = None
            else:
                try:
                    v = max(0, min(int(v), 1440))
                except (TypeError, ValueError):
                    raise ValueError(f"Invalid {field}.")
        elif ftype == "bool":
            v = 1 if v else 0
        out[field] = v
    for field in spec["required"]:
        if (not partial or field in out) and not out.get(field):
            raise ValueError(f"{field.capitalize()} is required.")
    return out


def row_json(kind, row):
    item = dict(row)
    item.pop("user_id", None)
    if "done" in item:
        item["done"] = bool(item["done"])
    return item


def get_resource(kind):
    if kind not in RESOURCES:
        abort(404)
    return RESOURCES[kind]


@app.get("/api/<kind>")
@access_required
def list_items(kind):
    spec = get_resource(kind)
    where, params = ["user_id = ?"], [g.user["id"]]
    if "date" in spec["fields"]:
        for arg, op in (("from", ">="), ("to", "<="), ("before", "<")):
            val = request.args.get(arg)
            if val:
                if not DATE_RE.match(val):
                    return error(f"Invalid {arg} date.")
                where.append(f"date {op} ?")
                params.append(val)
    if "done" in spec["fields"] and request.args.get("done") in ("0", "1"):
        where.append("done = ?")
        params.append(int(request.args["done"]))
    rows = db.get_db().execute(
        f"SELECT * FROM {kind} WHERE {' AND '.join(where)} ORDER BY {spec['order']}", params
    ).fetchall()
    return jsonify(items=[row_json(kind, r) for r in rows])


@app.post("/api/<kind>")
@access_required
def create_item(kind):
    get_resource(kind)
    try:
        data = clean(kind, request.get_json(silent=True) or {}, partial=False)
    except ValueError as e:
        return error(str(e))
    conn = db.get_db()
    cols = ["user_id", *data.keys()]
    cur = conn.execute(
        f"INSERT INTO {kind} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        (g.user["id"], *data.values()),
    )
    conn.commit()
    row = conn.execute(f"SELECT * FROM {kind} WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(row_json(kind, row)), 201


@app.patch("/api/<kind>/<int:item_id>")
@access_required
def update_item(kind, item_id):
    get_resource(kind)
    try:
        data = clean(kind, request.get_json(silent=True) or {}, partial=True)
    except ValueError as e:
        return error(str(e))
    conn = db.get_db()
    if data:
        sets = ", ".join(f"{k} = ?" for k in data)
        conn.execute(
            f"UPDATE {kind} SET {sets} WHERE id = ? AND user_id = ?", (*data.values(), item_id, g.user["id"])
        )
        conn.commit()
    row = conn.execute(f"SELECT * FROM {kind} WHERE id = ? AND user_id = ?", (item_id, g.user["id"])).fetchone()
    if row is None:
        return error("Not found", 404)
    return jsonify(row_json(kind, row))


@app.delete("/api/<kind>/<int:item_id>")
@access_required
def delete_item(kind, item_id):
    get_resource(kind)
    conn = db.get_db()
    conn.execute(f"DELETE FROM {kind} WHERE id = ? AND user_id = ?", (item_id, g.user["id"]))
    conn.commit()
    return jsonify(ok=True)


@app.post("/api/groceries/clear")
@access_required
def clear_groceries():
    conn = db.get_db()
    conn.execute("DELETE FROM groceries WHERE user_id = ? AND done = 1", (g.user["id"],))
    conn.commit()
    return jsonify(ok=True)


# ---------- startup ----------

db.init_db()


def maybe_start_scheduler():
    if os.environ.get("RUN_SCHEDULER", "1") == "1":
        scheduler.start()


if __name__ == "__main__":
    # The debug reloader runs a watcher process plus a server process; schedule only in the server.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        maybe_start_scheduler()
    app.run(debug=True, port=int(os.environ.get("PORT", 5050)))
else:
    maybe_start_scheduler()
