// Discord tab: switch the Discord bot on/off, save its token (PC only), and choose where it replies.
// Only exists for signed-in Admins and the Owner (PC and phone): the server leaves this tab (and this script)
// out of the page otherwise, and refuses the /discord/api/... calls for anyone else.
// Loaded after admin.js in index.html.
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const D = { active: false, busy: false, poll: null, st: null, dirty: false };

  async function api(path, method, body) {
    const opt = { method: method || "GET", credentials: "same-origin", headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    let d = {};
    try { d = await r.json(); } catch (e) {}
    if (!r.ok && !d.error) d.error = "Request failed (" + r.status + ")";
    return d;
  }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function say(text, bad) {
    const m = $("dcMsg");
    m.textContent = text || "";
    m.className = "dc-msg" + (bad ? " bad" : "");
    m.style.display = text ? "block" : "none";
  }
  function when(s) {
    if (!s) return "never";
    const d = new Date(s.replace(" ", "T") + "Z");
    return isNaN(d) ? s : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  }
  function toggle(id, title, sub, checked, disabled, onChange, warn) {
    const lab = el("label", "dc-switch" + (warn ? " warn" : ""));
    const i = el("input"); i.type = "checkbox"; i.id = id; i.checked = checked; i.disabled = disabled;
    i.addEventListener("change", () => onChange(i.checked));
    lab.append(i, el("span", "dc-slider"), el("span", "dc-switch-t", title), el("span", "dc-switch-s", sub));
    return lab;
  }

  // ---- cards ----------------------------------------------------------------
  function renderBot(s) {
    const box = $("dcBot");
    box.textContent = "";
    const st = s.state || "off";
    const tag = el("span", "dc-tag" + (st === "on" ? " good" : st === "error" ? " bad" : ""),
                   st === "on" ? "ONLINE" : st === "starting" ? "STARTING…" : st === "error" ? "ERROR" : "OFF");
    const head = el("div", "dc-line");
    head.append(el("span", "dc-k", "STATUS"), tag);
    if (st === "on" && s.name) head.append(el("code", "", s.name));
    box.append(head);

    box.append(toggle("dcPower", "DISCORD BOT", st === "on" ? "ON" : st === "starting" ? "STARTING" : "OFF",
      st === "on" || st === "starting", st === "starting" || (!s.has_token && st !== "on"), setPower));

    if (st === "error" && s.error) box.append(el("div", "dc-note bad", s.error));
    else if (!s.has_token) box.append(el("div", "dc-note", "Save a bot token first (on the PC), then the switch unlocks."));
    else if (st !== "on") box.append(el("div", "dc-note", "The bot is off and not connected to Discord. It only connects when you switch it on, and it disconnects when you switch it off or exit Rasputin."));
    else {
      const n = el("div", "dc-note");
      n.append("Connected to " + s.servers + " server" + (s.servers === 1 ? "" : "s") +
               (s.server_names && s.server_names.length ? " (" + s.server_names.join(", ") + ")" : "") + " // " +
               s.answered + " repl" + (s.answered === 1 ? "y" : "ies") + " this session // last reply " + when(s.last));
      box.append(n);
      if (s.invite) {
        const row = el("div", "dc-line");
        const b = el("button", "dc-btn", "COPY INVITE LINK"); b.type = "button";
        b.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(s.invite); b.textContent = "COPIED"; setTimeout(() => b.textContent = "COPY INVITE LINK", 1500); } catch (e) {}
        });
        row.append(b, el("span", "dc-hint", "open it in a browser to add the bot to a server"));
        box.append(row);
      }
    }
  }

  function renderToken(s) {
    const box = $("dcToken");
    box.textContent = "";
    const head = el("div", "dc-line");
    head.append(el("span", "dc-k", "TOKEN"), el("span", "dc-tag" + (s.has_token ? " good" : ""), s.has_token ? "SAVED" : "NONE"));
    box.append(head);
    if (!s.can_edit_token) {
      box.append(el("div", "dc-note", "The token can only be set or removed on the PC itself. It is never shown again after it is saved."));
      return;
    }
    const lab = el("label", "dc-field", "BOT TOKEN");
    const inp = el("input", "dc-in"); inp.id = "dcTok"; inp.type = "password"; inp.autocomplete = "off";
    inp.spellcheck = false; inp.maxLength = 200; inp.placeholder = s.has_token ? "saved: paste a new one to replace it" : "paste the token from the Developer Portal";
    lab.append(inp);
    box.append(lab);
    const row = el("div", "dc-actions");
    const save = el("button", "dc-btn primary", "SAVE TOKEN"); save.type = "button";
    save.addEventListener("click", async () => {
      if (!inp.value.trim()) { say("Paste the token first.", true); return; }
      save.disabled = true;
      const r = await api("/discord/api/token", "POST", { token: inp.value.trim() });
      inp.value = "";
      save.disabled = false;
      apply(r); say(r.error || "Token saved.", !!r.error);
    });
    row.append(save);
    if (s.has_token) {
      const rm = el("button", "dc-btn danger", "REMOVE"); rm.type = "button";
      rm.addEventListener("click", async () => {
        if (!confirm("Remove the saved bot token? The bot will not be able to start until you save a new one.")) return;
        if (s.state === "on" || s.state === "starting") await api("/discord/api/power", "POST", { on: false });
        const r = await api("/discord/api/token", "POST", { token: "" });
        apply(r); say(r.error || "Token removed.", !!r.error);
      });
      row.append(rm);
    }
    box.append(row);
    box.append(el("div", "dc-note", "Developer Portal > your app > Bot > Reset Token. On the same page turn on MESSAGE CONTENT INTENT. The token is stored on this PC and never sent back to any browser."));
  }

  function renderWhere(s) {
    const box = $("dcWhere");
    if (D.dirty) return;                       // do not wipe what is being typed
    box.textContent = "";
    box.append(toggle("dcDms", "DIRECT MESSAGES", s.dms ? "ON" : "OFF", s.dms, false, () => { D.dirty = true; }));
    box.append(toggle("dcAll", "ALL CHANNELS", s.all_channels ? "ON" : "OFF", s.all_channels, false, () => { D.dirty = true; }));
    box.append(toggle("dcRole", "WHEN HIS ROLE IS PINGED", s.roles ? "ON" : "OFF", s.roles, false, () => { D.dirty = true; }));
    box.append(toggle("dcMus", "MUSIC IN VOICE CHANNELS", s.music ? "ON" : "OFF", s.music, false, () => { D.dirty = true; }));
    box.append(toggle("dcMen", "WHEN @MENTIONED", s.mentions ? "ON" : "OFF", s.mentions, false, () => { D.dirty = true; }));
    box.append(toggle("dcImg", "PICTURES", s.images ? "ON" : "OFF", s.images, false, () => { D.dirty = true; }));
    box.append(toggle("dcFil", "CONTENT FILTER", s.filter ? "ON" : "OFF", s.filter, false, () => { D.dirty = true; }));
    const lab = el("label", "dc-field", "ALWAYS REPLY IN THESE CHANNEL IDS");
    const ta = el("textarea", "dc-in"); ta.id = "dcChans"; ta.rows = 3; ta.spellcheck = false;
    ta.placeholder = "one per line (optional)";
    ta.value = (s.channels || []).join("\n");
    ta.addEventListener("input", () => { D.dirty = true; });
    lab.append(ta);
    box.append(lab);
    const save = el("button", "dc-btn primary", "SAVE"); save.type = "button";
    save.addEventListener("click", async () => {
      save.disabled = true;
      const chans = $("dcChans").value.split(/[\s,;]+/).filter(Boolean);
      const r = await api("/discord/api/settings", "POST", { dms: $("dcDms").checked, mentions: $("dcMen").checked, images: $("dcImg").checked, filter: $("dcFil").checked, all_channels: $("dcAll").checked, roles: $("dcRole").checked, music: $("dcMus").checked, channels: chans });
      save.disabled = false;
      if (!r.error) D.dirty = false;
      apply(r); say(r.error || "Saved. It applies to the next message.", !!r.error);
    });
    box.append(save);
    box.append(el("div", "dc-note", "Replies go through the same Rasputin model and personality. Discord users cannot reach your lights. With PICTURES on, anyone who asks (for example: draw a fox in the snow) gets a picture from your image maker, posted where they asked. CONTENT FILTER (off by default) refuses explicit or gory picture requests from Discord. Channel IDs: turn on Developer Mode in Discord, right-click a channel, Copy Channel ID."));
  }

  function apply(s) {
    if (!s || !s.state) return;
    D.st = s;
    renderBot(s); renderToken(s); renderWhere(s);
    $("dcStatus").textContent = "● " + (s.state === "on" ? "ONLINE" : s.state === "starting" ? "STARTING" : s.state === "error" ? "ERROR" : "OFF");
    watch();
  }

  async function setPower(on) {
    const sw = $("dcPower"); if (sw) sw.disabled = true;
    if (on) apply(Object.assign({}, D.st, { state: "starting", error: "" }));
    const r = await api("/discord/api/power", "POST", { on: on });
    if (r.error && !r.state) r.state = "error";
    apply(r);
  }

  function watch() {                           // while connecting, ask again until it is on or failed
    clearTimeout(D.poll);
    if (!D.active || !D.st || D.st.state !== "starting") return;
    D.poll = setTimeout(async () => { const r = await api("/discord/api/status"); if (r.state) apply(r); else watch(); }, 1500);
  }

  async function load() {
    if (!D.active || D.busy) return;
    D.busy = true;
    try {
      const s = await api("/discord/api/status");
      if (s.state) apply(s); else $("dcStatus").textContent = "● " + (s.error || "OFFLINE");
    } catch (e) {
      $("dcStatus").textContent = "● OFFLINE // " + e.message;
    } finally { D.busy = false; }
  }

  // ---- terminal: every request and every reply (kept in the PC's RAM only) -----------------------
  const T = { seq: 0, timer: null, lines: 0 };
  function clock(s) {
    const d = new Date(s.replace(" ", "T") + "Z");
    return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function addLine(e) {
    const out = $("dcTermOut");
    const row = el("div", "dc-tl " + e.kind);
    const label = { request: "REQUEST", reply: "REPLY", image: "PICTURE", warn: "NOTICE", info: "INFO" }[e.kind] || e.kind.toUpperCase();
    const head = el("div", "dc-th");
    head.append(el("span", "dc-tt", clock(e.t)), el("span", "dc-tk", label));
    if (e.who) head.append(el("span", "dc-tw", e.who));
    if (e.where) head.append(el("span", "dc-tp", e.where));
    row.append(head);
    if (e.text) row.append(el("div", "dc-tx", e.text));
    out.append(row);
    if (++T.lines > 500) { out.removeChild(out.firstChild); T.lines--; }
  }
  function emptyHint() {
    const out = $("dcTermOut");
    if (!out.children.length) out.append(el("div", "dc-empty", "Nothing yet. Requests sent to the bot and its replies will appear here."));
  }
  async function tickLog() {
    clearTimeout(T.timer);
    if (!D.active) return;
    try {
      const r = await api("/discord/api/log?since=" + T.seq);
      if (r && Array.isArray(r.entries)) {
        if (r.last < T.seq) { $("dcTermOut").textContent = ""; T.lines = 0; T.seq = 0; }   // the PC app restarted
        const out = $("dcTermOut");
        if (r.entries.length) {
          const hint = out.querySelector(".dc-empty"); if (hint) hint.remove();
          r.entries.forEach(addLine);
          if ($("dcAuto").checked) out.scrollTop = out.scrollHeight;
        }
        T.seq = r.last;
        emptyHint();
      }
    } catch (e) {}
    T.timer = setTimeout(tickLog, 2000);
  }
  $("dcClear").addEventListener("click", async () => {
    await api("/discord/api/log/clear", "POST", {});
    $("dcTermOut").textContent = ""; T.lines = 0; emptyHint();
  });

  $("dcRefresh").addEventListener("click", () => { say(""); D.dirty = false; load(); });

  // ---- tab plumbing (same pattern as admin.js) ------------------------------
  const prev = window.switchView;
  window.switchView = function (v) {
    prev.apply(this, arguments);
    const view = $("discordView");
    if (!view) return;
    if (v === "discord") {
      $("chatInterfaceView").style.display = "none";
      view.style.display = "block";
      D.active = true;
      load();
      T.seq = 0; T.lines = 0; $("dcTermOut").textContent = "";
      tickLog();
    } else {
      view.style.display = "none";
      D.active = false;
      clearTimeout(D.poll);
      clearTimeout(T.timer);
    }
  };

  window.addEventListener("load", () => {
    TAB_NAMES.discord = "DISCORD";
    try {
      const sv = JSON.parse(localStorage.getItem("rasputinTabZoom") || "{}");
      tabZoom.discord = (sv.discord >= 0.5 && sv.discord <= 3) ? sv.discord : (tabZoom.chat || 1);
    } catch (e) { tabZoom.discord = tabZoom.chat || 1; }
  });
})();
