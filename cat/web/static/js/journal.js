/* Live updates for the journal. The page works without this file (it is fully server-rendered);
   this only polls /api/state and adds new things. Markup comes from the <template> elements in
   index.html; all words/labels come from the server (views.py). */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var body = document.body;
  var pollMs = (parseInt(body.dataset.poll, 10) || 10) * 1000;
  var SCROLLED_PX = 200;
  var snapshots = {};
  var pending = [];            // new diary entries held back while the reader is scrolled down
  var busy = false;

  // ---------- tiny template filler ----------
  function get(obj, path) {
    if (path === "$") return obj;
    return path.split(".").reduce(function (o, k) { return o == null ? o : o[k]; }, obj);
  }
  function empty(v) { return v == null || v === "" || v === false || (Array.isArray(v) && !v.length); }

  // Fill `root` (an element or fragment) from `item`. `strip` removes the data-f* hooks once used
  // so that an outer pass doesn't re-evaluate them against the wrong object.
  function fill(root, item, strip) {
    $$("template[data-each]", root).forEach(function (t) {
      (get(item, t.dataset.each) || []).forEach(function (x) {
        var frag = t.content.cloneNode(true);
        fill(frag, x, true);
        t.parentNode.insertBefore(frag, t);
      });
      t.remove();
    });
    var els = $$("[data-f],[data-f-attr],[data-f-class],[data-f-style],[data-show],[data-show-any]", root);
    if (root.nodeType === 1) els.unshift(root);
    els.forEach(function (el) {
      var d = el.dataset;
      if (d.f !== undefined) { var v = get(item, d.f); el.textContent = v == null ? "" : v; }
      if (d.fAttr) d.fAttr.split(";").forEach(function (pair) {
        var i = pair.indexOf(":"); var v = get(item, pair.slice(i + 1));
        if (v != null) el.setAttribute(pair.slice(0, i), v);
      });
      if (d.fClass) { var c = get(item, d.fClass); if (c) el.classList.add(String(c)); }
      if (d.fStyle) { var j = d.fStyle.indexOf(":"); var path = d.fStyle.slice(j + 1), unit = "";
        if (path.slice(-1) === "%") { path = path.slice(0, -1); unit = "%"; }
        el.style[d.fStyle.slice(0, j)] = get(item, path) + unit; }
      if (d.show !== undefined) el.hidden = empty(get(item, d.show));
      if (d.showAny !== undefined) el.hidden = d.showAny.split(",").every(function (p) { return empty(get(item, p)); });
      if (strip) ["f", "fAttr", "fClass", "fStyle", "show", "showAny"].forEach(function (k) { delete el.dataset[k]; });
    });
    return root;
  }
  function clone(id, item) { return fill(document.getElementById(id).content.cloneNode(true), item, true); }
  function setList(ul, tplId, items) {
    ul.textContent = "";
    items.forEach(function (it) { ul.appendChild(clone(tplId, it)); });
  }
  function changed(key, value) {
    var s = JSON.stringify(value);
    if (snapshots[key] === s) return false;
    snapshots[key] = s; return true;
  }

  // ---------- panels ----------
  function applyPanels(s) {
    // scalar text/attributes that already exist in the server-rendered page
    $$("[data-f],[data-f-attr]").forEach(function (el) {
      if (el.closest("template")) return;
      var d = el.dataset;
      if (d.f !== undefined) { var v = get(s, d.f); if (v != null && el.textContent !== String(v)) el.textContent = v; }
      if (d.fAttr) d.fAttr.split(";").forEach(function (pair) {
        var i = pair.indexOf(":"); var v = get(s, pair.slice(i + 1));
        if (v != null) el.setAttribute(pair.slice(0, i), v);
      });
    });
    // time of day + status classes on <body> (colours fade via CSS)
    var keep = body.className.split(/\s+/).filter(function (c) { return !/^(dawn|day|dusk|night|state-.*)$/.test(c); });
    keep.push(s.time.phase, "state-" + s.status.state);
    body.className = keep.join(" ");

    // portrait
    var img = $("#portrait-img");
    var want = s.status.state !== "awake" && s.portrait.asleep_src ? s.portrait.asleep_src : s.portrait.src;
    if (img.getAttribute("src") !== want) img.setAttribute("src", want);

    // mood pad + energy
    var dot = $("#pad-dot"); dot.style.left = s.mood.pad_x + "%"; dot.style.top = s.mood.pad_y + "%";
    var eb = $("#energy-bar");
    eb.setAttribute("aria-valuenow", s.energy.value); eb.setAttribute("aria-label", s.energy.aria);
    eb.firstElementChild.style.width = s.energy.value + "%";

    if (changed("needs", s.needs)) setList($("#needs"), "tpl-need", s.needs);
    if (changed("tendencies", s.tendencies)) setList($("#tendencies"), "tpl-tendency", s.tendencies);
    if (changed("prefs", s.preferences)) {
      setList($("#loves"), "tpl-love", s.preferences.loves);
      setList($("#dislikes"), "tpl-dislike", s.preferences.dislikes);
      $("#loves-empty").hidden = s.preferences.loves.length > 0;
      $("#dislikes-empty").hidden = s.preferences.dislikes.length > 0;
    }
    if (changed("keepsakes", s.keepsakes)) {
      setList($("#keepsakes"), "tpl-keepsake", s.keepsakes);
      $("#keepsakes-empty").hidden = s.keepsakes.length > 0;
      $("#more-wrap").hidden = !s.keepsakes_has_more;
    }
  }

  // ---------- diary ----------
  var TPL = { tick: "tpl-entry-tick", doze: "tpl-entry-doze", sleep: "tpl-entry-sleep" };
  function entryEl(e) {
    var el = clone(TPL[e.type], e).firstElementChild;
    el.dataset.dayLabel = e.day_label;
    return el;
  }
  function entries() { return $$("#diary > .entry"); }

  // Keep exactly one day header above each run of same-day entries.
  function normalizeHeaders() {
    var ol = $("#diary");
    $$("#diary > .day-header").forEach(function (h) { h.remove(); });
    var day = null;
    entries().forEach(function (el) {
      if (el.dataset.day !== day) {
        day = el.dataset.day;
        var h = clone("tpl-day-header", { day_number: day, day_label: el.dataset.dayLabel }).firstElementChild;
        ol.insertBefore(h, el);
      }
    });
    $("#diary-empty").hidden = entries().length > 0;
  }

  function insertEntry(e, animate) {
    var el = entryEl(e);
    var after = entries().filter(function (x) { return parseFloat(x.dataset.id) < e.id; })[0] || null;
    $("#diary").insertBefore(el, after);       // before the first older entry, or at the end
    if (animate) el.classList.add("is-new");
    return el;
  }

  function reconcileDiary(s) {
    var fresh = [];
    s.diary.forEach(function (e) {
      var el = $('#diary > .entry[data-key="' + e.key + '"]');
      if (el) {
        if (parseFloat(el.dataset.id) !== e.id) {          // e.g. a doze stretch that grew
          el.replaceWith(entryEl(e)); normalizeHeaders();
        }
      } else if (!pending.some(function (p) { return p.key === e.key; })) {
        fresh.push(e);
      }
    });
    if (!fresh.length) return;
    fresh.sort(function (a, b) { return a.id - b.id; });
    var haveEntries = entries().length > 0;
    if (haveEntries && window.scrollY > SCROLLED_PX) {     // don't move the page under the reader
      pending = pending.concat(fresh); showPill();
    } else {
      fresh.forEach(function (e) { insertEntry(e, haveEntries); });
      normalizeHeaders();
    }
  }
  function showPill() {
    var n = pending.length, pill = $("#new-pill");
    pill.textContent = n + " new " + (n === 1 ? "entry" : "entries") + " ↑";
    pill.hidden = false;
  }
  function flushPending() {
    pending.sort(function (a, b) { return a.id - b.id; }).forEach(function (e) { insertEntry(e, true); });
    pending = []; $("#new-pill").hidden = true; normalizeHeaders();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function loadOlder(ev) {
    ev.preventDefault();
    var ol = $("#diary"), before = ol.dataset.nextBefore, link = ev.currentTarget;
    if (!before || busy) return;
    busy = true;
    fetch("/api/diary?before=" + encodeURIComponent(before) + "&limit=20", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) {
        d.entries.forEach(function (e) { if (!$('#diary > .entry[data-key="' + e.key + '"]')) insertEntry(e, false); });
        normalizeHeaders();
        ol.dataset.nextBefore = d.next_before || "";
        $("#older-wrap").hidden = !d.next_before;
        if (d.next_before) link.setAttribute("href", "/?before=" + d.next_before);
      })
      .catch(function () { window.location.href = link.getAttribute("href"); })   // fall back to the no-JS page
      .then(function () { busy = false; });
  }

  function loadMoreKeepsakes(ev) {
    ev.preventDefault();
    var ul = $("#keepsakes"), link = ev.currentTarget;
    if (busy) return;
    busy = true;
    fetch("/api/keepsakes?offset=" + ul.children.length + "&limit=20", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) {
        d.keepsakes.forEach(function (k) { ul.appendChild(clone("tpl-keepsake", k)); });
        $("#more-wrap").hidden = !d.has_more;
        snapshots.keepsakes = null;                       // the first-page snapshot no longer matches the DOM
      })
      .catch(function () { window.location.href = link.getAttribute("href"); })
      .then(function () { busy = false; });
  }

  // ---------- polling ----------
  function poll() {
    if (document.hidden) { schedule(); return; }
    fetch("/api/state", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (s) {
        $("#banner").hidden = true;
        applyPanels(s);
        reconcileDiary(s);
        body.dataset.latest = s.latest_tick_id;
        if (s.poll_seconds) pollMs = s.poll_seconds * 1000;
      })
      .catch(function () { $("#banner").hidden = false; })   // keep showing the last data
      .then(schedule);
  }
  var timer = null;
  function schedule() { window.clearTimeout(timer); timer = window.setTimeout(poll, pollMs); }

  document.addEventListener("DOMContentLoaded", function () {
    var older = $("#older"); if (older) older.addEventListener("click", loadOlder);
    var more = $("#more-keepsakes"); if (more) more.addEventListener("click", loadMoreKeepsakes);
    $("#new-pill").addEventListener("click", flushPending);
    window.addEventListener("scroll", function () {
      if (pending.length && window.scrollY <= 20) flushPending();   // back at the top: show them
    }, { passive: true });
    document.addEventListener("visibilitychange", function () { if (!document.hidden) { window.clearTimeout(timer); poll(); } });
    schedule();
  });
})();
