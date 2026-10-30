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
    log.warning("SECRET_KEY not set: using a temporary key, so sign-ins reset when the server restarts.")
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=60),
)
app.teardown_appcontext(db.close_db)

CODE_TTL = 10 * 60
CODE_RESEND_WAIT = 30
CODE_MAX_ATTEMPTS = 5

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


def hash_code(email, code):
    return hmac.new(app.config["SECRET_KEY"].encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return db.get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            if request.path.startswith("/api/"):
                return error("Not signed in", 401)
            return redirect(url_for("login"))
        g.user = user
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
        "id": user["id"],
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
@login_required
def home():
    return render_template("app.html", me=user_json(g.user))


@app.get("/login")
def login():
    if current_user():
        return redirect(url_for("home"))
    return render_template("login.html")


# ---------- auth ----------

@app.post("/auth/start")
def auth_start():
    email = normalize_email((request.get_json(silent=True) or {}).get("email"))
    if not email:
        return error("That doesn't look like an email address.")
    # Dev mode hands the code to the browser, so it must never happen on a real server.
    if not mailer.configured() and not app.debug:
        log.error("Sign-in attempted but SMTP isn't configured")
        return error("Sign-in emails aren't set up on this server yet.", 503)

    conn = db.get_db()
    now = int(time.time())
    existing = conn.execute("SELECT sent_at FROM login_codes WHERE email = ?", (email,)).fetchone()
    if existing and now - existing["sent_at"] < CODE_RESEND_WAIT:
        return error("A code was just sent. Give it a few seconds.", 429)

    code = f"{secrets.randbelow(10**6):06d}"
    conn.execute(
        "INSERT OR REPLACE INTO login_codes (email, code_hash, expires_at, sent_at, attempts) VALUES (?, ?, ?, ?, 0)",
        (email, hash_code(email, code), now + CODE_TTL, now),
    )
    conn.commit()

    try:
        mailer.send_email(
            email,
            f"{code} is your Get It Done code",
            f"Your sign-in code is {code}\n\nIt expires in 10 minutes. If you didn't ask for it, you can ignore this email.",
        )
    except Exception:
        log.exception("Could not send sign-in code")
        return error("We couldn't send an email to that address. Check it and try again.", 502)

    resp = {"ok": True, "email": email}
    if not mailer.configured():
        resp["dev_code"] = code  # handy for local testing; never returned once email is set up
    return jsonify(resp)


@app.post("/auth/verify")
def auth_verify():
    body = request.get_json(silent=True) or {}
    email = normalize_email(body.get("email"))
    code = re.sub(r"\D", "", str(body.get("code", "")))
    if not email or len(code) != 6:
        return error("Enter the 6-digit code we emailed you.")

    conn = db.get_db()
    row = conn.execute("SELECT * FROM login_codes WHERE email = ?", (email,)).fetchone()
    if not row or row["expires_at"] < time.time() or row["attempts"] >= CODE_MAX_ATTEMPTS:
        return error("That code has expired. Request a new one.")
    if not hmac.compare_digest(row["code_hash"], hash_code(email, code)):
        conn.execute("UPDATE login_codes SET attempts = attempts + 1 WHERE email = ?", (email,))
        conn.commit()
        return error("That code isn't right. Try again.")

    conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user is None:
        tz = body.get("timezone") if valid_timezone(body.get("timezone") or "") else "America/New_York"
        conn.execute("INSERT INTO users (email, timezone) VALUES (?, ?)", (email, tz))
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.commit()

    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    return jsonify(ok=True, needs_name=not user["name"])


@app.post("/auth/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


# ---------- account ----------

@app.get("/api/me")
@login_required
def me():
    return jsonify(user_json(g.user))


@app.patch("/api/me")
@login_required
def update_me():
    body = request.get_json(silent=True) or {}
    updates = {}
    if "name" in body:
        updates["name"] = str(body["name"] or "").strip()[:60]
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
    return jsonify(user_json(current_user()))


@app.delete("/api/me")
@login_required
def delete_me():
    conn = db.get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (g.user["id"],))
    conn.commit()
    session.clear()
    return jsonify(ok=True)


@app.post("/api/me/test-email")
@login_required
def test_email():
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
@login_required
def reset_feed():
    conn = db.get_db()
    conn.execute("UPDATE users SET feed_token = ? WHERE id = ?", (secrets.token_urlsafe(24), g.user["id"]))
    conn.commit()
    return jsonify(user_json(current_user()))


def source_json(row):
    return {"id": row["id"], "name": row["name"], "last_error": row["last_error"]}


@app.get("/api/calendars")
@login_required
def list_calendars():
    rows = db.get_db().execute(
        "SELECT * FROM calendar_sources WHERE user_id = ? ORDER BY id", (g.user["id"],)
    ).fetchall()
    return jsonify(items=[source_json(r) for r in rows])


@app.post("/api/calendars")
@login_required
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
@login_required
def delete_calendar(source_id):
    conn = db.get_db()
    conn.execute("DELETE FROM calendar_sources WHERE id = ? AND user_id = ?", (source_id, g.user["id"]))
    conn.commit()
    return jsonify(ok=True)


@app.get("/api/events")
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
def delete_item(kind, item_id):
    get_resource(kind)
    conn = db.get_db()
    conn.execute(f"DELETE FROM {kind} WHERE id = ? AND user_id = ?", (item_id, g.user["id"]))
    conn.commit()
    return jsonify(ok=True)


@app.post("/api/groceries/clear")
@login_required
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
