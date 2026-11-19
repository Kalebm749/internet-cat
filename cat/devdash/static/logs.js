/* Live tail for the Logs page: poll /api/dev/logs for lines newer than the last one shown. */
(function () {
  "use strict";
  var box = document.getElementById("live-tail"), table = document.getElementById("log-table");
  if (!box || !table) return;
  var body = table.tBodies[0], state = document.getElementById("tail-state"), form = document.getElementById("log-filters");
  var newest = table.getAttribute("data-newest") || "", timer = null, seen = {};
  Array.prototype.forEach.call(body.rows, function (r) { if (r.dataset.id) seen[r.dataset.id] = 1; });

  function cell(text, cls) { var td = document.createElement("td"); if (cls) td.className = cls; td.textContent = text == null ? "" : text; return td; }
  function row(r) {
    var tr = document.createElement("tr"); tr.className = "lv-" + String(r.level).toLowerCase(); tr.dataset.id = r.id;
    tr.appendChild(cell((r.ts || "").slice(5, 23), "nowrap dim mono")); tr.appendChild(cell(r.service)); tr.appendChild(cell(r.level, "lvl"));
    var t = document.createElement("td");
    if (r.tick_id) { var a = document.createElement("a"); a.href = "/ticks/" + r.tick_id; a.textContent = "#" + r.tick_id; t.appendChild(a); }
    tr.appendChild(t); tr.appendChild(cell(r.event, "dim"));
    var m = cell(r.message);
    if (r.extra && Object.keys(r.extra).length) {
      var d = document.createElement("details"); d.className = "inline"; var s = document.createElement("summary"); s.textContent = "extra";
      var p = document.createElement("pre"); p.textContent = JSON.stringify(r.extra, null, 2); d.appendChild(s); d.appendChild(p); m.appendChild(document.createTextNode(" ")); m.appendChild(d);
    }
    tr.appendChild(m); return tr;
  }
  function poll() {
    var q = new URLSearchParams();
    new FormData(form).forEach(function (v, k) { if (String(v).trim() !== "") q.set(k, v); });   // drop empty fields
    q.set("limit", "200");
    if (newest) q.set("since", newest);
    fetch("/api/dev/logs?" + q.toString(), { headers: { "X-Requested-With": "devdash" }, cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var fresh = d.lines.filter(function (l) { return !seen[l.id]; }).reverse();      // oldest of the new ones first
        fresh.forEach(function (l) { seen[l.id] = 1; body.insertBefore(row(l), body.firstChild); });
        if (d.newest && d.newest > newest) newest = d.newest;
        state.textContent = fresh.length ? "+" + fresh.length + " new" : "listening…";
        while (body.rows.length > 1500) body.deleteRow(body.rows.length - 1);
      })
      .catch(function () { state.textContent = "(can't reach devdash)"; });
  }
  box.addEventListener("change", function () {
    clearInterval(timer);
    if (box.checked) { state.textContent = "listening…"; poll(); timer = setInterval(poll, 2000); } else state.textContent = "";
  });
})();
