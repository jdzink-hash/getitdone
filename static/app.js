"use strict";

/* ---------- tiny helpers ---------- */

const $ = (sel, el = document) => el.querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "X-Requested-With": "fetch", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("Signed out");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong.");
  return data;
}

let toastTimer;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

// Run an async action, surfacing any error as a toast.
const attempt = (fn) => async (...args) => {
  try { await fn(...args); } catch (err) { toast(err.message); }
};

/* ---------- dates ---------- */

const pad = (n) => String(n).padStart(2, "0");
const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parse = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const today = () => iso(new Date());
const addDays = (s, n) => { const d = parse(s); d.setDate(d.getDate() + n); return iso(d); };
const fmtDay = (s, opts = { weekday: "long", month: "long", day: "numeric" }) =>
  parse(s).toLocaleDateString(undefined, opts);
const fmtShort = (s) => fmtDay(s, { weekday: "short", month: "short", day: "numeric" });

function fmtTime(t) {
  if (!t) return "";
  let [hh, mm] = t.split(":").map(Number);
  const ap = hh >= 12 ? "pm" : "am";
  hh = hh % 12 || 12;
  return `${hh}${mm ? ":" + pad(mm) : ""}${ap}`;
}

const fmtRange = (e) => e.all_day ? "All day" : fmtTime(e.time) + (e.end_time ? "–" + fmtTime(e.end_time) : "");

// Events from connected Google/Apple calendars (read-only).
const fetchEvents = async (from, to) => (await api("GET", `/api/events?from=${from}&to=${to}`)).items;

function relDay(s) {
  const t = today();
  if (s === t) return "Today";
  if (s === addDays(t, 1)) return "Tomorrow";
  if (s === addDays(t, -1)) return "Yesterday";
  return fmtDay(s);
}

function weekStart(s) {
  const d = parse(s);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); // Monday
  return iso(d);
}

/* ---------- building blocks ---------- */

function section(title, { note, action } = {}, ...body) {
  return h("section", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, title), action || (note && h("span", { class: "section-note" }, note))),
    ...body);
}

// A list that knows how to reload itself without re-rendering the surrounding form.
function liveList(load, renderItem, emptyText) {
  const ul = h("ul", { class: "list" });
  const refresh = attempt(async () => {
    const items = await load();
    ul.replaceChildren(...(items.length
      ? items.map((it) => renderItem(it, refresh))
      : [h("li", { class: "empty" }, emptyText)]));
  });
  refresh();
  return { ul, refresh };
}

function checkRow(kind, item, label, meta, refresh, extra) {
  return h("li", { class: "item" + (item.done ? " done" : "") },
    h("button", {
      class: "check",
      "aria-label": item.done ? "Mark as not done" : "Mark as done",
      onclick: attempt(async () => { await api("PATCH", `/api/${kind}/${item.id}`, { done: !item.done }); refresh(); }),
    }),
    h("div", { class: "item-body" }, h("span", { class: "item-label" }, label, extra), meta && h("span", { class: "item-meta" }, meta)),
    removeBtn(kind, item, refresh));
}

function removeBtn(kind, item, refresh) {
  return h("button", {
    class: "remove",
    "aria-label": "Delete",
    onclick: attempt(async () => { await api("DELETE", `/api/${kind}/${item.id}`); refresh(); }),
  }, "×");
}

function quickAdd(placeholder, onAdd, ...extras) {
  const input = h("input", { type: "text", placeholder, maxLength: 200, "aria-label": placeholder });
  return h("form", {
    class: "quick-add",
    onsubmit: attempt(async (e) => {
      e.preventDefault();
      const value = input.value.trim();
      if (!value) return;
      await onAdd(value);
      input.value = "";
      input.focus();
    }),
  }, h("span", { class: "plus" }, "+"), input, ...extras);
}

/* ---------- views ---------- */

const views = {};

views.today = () => {
  const t = today();

  const tasks = liveList(async () => {
    const [overdue, todays] = await Promise.all([
      api("GET", `/api/tasks?before=${t}&done=0`),
      api("GET", `/api/tasks?from=${t}&to=${t}`),
    ]);
    return [...overdue.items, ...todays.items].sort((a, b) => a.done - b.done);
  }, (task, refresh) => checkRow("tasks", task, task.title, null, refresh,
    task.date < t && h("span", { class: "badge" }, fmtShort(task.date))),
  "Nothing yet. Add the first thing.");

  const appts = liveList(async () => {
    const [own, events] = await Promise.all([api("GET", `/api/appointments?from=${t}&to=${t}`), fetchEvents(t, t)]);
    // Untimed items first, then by time.
    return [...events, ...own.items].sort((a, b) => !!a.time - !!b.time || (a.time || "").localeCompare(b.time || ""));
  }, (a, refresh) => h("li", { class: "item" },
    h("span", { class: "time-col" }, a.source ? fmtTime(a.time) || "All day" : fmtTime(a.time) || "Any time"),
    h("div", { class: "item-body" }, h("span", { class: "item-label" }, a.title),
      (a.location || a.source) && h("span", { class: "item-meta" }, [a.location, a.source].filter(Boolean).join(" · "))),
    !a.source && removeBtn("appointments", a, refresh)),
  "No appointments today.");

  const minutes = h("input", { type: "number", min: 0, max: 600, placeholder: "min", "aria-label": "Minutes" });
  const moves = liveList(
    async () => (await api("GET", `/api/workouts?from=${t}&to=${t}`)).items,
    (w, refresh) => checkRow("workouts", w, w.activity, w.duration ? `${w.duration} min` : null, refresh),
    "No movement planned. A walk counts!");

  const low = h("div", { class: "chips" });
  attempt(async () => {
    const { items } = await api("GET", "/api/groceries?done=0");
    low.replaceChildren(...(items.length
      ? items.map((g) => h("span", { class: "chip" }, g.name))
      : [h("span", { class: "section-note" }, "Everything's stocked.")]));
  })();

  return [
    section("To do", {},
      quickAdd("Add a task for today", async (title) => { await api("POST", "/api/tasks", { title, date: t }); tasks.refresh(); }),
      tasks.ul),
    section("Appointments", { action: h("a", { class: "link-btn", href: "#appointments" }, "Add") }, appts.ul),
    section("Movement", {},
      quickAdd("Add exercise", async (activity) => {
        await api("POST", "/api/workouts", { activity, date: t, duration: minutes.value });
        minutes.value = "";
        moves.refresh();
      }, minutes),
      moves.ul),
    section("Running low", { action: h("a", { class: "link-btn", href: "#groceries" }, "Edit") }, low),
  ];
};

const cal = { month: null, selected: null };

views.calendar = () => {
  if (!cal.month) { const d = new Date(); cal.month = new Date(d.getFullYear(), d.getMonth(), 1); }
  cal.selected ||= today();

  const wrap = h("div");
  const agenda = h("div");

  const drawAgenda = () => {
    const day = cal.selected;
    const list = liveList(async () => {
      const [tasks, appts, workouts, events] = await Promise.all([
        ...["tasks", "appointments", "workouts"].map((k) => api("GET", `/api/${k}?from=${day}&to=${day}`)),
        fetchEvents(day, day),
      ]);
      return [
        ...events.map((x) => ({ ...x, kind: "events" })),
        ...appts.items.map((x) => ({ ...x, kind: "appointments" })),
        ...tasks.items.map((x) => ({ ...x, kind: "tasks" })),
        ...workouts.items.map((x) => ({ ...x, kind: "workouts" })),
      ];
    }, (it, refresh) => {
      const after = () => { refresh(); drawGrid(); };
      if (it.kind === "events") {
        return h("li", { class: "item" },
          h("span", { class: "dot event" }),
          h("div", { class: "item-body" }, h("span", { class: "item-label" }, it.title),
            h("span", { class: "item-meta" }, [fmtRange(it), it.location, it.source].filter(Boolean).join(" · "))));
      }
      if (it.kind === "appointments") {
        return h("li", { class: "item" },
          h("span", { class: "dot appointment" }),
          h("div", { class: "item-body" }, h("span", { class: "item-label" }, it.title),
            h("span", { class: "item-meta" }, [fmtTime(it.time) || "Any time", it.location].filter(Boolean).join(" · "))),
          removeBtn("appointments", it, after));
      }
      const label = it.kind === "tasks" ? it.title : it.activity;
      const meta = it.kind === "workouts" ? ["Exercise", it.duration && `${it.duration} min`].filter(Boolean).join(" · ") : null;
      return checkRow(it.kind, it, label, meta, after);
    }, "Nothing planned.");

    agenda.replaceChildren(section(relDay(day), { note: day === today() ? null : fmtShort(day) },
      quickAdd(`Add a task for ${day === today() ? "today" : fmtShort(day)}`, async (title) => {
        await api("POST", "/api/tasks", { title, date: day });
        list.refresh();
        drawGrid();
      }),
      list.ul));
  };

  const drawGrid = attempt(async () => {
    const first = cal.month;
    const start = weekStart(iso(first));
    const end = addDays(start, 41);
    const [tasks, appts, workouts, events] = await Promise.all([
      ...["tasks", "appointments", "workouts"].map((k) => api("GET", `/api/${k}?from=${start}&to=${end}`)),
      fetchEvents(start, end),
    ]);
    const marks = {};
    const mark = (d, cls) => { (marks[d] ||= new Set()).add(cls); };
    tasks.items.forEach((x) => !x.done && mark(x.date, "task"));
    appts.items.forEach((x) => mark(x.date, "appointment"));
    workouts.items.forEach((x) => mark(x.date, "workout"));
    events.forEach((x) => mark(x.date, "event"));

    const cells = [];
    for (let i = 0; i < 42; i++) {
      const d = addDays(start, i);
      const date = parse(d);
      const cls = ["cal-day",
        date.getMonth() !== first.getMonth() && "other",
        d === today() && "today",
        d === cal.selected && "selected"].filter(Boolean).join(" ");
      cells.push(h("button", {
        class: cls,
        "aria-label": fmtDay(d),
        onclick: () => { cal.selected = d; drawGrid(); drawAgenda(); },
      }, h("span", { class: "num" }, date.getDate()),
      h("span", { class: "dots" }, [...(marks[d] || [])].map((c) => h("span", { class: `dot ${c}` })))));
    }

    const shift = (n) => () => { cal.month = new Date(first.getFullYear(), first.getMonth() + n, 1); drawGrid(); };
    wrap.replaceChildren(
      h("div", { class: "cal-head" },
        h("h2", {}, first.toLocaleDateString(undefined, { month: "long", year: "numeric" })),
        h("div", { class: "cal-nav" },
          h("button", { onclick: shift(-1), "aria-label": "Previous month" }, "‹"),
          h("button", { onclick: () => { const d = new Date(); cal.month = new Date(d.getFullYear(), d.getMonth(), 1); cal.selected = today(); drawGrid(); drawAgenda(); } }, "Today"),
          h("button", { onclick: shift(1), "aria-label": "Next month" }, "›"))),
      h("div", { class: "cal-grid" },
        ...["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => h("div", { class: "cal-dow" }, d)),
        ...cells),
      h("div", { class: "legend" },
        h("span", {}, h("span", { class: "dot task" }), "To do"),
        h("span", {}, h("span", { class: "dot appointment" }), "Appointment"),
        h("span", {}, h("span", { class: "dot workout" }), "Exercise"),
        h("span", {}, h("span", { class: "dot event" }), "Your calendars")));
  });

  drawGrid();
  drawAgenda();
  return [wrap, agenda];
};

views.groceries = () => {
  const list = liveList(
    async () => (await api("GET", "/api/groceries")).items,
    (g, refresh) => checkRow("groceries", g, g.name, null, refresh),
    "Nothing on the list. Add things as they run out.");

  return [section("Running low", {
    action: h("button", {
      class: "link-btn",
      onclick: attempt(async () => { await api("POST", "/api/groceries/clear"); list.refresh(); }),
    }, "Clear checked"),
  },
  quickAdd("Add groceries or supplies", async (name) => { await api("POST", "/api/groceries", { name }); list.refresh(); }),
  list.ul)];
};

views.appointments = () => {
  const t = today();
  const title = h("input", { class: "field full", placeholder: "What's the appointment?", maxLength: 200, required: true });
  const date = h("input", { class: "field", type: "date", value: t, required: true, "aria-label": "Date" });
  const time = h("input", { class: "field", type: "time", "aria-label": "Time" });
  const location = h("input", { class: "field full", placeholder: "Where? (optional)", maxLength: 200 });

  const grouped = (items, refresh) => {
    const out = [];
    let last = null;
    for (const a of items) {
      if (a.date !== last) {
        out.push(h("li", { class: "item-meta", style: "padding: 18px 2px 4px" }, relDay(a.date)));
        last = a.date;
      }
      out.push(h("li", { class: "item" },
        h("span", { class: "time-col" }, fmtTime(a.time) || "Any time"),
        h("div", { class: "item-body" }, h("span", { class: "item-label" }, a.title), a.location && h("span", { class: "item-meta" }, a.location)),
        removeBtn("appointments", a, refresh)));
    }
    return out;
  };

  const ul = h("ul", { class: "list" });
  let showPast = false;
  const refresh = attempt(async () => {
    const q = showPast ? `to=${addDays(t, -1)}` : `from=${t}`;
    let { items } = await api("GET", `/api/appointments?${q}`);
    if (showPast) items = items.reverse();
    ul.replaceChildren(...(items.length ? grouped(items, refresh)
      : [h("li", { class: "empty" }, showPast ? "No past appointments." : "Nothing coming up.")]));
  });
  refresh();

  const toggle = h("button", {
    class: "link-btn",
    onclick: () => { showPast = !showPast; toggle.textContent = showPast ? "Show upcoming" : "Show past"; refresh(); },
  }, "Show past");

  const form = h("form", {
    class: "form-grid",
    onsubmit: attempt(async (e) => {
      e.preventDefault();
      await api("POST", "/api/appointments", { title: title.value, date: date.value, time: time.value, location: location.value });
      title.value = ""; time.value = ""; location.value = "";
      toast("Appointment added");
      refresh();
    }),
  }, title, date, time, location, h("div", { class: "full" }, h("button", { class: "btn", type: "submit" }, "Add appointment")));

  return [
    section("New appointment", {}, form),
    section("Upcoming", { action: toggle }, ul),
  ];
};

views.exercise = () => {
  const t = today();
  const from = weekStart(t);
  const date = h("input", { type: "date", value: t, class: "small", "aria-label": "Date" });
  const minutes = h("input", { type: "number", min: 0, max: 600, placeholder: "min", "aria-label": "Minutes" });
  const stats = h("span", { class: "section-note" });

  const list = liveList(async () => {
    const { items } = await api("GET", `/api/workouts?from=${from}`);
    const done = items.filter((w) => w.done && w.date <= addDays(from, 6));
    const mins = done.reduce((s, w) => s + (w.duration || 0), 0);
    stats.textContent = `${done.length} session${done.length === 1 ? "" : "s"} · ${mins} min this week`;
    return items;
  }, (w, refresh) => checkRow("workouts", w, w.activity,
    [relDay(w.date), w.duration && `${w.duration} min`].filter(Boolean).join(" · "), refresh),
  "Plan a walk, a run, a stretch. Anything counts.");

  return [section("Exercise", { action: stats },
    quickAdd("Run, yoga, pilates…", async (activity) => {
      await api("POST", "/api/workouts", { activity, date: date.value || t, duration: minutes.value });
      minutes.value = "";
      list.refresh();
    }, minutes, date),
    list.ul)];
};

views.settings = () => {
  const me = window.ME;
  const save = attempt(async (patch) => {
    Object.assign(window.ME, await api("PATCH", "/api/me", patch));
    renderHeader();
    toast("Saved");
  });

  const zones = (Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : [me.timezone]);
  if (!zones.includes(me.timezone)) zones.unshift(me.timezone);

  const preview = h("div");
  const setting = (label, hint, control) =>
    h("div", { class: "setting" }, h("div", { class: "label" }, label, hint && h("span", { class: "hint" }, hint)), control);

  return [
    section("Morning email", {},
      setting("Send me my day", "An email each morning with your list.",
        h("label", { class: "toggle" },
          h("input", { type: "checkbox", checked: me.digest_enabled, onchange: (e) => save({ digest_enabled: e.target.checked }) }),
          h("span"))),
      setting("Send at", null,
        h("input", { class: "field", type: "time", value: me.send_time, onchange: (e) => e.target.value && save({ send_time: e.target.value }) })),
      setting("Time zone", null,
        h("select", { class: "field", onchange: (e) => save({ timezone: e.target.value }) },
          zones.map((z) => h("option", { value: z, selected: z === me.timezone }, z.replace(/_/g, " "))))),
      h("div", { style: "padding-top:18px" },
        h("button", {
          class: "btn ghost",
          onclick: attempt(async (e) => {
            e.target.disabled = true;
            try {
              const res = await api("POST", "/api/me/test-email");
              toast(res.sent ? "Email sent" : "Dev mode: preview below (not sent)");
              preview.replaceChildren(h("div", { class: "preview" }, res.preview));
            } finally { e.target.disabled = false; }
          }),
        }, "Email me today's list now")),
      preview),
    calendarSettings(),
    section("Account", {},
      setting("Name", null,
        h("input", { class: "field", value: me.name, maxLength: 60, onchange: (e) => save({ name: e.target.value }) })),
      setting("Email", null, h("span", { class: "section-note" }, me.email)),
      h("div", { style: "display:flex; gap:12px; padding-top:18px; align-items:center" },
        h("button", { class: "btn ghost", onclick: attempt(async () => { await api("POST", "/auth/logout"); location.href = "/login"; }) }, "Sign out"),
        h("button", {
          class: "btn danger",
          onclick: attempt(async () => {
            if (!confirm("Delete your account and everything in it? This can't be undone.")) return;
            await api("DELETE", "/api/me");
            location.href = "/login";
          }),
        }, "Delete account"))),
  ];
};

function calendarSettings() {
  const me = window.ME;

  // Out: subscribe to the app from Google / Apple Calendar.
  const feedInput = h("input", { class: "field", readOnly: true, value: me.feed_url, "aria-label": "Your calendar link", onfocus: (e) => e.target.select() });
  const webcal = () => me.feed_url.replace(/^https?:/, "webcal:");
  const appleLink = h("a", { class: "btn ghost", href: webcal() }, "Add to Apple Calendar");
  const googleLink = h("a", { class: "btn ghost", target: "_blank", rel: "noopener",
    href: "https://calendar.google.com/calendar/r?cid=" + encodeURIComponent(webcal()) }, "Add to Google Calendar");
  const refreshLinks = () => {
    feedInput.value = me.feed_url;
    appleLink.href = webcal();
    googleLink.href = "https://calendar.google.com/calendar/r?cid=" + encodeURIComponent(webcal());
  };
  const isLocal = /\/\/(localhost|127\.0\.0\.1)[:/]/.test(me.feed_url);

  // In: show Google / Apple calendars here.
  const sources = h("ul", { class: "list" });
  const loadSources = attempt(async () => {
    const { items } = await api("GET", "/api/calendars");
    sources.replaceChildren(...(items.length ? items.map((c) => h("li", { class: "item" },
      h("span", { class: "dot event" }),
      h("div", { class: "item-body" }, h("span", { class: "item-label" }, c.name),
        c.last_error && h("span", { class: "item-meta error-text" }, c.last_error)),
      h("button", { class: "remove", "aria-label": `Disconnect ${c.name}`,
        onclick: attempt(async () => { await api("DELETE", `/api/calendars/${c.id}`); loadSources(); }) }, "×")))
      : [h("li", { class: "empty" }, "No calendars connected yet.")]));
  });
  loadSources();

  const name = h("input", { class: "field", placeholder: "Name, e.g. Work", maxLength: 60, "aria-label": "Calendar name" });
  const url = h("input", { class: "field full", placeholder: "Paste the calendar link (https://… or webcal://…)", required: true, "aria-label": "Calendar link" });
  const addBtn = h("button", { class: "btn", type: "submit" }, "Connect calendar");
  const form = h("form", {
    class: "form-grid",
    style: "margin-top:18px",
    onsubmit: attempt(async (e) => {
      e.preventDefault();
      addBtn.disabled = true;
      addBtn.textContent = "Connecting…";
      try {
        await api("POST", "/api/calendars", { name: name.value, url: url.value });
        name.value = ""; url.value = "";
        toast("Calendar connected");
        loadSources();
      } finally {
        addBtn.disabled = false;
        addBtn.textContent = "Connect calendar";
      }
    }),
  }, url, name, addBtn);

  return section("Calendars", {},
    h("p", { class: "hint-block" }, "See your tasks, appointments and exercise in Google or Apple Calendar. They update automatically."),
    h("div", { class: "copy-row" },
      feedInput,
      h("button", { class: "btn ghost", onclick: attempt(async () => { await navigator.clipboard.writeText(me.feed_url); toast("Link copied"); }) }, "Copy")),
    h("div", { class: "button-row" }, appleLink, googleLink),
    isLocal && h("p", { class: "hint-block small-note" }, "Google can only use this link once the app is online. Apple Calendar on this computer works now."),
    h("p", { class: "hint-block small-note" }, "Keep this link private: anyone with it can see your list. ",
      h("button", { class: "link-btn", onclick: attempt(async () => {
        if (!confirm("Make a new link? Calendars using the old one will stop updating.")) return;
        Object.assign(me, await api("POST", "/api/me/feed/reset"));
        refreshLinks();
        toast("New link created");
      }) }, "Reset link")),

    h("h3", { class: "subhead" }, "Show your calendars here"),
    h("p", { class: "hint-block" }, "Bring your Google or Apple events into your calendar, Today page and morning email."),
    h("details", { class: "help" },
      h("summary", {}, "Where do I find the link?"),
      h("p", {}, h("strong", {}, "Google Calendar: "), "on a computer, open Settings → click your calendar under “Settings for my calendars” → “Integrate calendar” → copy the “Secret address in iCal format”."),
      h("p", {}, h("strong", {}, "Apple Calendar: "), "on iPhone, open Calendar → Calendars → tap ⓘ next to a calendar → turn on “Public Calendar” → Share Link → Copy. On a Mac, right-click the calendar → Share Calendar → tick “Public Calendar” → copy the link.")),
    sources,
    form);
}

/* ---------- shell ---------- */

function renderHeader() {
  const hr = new Date().getHours();
  const part = hr < 12 ? "morning" : hr < 17 ? "afternoon" : "evening";
  const name = (window.ME.name || "").split(" ")[0];
  $("#greeting").textContent = `Good ${part}${name ? ", " + name : ""}`;
  $("#date-title").textContent = fmtDay(today());
}

function route() {
  const name = location.hash.slice(1);
  const view = views[name] ? name : "today";
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("active", a.hash === "#" + view);
  $("#view").replaceChildren(...views[view]());
  renderHeader();
}

window.addEventListener("hashchange", route);
// Refresh when coming back to the tab on a new day.
let lastDay = today();
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && today() !== lastDay) { lastDay = today(); route(); }
});
route();
