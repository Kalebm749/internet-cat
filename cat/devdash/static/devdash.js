/* devdash: small helpers. Pages are server-rendered; this adds auto-refresh, copy buttons and the
   dev-control buttons (which only queue commands: the engine runs them). */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  function toast(msg, ms) {
    var t = $("#toast"); if (!t) return;
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.hidden = true; }, ms || 4000);
  }
  window.devdashToast = toast;

  // ---------- copy buttons ----------
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-copy]");
    if (!b) return;
    var el = $(b.getAttribute("data-copy"));
    if (!el) return;
    var text = el.textContent;
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).catch(function () {
      var ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch (e) { /* ignore */ } ta.remove();
    }).then(function () { b.textContent = "copied"; setTimeout(function () { b.textContent = "copy"; }, 1200); });
  });

  // ---------- dev-control buttons ----------
  function api(method, url, body) {
    return fetch(url, { method: method, headers: { "Content-Type": "application/json", "X-Requested-With": "devdash" },
                        body: body ? JSON.stringify(body) : undefined })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.detail || j.error || ("HTTP " + r.status)); return j; }); });
  }
  window.devdashApi = api;

  function sendCommand(command, args, question) {
    if (!window.confirm(question)) return;
    window.__busy = true;
    api("POST", "/api/dev/commands", { command: command, args: args || {}, confirm: true })
      .then(function (j) { toast("queued #" + j.id + ": " + command + " (the engine runs it within a few seconds)"); setTimeout(refresh, 2500); })
      .catch(function (e) { toast("could not queue " + command + ": " + e.message, 7000); })
      .then(function () { window.__busy = false; });
  }

  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-command]");
    if (!b || b.disabled) return;
    var cmd = b.getAttribute("data-command"), ask = b.getAttribute("data-ask");
    if (ask === "interval") {
      var s = window.prompt("Tick interval in seconds (empty = reset to settings.yaml):", "5");
      if (s === null) return;
      var args = { seconds: s.trim() === "" ? null : Number(s) };
      if (args.seconds !== null) {
        var n = window.prompt("For how many ticks? (empty = until reset or restart)", "");
        if (n === null) return;
        args.ticks = n.trim() === "" ? null : Number(n);
      }
      sendCommand(cmd, args, "Run set_interval " + JSON.stringify(args) + "?");
    } else if (ask === "event") {
      var text = window.prompt("What happens? (one-shot world event on the next tick)", "A strange noise comes from outside.");
      if (!text) return;
      var spawn = window.prompt("Optional: an object that appears (name), or leave empty:", "");
      var a = { text: text };
      if (spawn) a.spawn = { name: spawn, tags: ["movable"] };
      sendCommand(cmd, a, "Inject this event on the next tick?\n\n" + text + (spawn ? "\n+ spawn: " + spawn : ""));
    } else {
      sendCommand(cmd, {}, b.getAttribute("data-confirm") || ("Run " + cmd + "?"));
    }
  });

  // ---------- auto-refresh: swap #content with a fresh copy of this page ----------
  var main = $("#content");
  var every = main ? parseInt(main.getAttribute("data-refresh"), 10) || 0 : 0;
  var box = $("#auto-refresh"), state = $("#refresh-state");
  var KEY = "devdash.autorefresh";
  try { if (box && localStorage.getItem(KEY) === "0") box.checked = false; } catch (e) { /* ignore */ }

  function typing() {
    var a = document.activeElement;
    return window.__busy || (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && main.contains(a));
  }
  function refresh() {
    if (!main || !every || (box && !box.checked) || document.hidden || typing()) return Promise.resolve();
    return fetch(location.href, { headers: { "X-Requested-With": "devdash" }, cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var fresh = doc.getElementById("content");
        if (fresh && !typing()) { main.innerHTML = fresh.innerHTML; if (state) state.textContent = "updated " + new Date().toLocaleTimeString(); }
      })
      .catch(function () { if (state) state.textContent = "(can't reach devdash)"; });
  }
  window.devdashRefresh = refresh;
  if (box) box.addEventListener("change", function () {
    try { localStorage.setItem(KEY, box.checked ? "1" : "0"); } catch (e) { /* ignore */ }
    if (state) state.textContent = box.checked ? "" : "(paused)";
  });
  if (main && every) setInterval(refresh, every * 1000);
})();
