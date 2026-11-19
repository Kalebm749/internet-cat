/* Tendency history chart: value after each night. Data: /api/dev/sleep (history). */
(function () {
  "use strict";
  var el = document.getElementById("chart-tendencies");
  if (!el || typeof uPlot === "undefined" || !window.ChartLib) return;
  var L = window.ChartLib;
  fetch("/api/dev/sleep?limit=1", { headers: { "X-Requested-With": "devdash" }, cache: "no-store" })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var h = d.history, ids = Object.keys(h.tendencies);
      var note = document.getElementById("tendency-note");
      if (!h.x.length) { note.textContent = "no nights yet"; return; }
      note.textContent = h.x.length + " nights · x-axis: night number (1 = first)";
      var series = [{ label: "night" }], data = [h.x];
      ids.forEach(function (id, i) { series.push(L.line(id, L.PALETTE[i % L.PALETTE.length], { points: { show: h.x.length < 40 } })); data.push(h.tendencies[id]); });
      L.make(el, { series: series, scales: { x: { time: false }, y: { range: [0, 1] } } }, data, { time: false });
    });
})();
