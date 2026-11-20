// Keeps the page current without reloading: asks /api/now every few seconds, updates "Now", adds new diary entries.
// The page works without JavaScript; this only refreshes it. All text goes in as text, never as HTML.
(function () {
  const body = document.body;
  const every = (parseInt(body.dataset.refresh, 10) || 10) * 1000;
  if (new URLSearchParams(location.search).has("before")) return;      // an older page: leave it still
  let latest = body.dataset.latest || "";

  function text(el, value) { if (el) el.textContent = value || ""; }

  function entry(e) {
    const li = document.createElement("li");
    li.className = `entry ${e.kind} ${e.status || ""}`;
    li.dataset.id = e.id;
    li.dataset.time = e.time;
    const t = document.createElement("time");
    t.textContent = `Day ${e.day}, ${e.clock}`;
    li.appendChild(t);
    if (e.kind === "activity" && e.thought) {
      const p = document.createElement("p"); p.className = "thought"; p.textContent = e.thought; li.appendChild(p);
    }
    const p = document.createElement("p");
    p.className = e.kind === "night" ? "night-entry" : e.kind === "event" ? "event-entry" : "what";
    p.textContent = e.text;
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
        text(document.getElementById("doing"),
             n.asleep ? "Asleep." : n.doing ? n.doing.name.charAt(0).toUpperCase() + n.doing.name.slice(1) + "." : "Between things.");
        const th = document.getElementById("thought");
        if (th) text(th, n.asleep || !n.thought ? "" : n.thought.text);
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
  setInterval(tick, every);
})();
