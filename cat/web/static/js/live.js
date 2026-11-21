// Keeps the page current without reloading: asks /api/now every few seconds, updates "Now", adds new diary entries.
// The page works without JavaScript; this only refreshes it. All text goes in as text, never as HTML.
(function () {
  const body = document.body;
  const every = (parseInt(body.dataset.refresh, 10) || 10) * 1000;
  if (new URLSearchParams(location.search).has("before")) return;      // an older page: leave it still
  let latest = body.dataset.latest || "";
  const doingEl = document.getElementById("doing");
  let activeId = doingEl?.dataset.activityId || null;
  let activeLine = doingEl?.dataset.line || "";
  let dots = 1;

  function text(el, value) { if (el) el.textContent = value || ""; }

  function updateCurrentEntry(doing) {
    const list = document.getElementById("diary");
    if (!list) return;
    let li = document.getElementById("current-action");
    if (!doing) {
      if (li) li.remove();
      return;
    }
    if (!li) {
      li = document.createElement("li");
      li.className = "entry current";
      li.id = "current-action";
      const when = document.createElement("p"); when.className = "when"; when.textContent = "In progress";
      const what = document.createElement("p"); what.className = "what";
      const bold = document.createElement("b"); what.appendChild(bold);
      li.append(when, what);
      list.prepend(li);
    }
    li.dataset.activityId = String(doing.id);
    li.dataset.line = doing.line || doing.name;
    const bold = li.querySelector(".what b");
    text(bold, `${li.dataset.line}${".".repeat(dots)}`);
  }

  function entry(e) {
    const li = document.createElement("li");
    li.className = `entry ${e.kind} ${e.status || ""}`;
    li.dataset.id = e.id;
    li.dataset.time = e.time;
    const when = document.createElement("p");
    when.className = "when";
    const t = document.createElement("time");
    if (e.kind === "activity") {
      t.dateTime = e.started;
      t.textContent = `Day ${e.start_day}, ${e.start_clock}`;
      when.appendChild(t);
      const rest = e.status === "rejected" ? [" · tried to ", e.name, ", but could not"]
        : [` → ${e.end_clock} (${e.minutes} min${e.status === "interrupted" ? ", stopped early" : ""}) · `, e.name, ""];
      when.appendChild(document.createTextNode(rest[0]));
      const b = document.createElement("b"); b.textContent = rest[1]; when.appendChild(b);
      when.appendChild(document.createTextNode(rest[2]));
      if (e.decided_real) {
        const s = document.createElement("span"); s.className = "real";
        s.textContent = ` decided at ${e.decided_real} your time`; when.appendChild(s);
      }
    } else {
      t.dateTime = e.time;
      t.textContent = `Day ${e.day}, ${e.clock}`;
      when.appendChild(t);
    }
    li.appendChild(when);
    if (e.kind === "activity" && e.thought) {
      const p = document.createElement("p"); p.className = "thought"; p.textContent = e.thought; li.appendChild(p);
    }
    if (e.kind === "activity" && e.artwork_id) {
      const a = document.createElement("a"); a.className = "journal-art"; a.href = `/artwork/${encodeURIComponent(e.artwork_id)}`;
      const img = document.createElement("img"); img.src = `/artwork/image/${encodeURIComponent(e.artwork_id)}`;
      img.alt = "Drawing made by Nit"; a.appendChild(img); li.appendChild(a);
    }
    const p = document.createElement("p");
    p.className = e.kind === "night" ? "night-entry" : e.kind === "event" ? "event-entry" : "what";
    if (e.kind === "activity" && e.line) {
      const b = document.createElement("b"); b.textContent = e.line + ". "; p.appendChild(b);
      const s = document.createElement("span"); s.className = "hint"; s.textContent = e.text; p.appendChild(s);
    } else {
      p.textContent = e.text;
    }
    li.appendChild(p);
    return li;
  }

  async function tick() {
    try {
      const r = await fetch(`/api/now?after=${encodeURIComponent(latest)}`, { cache: "no-store" });
      if (!r.ok) return;
      const d = await r.json();
      const n = d.now;
      if (n) {
        text(document.getElementById("where"),
             `Day ${n.day}, ${n.part_of_day} (${n.clock}), ${n.weather || ""}. At ${n.place}.`);
        if (n.asleep) {
          text(doingEl, "Asleep.");
          if (n.doing) {
            activeId = String(n.doing.id);
            activeLine = n.doing.line || n.doing.name;
            updateCurrentEntry(n.doing);
          } else {
            activeId = null; activeLine = "";
            updateCurrentEntry(null);
          }
        } else if (n.doing) {
          const id = String(n.doing.id);
          if (activeId !== id) dots = 1;
          activeId = id;
          activeLine = n.doing.line || n.doing.name;
          if (doingEl) {
            doingEl.dataset.activityId = id;
            doingEl.dataset.line = activeLine;
          }
          text(doingEl, `${activeLine}${".".repeat(dots)}`);
          updateCurrentEntry(n.doing);
        } else {
          activeId = null; activeLine = "";
          if (doingEl) delete doingEl.dataset.activityId;
          text(doingEl, "Between things.");
          updateCurrentEntry(null);
        }
        const th = document.getElementById("thought");
        if (th) text(th, n.asleep || !n.thought ? "" : n.thought.text);
        for (const v of ["mood", "energy", "hunger"]) {
          const m = document.getElementById(`m-${v}`); if (m) m.value = n.body[v];
        }
        const ul = document.getElementById("body");
        if (ul) {
          ul.replaceChildren(...[...n.body.words, n.body.derived_words].filter(Boolean).map(w => {
            const li = document.createElement("li"); li.textContent = w; return li; }));
        }
        body.classList.toggle("night", n.part_of_day === "night");
        body.classList.toggle("asleep", n.asleep);
        const img = document.getElementById("portrait");
        if (img) img.src = `/static/img/nit/${n.asleep ? "nit-asleep" : "nit"}.svg`;
      }
      const list = document.getElementById("diary");
      if (list && d.entries && d.entries.length) {
        for (const e of d.entries.slice().reverse()) {
          if (!list.querySelector(`[data-id="${e.id}"]`)) list.prepend(entry(e));
        }
        latest = d.entries[0].time;
      }
    } catch (_) { /* the next tick tries again */ }
  }
  setInterval(() => {
    if (!activeId || !activeLine) return;
    dots = dots % 3 + 1;
    text(doingEl, `${activeLine}${".".repeat(dots)}`);
    const currentLine = document.querySelector("#current-action .what b");
    text(currentLine, `${activeLine}${".".repeat(dots)}`);
  }, 700);
  setInterval(tick, every);
})();
