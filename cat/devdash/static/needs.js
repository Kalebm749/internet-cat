/* Needs & mood charts over sim time, with sleep/doze bands. Data: /api/dev/needs?range=... */
(function () {
  "use strict";
  var host = document.getElementById("needs-charts");
  if (!host || typeof uPlot === "undefined" || !window.ChartLib) return;
  var L = window.ChartLib, plots = [];
  function load() {
    fetch("/api/dev/needs?range=" + encodeURIComponent(host.getAttribute("data-range")), { headers: { "X-Requested-With": "devdash" }, cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        plots.forEach(function (p) { p.destroy(); }); plots = [];
        var note = document.getElementById("needs-note");
        if (!d.x.length) { note.textContent = "no ticks in this range yet"; return; }
        note.textContent = d.ticks + " ticks · sim time " + d.from.replace("T", " ") + " → " + d.to.replace("T", " ") + " · shaded: blue = asleep, yellow = dozing (brain offline), grey = catch-up";
        var ids = Object.keys(d.needs), crit = L.css("--bad", "#c53030");
        var series = [{ label: "sim time" }], data = [d.x];
        ids.forEach(function (id, i) { series.push(L.line(d.labels[id] || id, L.PALETTE[i % L.PALETTE.length])); data.push(d.needs[id]); });
        var ex = { time: true, bands: d.bands };
        plots.push(L.make(document.getElementById("chart-needs"),
          { series: series, scales: { x: { time: true }, y: { range: [0, 100] } } }, data,
          Object.assign({}, ex, { hooks: [L.lineHook(d.critical_below, crit)] })));
        plots.push(L.make(document.getElementById("chart-energy"),
          { series: [{ label: "sim time" }, L.line("energy", L.PALETTE[2])], scales: { x: { time: true }, y: { range: [-50, 50] } } }, [d.x, d.energy], ex));
        plots.push(L.make(document.getElementById("chart-mood"),
          { series: [{ label: "sim time" }, L.line("valence", L.PALETTE[0]), L.line("arousal", L.PALETTE[3])], scales: { x: { time: true }, y: { range: [-1, 1] } } },
          [d.x, d.valence, d.arousal], ex));
      });
  }
  window.addEventListener("resize", function () { clearTimeout(load._t); load._t = setTimeout(load, 250); });
  load();
})();
