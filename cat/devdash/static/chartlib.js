/* Shared uPlot helpers for the needs / sleep charts (uPlot is vendored: /static/vendor/). */
(function () {
  "use strict";
  var PALETTE = ["#2b6cb0", "#c53030", "#2f855a", "#b7791f", "#805ad5", "#dd6b20", "#319795", "#d53f8c", "#718096"];
  function css(name, fallback) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; }

  // Shaded vertical bands (sleep / doze periods) drawn under the lines.
  function bandHook(bands) {
    var fills = { asleep: "rgba(91,111,140,0.22)", doze: "rgba(217,164,65,0.28)", catchup: "rgba(160,160,160,0.25)" };
    return function (u) {
      var ctx = u.ctx, b = u.bbox;
      (bands || []).forEach(function (band) {
        var x0 = Math.max(b.left, u.valToPos(band.from, "x", true)), x1 = Math.min(b.left + b.width, u.valToPos(band.to, "x", true));
        if (x1 <= b.left || x0 >= b.left + b.width || x1 <= x0) return;
        ctx.save(); ctx.fillStyle = fills[band.kind] || fills.catchup; ctx.fillRect(x0, b.top, Math.max(1, x1 - x0), b.height); ctx.restore();
      });
    };
  }
  function lineHook(y, color, scale) {            // a dashed horizontal reference line
    return function (u) {
      var ctx = u.ctx, b = u.bbox, py = u.valToPos(y, scale || "y", true);
      if (py < b.top || py > b.top + b.height) return;
      ctx.save(); ctx.strokeStyle = color; ctx.setLineDash([6, 5]); ctx.lineWidth = 1; ctx.beginPath();
      ctx.moveTo(b.left, py); ctx.lineTo(b.left + b.width, py); ctx.stroke(); ctx.restore();
    };
  }
  function make(el, opts, data, extra) {
    extra = extra || {};
    var ink = css("--dim", "#666"), grid = css("--rule", "#ddd");
    var o = Object.assign({
      width: Math.max(360, el.clientWidth || 600), height: 230, legend: { show: true },
      scales: { x: { time: !!extra.time } },
      axes: [{ stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid } },
             { stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, size: 52 }],
      hooks: { drawClear: [bandHook(extra.bands)].concat(extra.hooks || []) }
    }, opts);
    if (extra.time) o.tzDate = function (ts) { return uPlot.tzDate(new Date(ts * 1e3), "Etc/UTC"); };   // sim times are naive: show them as-is
    el.textContent = "";
    return new uPlot(o, data, el);
  }
  function line(label, color, extra) {
    return Object.assign({ label: label, stroke: color, width: 1.6, points: { show: false }, spanGaps: true }, extra || {});
  }
  window.ChartLib = { PALETTE: PALETTE, css: css, make: make, line: line, bandHook: bandHook, lineHook: lineHook };
})();
