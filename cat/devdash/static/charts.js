/* LLM-calls charts (uPlot, vendored). Data comes from /api/dev/llm/series with the page's filters. */
(function () {
  "use strict";
  if (typeof uPlot === "undefined") return;
  var form = document.getElementById("llm-filters");
  var axis = "tick", plots = [];
  function css(name, fallback) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; }

  function series(label, color, dash) {
    return { label: label, stroke: color, width: 1.6, dash: dash, points: { show: false }, spanGaps: true };
  }
  function make(el, opts, data) {
    var w = Math.max(320, el.clientWidth || 420);
    var ink = css("--dim", "#666"), grid = css("--rule", "#ddd");
    var o = Object.assign({ width: w, height: 190, legend: { show: true },
      scales: { x: { time: axis === "time" } },
      axes: [{ stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid } },
             { stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, size: 56 }] }, opts);
    var p = new uPlot(o, data, el);
    plots.push(p);
  }
  function load() {
    var q = new URLSearchParams();
    ["kind", "model"].forEach(function (n) { var v = form && form.elements[n] && form.elements[n].value; if (v) q.set(n, v); });
    q.set("x", axis);
    fetch("/api/dev/llm/series?" + q.toString(), { headers: { "X-Requested-With": "devdash" }, cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        plots.forEach(function (p) { p.destroy(); }); plots = [];
        ["chart-latency", "chart-tokens", "chart-invalid"].forEach(function (id) { document.getElementById(id).textContent = ""; });
        var note = document.getElementById("chart-note");
        if (!d.x.length) { note.textContent = "no LLM calls to chart yet"; return; }
        note.textContent = d.x.length + " points · " + d.n.reduce(function (a, b) { return a + b; }, 0) + " calls · x-axis: " + (axis === "time" ? "real time" : "tick number");
        var red = css("--bad", "#c53030"), blue = css("--accent", "#2b6cb0"), green = css("--ok", "#2f855a"), warn = css("--warn", "#b7791f");
        make(document.getElementById("chart-latency"), { series: [{ label: axis === "time" ? "time" : "tick" }, series("p50 ms", blue), series("p95 ms", red)] }, [d.x, d.p50, d.p95]);
        make(document.getElementById("chart-tokens"), { series: [{ label: axis === "time" ? "time" : "tick" }, series("tokens in", green), series("tokens out", warn)] }, [d.x, d.tokens_in, d.tokens_out]);
        make(document.getElementById("chart-invalid"), { series: [{ label: axis === "time" ? "time" : "tick" }, series("invalid %", red)], scales: { x: { time: axis === "time" }, y: { range: [0, 100] } } }, [d.x, d.invalid]);
      });
  }
  Array.prototype.forEach.call(document.querySelectorAll("button.axis"), function (b) {
    b.addEventListener("click", function () {
      axis = b.getAttribute("data-axis");
      Array.prototype.forEach.call(document.querySelectorAll("button.axis"), function (x) { x.setAttribute("aria-pressed", String(x === b)); });
      load();
    });
  });
  window.addEventListener("resize", function () { clearTimeout(load._t); load._t = setTimeout(load, 250); });
  load();
})();
