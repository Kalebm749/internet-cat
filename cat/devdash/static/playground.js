/* Prompt playground: run a (possibly edited) prompt on 1-3 models and show results next to the original. */
(function () {
  "use strict";
  var $ = function (s) { return document.querySelector(s); };
  var form = $("#pg-form"); if (!form) return;
  var src = JSON.parse(($("#pg-src") || { textContent: "null" }).textContent);

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function ms(v) { return v == null ? "–" : (v >= 1000 ? (v / 1000).toFixed(2) + " s" : Math.round(v) + " ms"); }
  function sameKeys(a, b) {
    if (!a || !b || typeof a !== "object" || typeof b !== "object") return null;
    var keys = {}; Object.keys(a).concat(Object.keys(b)).forEach(function (k) { keys[k] = 1; });
    return Object.keys(keys).filter(function (k) { return JSON.stringify(a[k]) !== JSON.stringify(b[k]); }).sort();
  }
  function card(r) {
    var c = el("div", "rcard");
    c.appendChild(el("h3", null, r.model + "  "));
    c.firstChild.appendChild(el("span", "dim", "run #" + r.id));
    var st = el("div"); st.appendChild(el("span", "pill " + (r.status === "ok" ? "ok" : "bad"), r.status));
    if (r.error) st.appendChild(el("span", "bad small", "  " + r.error));
    c.appendChild(st);
    c.appendChild(el("p", "dim small", "in/out " + (r.prompt_tokens == null ? "–" : r.prompt_tokens) + " / " + (r.completion_tokens == null ? "–" : r.completion_tokens) +
      " · load " + ms(r.load_ms) + " · prompt eval " + ms(r.prompt_eval_ms) + " · generation " + ms(r.eval_ms) + " · total " + ms(r.total_ms)));
    if (src && src.parsed && r.parsed_obj) {
      var d = sameKeys(src.parsed, r.parsed_obj);
      c.appendChild(el("p", "small", d && d.length ? "differs from the original in: " + d.join(", ") : "same JSON as the original"));
    }
    c.appendChild(el("pre", null, r.raw_response == null ? "(no response)" : r.raw_response));
    return c;
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var models = Array.prototype.map.call(form.querySelectorAll("input[name=model]:checked"), function (i) { return i.value; });
    var extra = $("#pg-extra-model").value.trim(); if (extra && models.indexOf(extra) < 0) models.push(extra);
    var status = $("#pg-status"), btn = $("#pg-run");
    if (!models.length) { status.textContent = "choose a model"; return; }
    if (models.length > 3) { status.textContent = "compare at most 3 models"; return; }
    var think = $("#pg-think").value;
    var body = { models: models, system_prompt: $("#pg-system").value, user_prompt: $("#pg-user").value, schema: $("#pg-schema").value,
      options: { temperature: parseFloat($("#pg-temp").value), num_ctx: parseInt($("#pg-ctx").value, 10) },
      think: think === "" ? null : think === "true", source_call_id: src ? src.id : null };
    if (isNaN(body.options.temperature)) delete body.options.temperature;
    if (isNaN(body.options.num_ctx)) delete body.options.num_ctx;
    btn.disabled = true; status.textContent = "running on " + models.join(", ") + " … (this can take a while)";
    var t0 = Date.now();
    fetch("/api/dev/playground/run", { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "devdash" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status)); return j; }); })
      .then(function (j) {
        var box = $("#pg-results");
        j.runs.forEach(function (r) { box.appendChild(card(r)); });
        status.textContent = "done in " + ((Date.now() - t0) / 1000).toFixed(1) + " s · ";
        var a = el("a", null, "open as a saved comparison"); a.href = "/playground/groups/" + j.group_id; status.appendChild(a);
      })
      .catch(function (e) { status.textContent = "failed: " + e.message; })
      .then(function () { btn.disabled = false; });
  });
})();
