// Security tab (Wyze-style): Cameras / Events / People / Settings.
// Only exists for signed-in Admins and the Owner (PC and phone): the server leaves this tab (and this script)
// out of the page otherwise, and refuses the /security/... calls for anyone else.
// The camera is off until it is switched on. Same /security/api/... calls as before.
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const S = { active: false, st: null, timer: null, streaming: false, lastNotice: "", msgTimer: null, page: "cams",
              log: { seq: 0, timer: null }, events: [], filter: "all", unread: 0, thumbs: {}, profTimer: null, firstLog: true };

  async function api(path, method, body) {
    const opt = { method: method || "GET", credentials: "same-origin", headers: {} };
    if (body instanceof FormData) opt.body = body;
    else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    let d = {};
    try { d = await r.json(); } catch (e) {}
    if (!r.ok && !d.error && !d.message) d.error = "Request failed (" + r.status + ")";
    return d;
  }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  const svg = d => { const t = document.createElement("template"); t.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + d + "</svg>"; return t.content.firstChild; };
  const PERSON = '<circle cx="12" cy="8" r="3.500"/><path d="M5 20c.5-4 3.500-6 7-6s6.500 2 7 6"/>';

  function say(text, bad) {
    const m = $("csMsg");
    clearTimeout(S.msgTimer);
    m.textContent = text || "";
    m.className = "cs-toast" + (bad ? " bad" : "");
    m.style.display = text ? "block" : "none";
    if (text) S.msgTimer = setTimeout(() => { m.style.display = "none"; }, bad ? 9000 : 5000);
  }

  // ---- pages (Cameras / Events / People / Settings) --------------------------------------------------------------
  function go(page) {
    S.page = page;
    ["cams", "events", "people", "settings"].forEach(p => { $("csPage-" + p).hidden = p !== page; });
    document.querySelectorAll("#csTabs button").forEach(b => b.classList.toggle("on", b.dataset.page === page));
    if (page === "events") { S.unread = 0; renderBadge(); renderEvents(); }
    if (page === "people") loadProfiles();
    const v = $("securityView"); if (v) v.scrollTop = 0;
  }
  document.querySelectorAll("#csTabs button").forEach(b => b.addEventListener("click", () => go(b.dataset.page)));
  document.querySelectorAll("#securityView [data-go]").forEach(b => b.addEventListener("click", () => {
    if (b.dataset.go === "people-add") { go("people"); openSheet(); } else go(b.dataset.go);
  }));
  function renderBadge() {
    const b = $("csBadge");
    b.hidden = !S.unread || S.page === "events";
    b.textContent = S.unread > 99 ? "99+" : String(S.unread);
  }

  // ---- live feed ----------------------------------------------------------------------------------------------------
  function startFeed() {
    if (S.streaming) return;
    S.streaming = true;
    $("csFeed").src = "/security/stream?t=" + Date.now();
    $("csFeedBox").classList.add("live");
  }
  function stopFeed() {
    S.streaming = false;
    $("csFeed").removeAttribute("src");               // closes the stream so the server can stop sending
    $("csFeedBox").classList.remove("live");
  }
  $("csFeed").addEventListener("error", () => { if (S.streaming) stopFeed(); });
  $("csFull").addEventListener("click", () => {
    const b = $("csFeedBox");
    if (document.fullscreenElement) document.exitFullscreen();
    else if (b.requestFullscreen) b.requestFullscreen().catch(() => say("Full screen is not available here.", true));
  });
  $("csSnap").addEventListener("click", () => {
    const img = $("csFeed");
    if (!img.naturalWidth) { say("Nothing to take a picture of yet.", true); return; }
    try {
      const c = document.createElement("canvas");
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      c.getContext("2d").drawImage(img, 0, 0);
      const a = el("a"); a.href = c.toDataURL("image/png");
      a.download = "Rasputin_" + new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19) + ".png";
      document.body.append(a); a.click(); a.remove();
      say("Picture saved to your downloads.");
    } catch (e) { say("Could not save that picture.", true); }
  });

  // ---- status -------------------------------------------------------------------------------------------------------
  function metric(id, text, good) { const n = $(id); n.textContent = text; n.className = good ? "good" : ""; }
  function render(s) {
    if (!s || s.error && !s.state) { if (s && s.error) say(s.error, true); return; }
    S.st = s;
    const st = s.state, on = st === "on", starting = st === "starting";

    const pill = $("csStatus");
    pill.className = "cs-pill " + (on ? "good" : starting ? "warn" : st === "error" ? "bad" : "");
    $("csStatusTxt").textContent = on ? "Camera on" : starting ? "Starting…" : st === "error" ? "Problem" : "Camera off";
    $("csSub").textContent = on ? (s.faces ? (s.faces === 1 ? "1 person in view" : s.faces + " people in view") : "Watching. No one in view.")
      : starting ? "Getting the camera ready…" : st === "error" ? "Something went wrong with the camera." : "Your camera is turned off.";

    document.querySelectorAll(".js-power").forEach(pw => {
      pw.checked = on || starting;
      pw.disabled = starting || !s.installed;
    });
    $("csCamState").textContent = on ? "On · " + s.fps + " fps" : starting ? "Starting…" : "Off";
    $("csCamName").textContent = "Camera " + s.camera;

    const box = $("csFeedBox"), off = $("csFeedOff");
    box.classList.toggle("starting", starting);
    if (on) startFeed();
    else {
      stopFeed();
      const bad = (st === "error" || !s.installed) && !!s.error;
      box.classList.toggle("err-lib", !s.installed);
      $("csFeedTxt").textContent = starting ? "Starting camera…" : bad ? "Camera problem" : "Camera is off";
      $("csFeedHelp").textContent = starting ? "This takes a few seconds." : bad ? s.error : "Turn it on to see the live picture.";
      off.classList.toggle("bad", bad);
    }

    const chip = $("csWhoChip");
    const known = on && s.faces && s.recognised !== false && s.label && !/^unknown/i.test(s.label);
    chip.className = "cs-who" + (on && s.faces ? " show " + (known ? "known" : "stranger") : "");
    chip.textContent = on && s.faces ? (known ? s.label : "Unknown person") : "";

    if (s.notice && s.notice !== S.lastNotice) say(s.notice, true);
    S.lastNotice = s.notice || "";

    $("csCaptureSw").checked = !!s.capture_unknown;
    $("csLogsSw").checked = !!s.logs;

    metric("csFps", on ? String(s.fps) : "-");
    metric("csFaces", on ? String(s.faces) : "-");
    metric("csWho", on && s.faces ? s.label : "-");
    metric("csLive", on ? s.liveness : "-", s.liveness === "HUMAN");
    metric("csCam", "Camera " + s.camera);
    metric("csCount", String(s.profiles));

    const v = $("csVerify");
    v.disabled = !on || !!s.verifying;
    v.classList.toggle("busy", !!s.verifying);
    $("csSave").disabled = !on;
    $("csSaveHelp").textContent = on ? "They look at the camera" : "Turn the camera on first";

    const sel = $("csCamSel");
    if (document.activeElement !== sel) {
      if (![...sel.options].some(o => +o.value === s.camera)) {
        const o = el("option", "", "Camera " + s.camera); o.value = s.camera; sel.append(o);
      }
      sel.value = String(s.camera);
    }
    const u = s.unknowns || 0;
    $("csUnkTxt").textContent = u === 0 ? "No strangers saved" : u + " stranger" + (u === 1 ? "" : "s") + " saved";
    $("csUnkClear").disabled = !u;
  }
  async function tickStatus() {
    clearTimeout(S.timer);
    if (!S.active) return;
    try { render(await api("/security/api/status")); } catch (e) {}
    S.timer = setTimeout(tickStatus, 700);
  }

  // ---- controls -----------------------------------------------------------------------------------------------------
  async function power(on) {
    document.querySelectorAll(".js-power").forEach(p => { p.disabled = true; });
    render(await api("/security/api/power", "POST", { on }));
  }
  document.querySelectorAll(".js-power").forEach(p => p.addEventListener("change", e => power(e.target.checked)));
  $("csBigOn").addEventListener("click", () => power(true));
  $("csCaptureSw").addEventListener("change", async e => render(await api("/security/api/settings", "POST", { capture_unknown: e.target.checked })));
  $("csLogsSw").addEventListener("change", async e => render(await api("/security/api/settings", "POST", { logs: e.target.checked })));
  $("csVerify").addEventListener("click", async () => {
    say("Blink and move your head a little for 2 seconds…");
    const r = await api("/security/api/verify", "POST", {});
    if (r.success === false) say(r.message, true);
  });
  $("csCamSel").addEventListener("change", async e => {
    const r = await api("/security/api/camera", "POST", { index: +e.target.value });
    if (r.error) say(r.error, true); else render(r);
    e.target.blur();
  });
  async function findCameras(btn) {
    const old = btn.textContent;
    btn.disabled = true; if (btn.id === "csScan") btn.textContent = "Looking…";
    const r = await api("/security/api/cameras");
    btn.disabled = false; btn.textContent = old;
    if (r.error) { say(r.error, true); return; }
    const list = r.cameras || [];
    const sel = $("csCamSel");
    sel.textContent = "";
    list.forEach(i => { const o = el("option", "", "Camera " + i); o.value = i; sel.append(o); });
    sel.value = String(r.active);
    const chips = $("csCamChips");
    chips.textContent = "";
    chips.style.display = list.length > 1 ? "flex" : "none";
    list.forEach(i => {
      const c = el("button", i === r.active ? "on" : "", "Camera " + i); c.type = "button";
      c.addEventListener("click", async () => {
        const x = await api("/security/api/camera", "POST", { index: i });
        if (x.error) say(x.error, true); else { render(x); chips.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === c)); }
      });
      chips.append(c);
    });
    say(list.length === 1 ? "Found 1 camera." : "Found " + list.length + " cameras. Tap one to switch.");
  }
  $("csScan").addEventListener("click", e => findCameras(e.currentTarget));
  $("csFind").addEventListener("click", e => findCameras(e.currentTarget));

  // ---- add a person (pop-up) ----------------------------------------------------------------------------------------
  function openSheet() { $("csSheet").hidden = false; $("csName").focus(); }
  function closeSheet() { $("csSheet").hidden = true; }
  $("csAddOpen").addEventListener("click", openSheet);
  $("csSheetX").addEventListener("click", closeSheet);
  $("csSheet").addEventListener("click", e => { if (e.target === $("csSheet")) closeSheet(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("csSheet").hidden) closeSheet(); });
  function enrollName() {
    const n = $("csName").value.trim();
    if (!n) { say("Type a name first.", true); $("csName").focus(); }
    return n;
  }
  $("csSave").addEventListener("click", async () => {
    const name = enrollName(); if (!name) return;
    const r = await api("/security/api/enroll", "POST", { name });
    say(r.message || r.error, !r.success);
    if (r.success) { $("csName").value = ""; closeSheet(); loadProfiles(); }
  });
  $("csImport").addEventListener("click", () => { if (enrollName()) $("csFile").click(); });
  $("csFile").addEventListener("change", async e => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("name", $("csName").value.trim());
    fd.append("image", f);
    const r = await api("/security/api/import", "POST", fd);
    e.target.value = "";
    say(r.message || r.error, !r.success);
    if (r.success) { $("csName").value = ""; closeSheet(); loadProfiles(); }
  });

  // ---- full photo pop-up ----------------------------------------------------------------------------------------------
  function when(created) {
    const d = new Date(String(created).replace(" ", "T"));
    return isNaN(d) ? String(created || "") : d.toLocaleString(undefined, { dateStyle: "long", timeStyle: "medium" });
  }
  async function openPhoto(fid, who) {
    $("csPhotoName").textContent = who || "Photo";
    $("csPhotoWhen").textContent = ""; $("csPhotoNote").textContent = "";
    const img = $("csPhotoImg"); img.removeAttribute("src"); img.alt = who || "Photo";
    $("csPhoto").hidden = false;
    img.src = "/security/api/photo/" + fid + ".jpg";
    const i = await api("/security/api/face/" + fid + "/info");
    if (i.error) { $("csPhotoNote").textContent = i.error; return; }
    $("csPhotoName").textContent = i.kind === "unknown" ? "Stranger" : i.name;
    $("csPhotoWhen").textContent = (i.kind === "unknown" ? "Photo taken " : "Saved ") + when(i.created);
    if (!i.full) $("csPhotoNote").textContent = "This one was saved before full photos were kept, so only the small face picture is available.";
  }
  function closePhoto() { $("csPhoto").hidden = true; $("csPhotoImg").removeAttribute("src"); }
  $("csPhotoX").addEventListener("click", closePhoto);
  $("csPhoto").addEventListener("click", e => { if (e.target === $("csPhoto")) closePhoto(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("csPhoto").hidden) closePhoto(); });
  $("csPhotoImg").addEventListener("error", () => { $("csPhotoNote").textContent = "That picture is no longer saved."; });
  function makeZoomable(node, fid, who) {
    node.classList.add("cs-zoomable");
    node.tabIndex = 0; node.setAttribute("role", "button"); node.setAttribute("aria-label", "View photo of " + who);
    node.addEventListener("click", () => openPhoto(fid, who));
    node.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPhoto(fid, who); } });
  }

  // ---- people -------------------------------------------------------------------------------------------------------
  async function loadProfiles() {
    const box = $("csProfiles");
    let list = [];
    const q = $("csSearch").value.trim();
    try { list = await api("/security/api/profiles?q=" + encodeURIComponent(q)); } catch (e) {}
    box.textContent = "";
    if (!Array.isArray(list)) list = [];
    list.forEach(p => { S.thumbs[p.name.toLowerCase()] = p.thumb; });
    if (!list.length) {
      const e = el("div", "cs-empty"); e.style.gridColumn = "1 / -1";
      e.append(el("b", "", q ? "No one matches that" : "No one added yet"),
               q ? "Try a different name." : "Tap “Add a person” so the camera can say who it sees.");
      box.append(e);
      return;
    }
    list.forEach(p => {
      const card = el("div", "cs-person");
      const img = el("img"); img.alt = ""; img.src = "/security/api/face/" + p.thumb + ".jpg";
      img.addEventListener("error", () => { img.style.visibility = "hidden"; });
      const pic = el("div", "cs-pic"); pic.append(img); makeZoomable(pic, p.thumb, p.name);
      const del = el("button", "cs-x", "×"); del.type = "button"; del.title = "Remove " + p.name; del.setAttribute("aria-label", "Remove " + p.name);
      del.addEventListener("click", async () => {
        if (!confirm('Remove "' + p.name + '"? ' + (p.samples === 1 ? "Their face sample" : "All " + p.samples + " of their face samples") + " will be deleted.")) return;
        const r = await api("/security/api/profiles/delete", "POST", { name: p.name });
        say(r.message || r.error, !r.success);
        loadProfiles();
      });
      card.append(pic, el("b", "", p.name), el("span", "", p.samples + (p.samples === 1 ? " photo" : " photos")), del);
      box.append(card);
    });
  }
  $("csSearch").addEventListener("input", () => { clearTimeout(S.profTimer); S.profTimer = setTimeout(loadProfiles, 200); });
  $("csUnkClear").addEventListener("click", async () => {
    if (!confirm("Remove every saved photo of strangers?")) return;
    const r = await api("/security/api/unknowns/clear", "POST", {});
    say(r.message || r.error, !r.success);
  });

  // ---- events -------------------------------------------------------------------------------------------------------
  function kindOf(name) { return name === "Unknown" || name.indexOf("Unknown_Captured") === 0 ? "stranger" : "known"; }
  function titleOf(e) {
    if (e.name === "Unknown") return "Someone unknown was seen";
    if (e.name.indexOf("Unknown_Captured") === 0) return "Stranger spotted — photo saved";
    return e.name + " was seen";
  }
  function dayLabel(d) {
    const iso = x => x.getFullYear() + "-" + String(x.getMonth() + 1).padStart(2, "0") + "-" + String(x.getDate()).padStart(2, "0");
    const now = new Date(), y = new Date(now.getTime() - 864e5);
    if (d === iso(now)) return "Today";
    if (d === iso(y)) return "Yesterday";
    const t = new Date(d + "T12:00:00");
    return isNaN(t) ? d : t.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  }
  function evRow(e) {
    const k = kindOf(e.name);
    const row = el("div", "cs-ev");
    const av = el("div", "cs-av" + (k === "stranger" ? " stranger" : ""));
    const m = /^Unknown_Captured_(\d+)$/.exec(e.name);
    const th = k === "known" ? S.thumbs[e.name.toLowerCase()] : m ? +m[1] - 1000 : null;     // a stranger's photo id is in the name
    const fallback = () => { av.textContent = ""; if (k === "known") av.textContent = (e.name[0] || "?").toUpperCase(); else av.append(svg(PERSON)); av.classList.remove("cs-zoomable"); };
    if (th) {
      const i = el("img"); i.alt = ""; i.src = "/security/api/face/" + th + ".jpg";
      i.addEventListener("error", fallback); av.append(i);
      makeZoomable(av, th, k === "known" ? e.name : "Stranger");
    } else fallback();
    const t = el("div", "cs-ev-t"); t.append(el("b", "", titleOf(e)), el("span", "", (e.time || "").slice(0, 8)));
    row.append(av, t, el("span", "cs-tag" + (k === "stranger" ? " stranger" : ""), k === "stranger" ? "Stranger" : "Known"));
    return row;
  }
  function emptyEvents(box, text) {
    const e = el("div", "cs-empty"); e.append(el("b", "", "Nothing to show yet"), text); box.append(e);
  }
  function renderEvents() {
    const all = S.events;
    const rb = $("csRecent"); rb.textContent = "";
    const recent = all.slice(-3).reverse();
    if (!recent.length) emptyEvents(rb, "When the camera sees someone, it shows up here.");
    recent.forEach(e => rb.append(evRow(e)));

    const box = $("csEvents"); const keep = box.scrollTop; box.textContent = "";
    $("csEvCount").textContent = all.length + (all.length === 1 ? " event" : " events");
    const list = all.filter(e => S.filter === "all" || kindOf(e.name) === (S.filter === "known" ? "known" : "stranger")).reverse();
    if (!list.length) { emptyEvents(box, S.filter === "all" ? "Turn on the camera and keep “Keep a history” on in Settings." : "No events in this group."); return; }
    let last = "";
    list.slice(0, 300).forEach(e => {
      if (e.date !== last) { last = e.date; box.append(el("div", "cs-day", dayLabel(e.date))); }
      box.append(evRow(e));
    });
    box.scrollTop = keep;
  }
  document.querySelectorAll("#csFilter button").forEach(b => b.addEventListener("click", () => {
    S.filter = b.dataset.f;
    document.querySelectorAll("#csFilter button").forEach(x => x.classList.toggle("on", x === b));
    renderEvents();
  }));
  async function tickLog() {
    clearTimeout(S.log.timer);
    if (!S.active) return;
    try {
      const r = await api("/security/api/logs?since=" + S.log.seq);
      if (r && Array.isArray(r.entries)) {
        if (r.last < S.log.seq) { S.events = []; S.log.seq = 0; renderEvents(); }            // history was cleared
        else {
          if (r.entries.length) {
            S.events.push(...r.entries);
            if (S.events.length > 500) S.events.splice(0, S.events.length - 500);
            if (!S.firstLog && S.page !== "events") { S.unread += r.entries.length; renderBadge(); }
            renderEvents();
          } else if (S.firstLog) renderEvents();
          S.log.seq = r.last;
          S.firstLog = false;
        }
      }
    } catch (e) {}
    S.log.timer = setTimeout(tickLog, 2000);
  }
  $("csExport").addEventListener("click", () => {
    const a = el("a"); a.href = "/security/api/export?format=" + encodeURIComponent($("csFmt").value);
    a.download = ""; document.body.append(a); a.click(); a.remove();
  });
  $("csClear").addEventListener("click", async () => {
    if (!confirm("Delete the whole history? Download it first if you want a copy.")) return;
    await api("/security/api/logs/clear", "POST", {});
    S.events = []; S.log.seq = 0; S.unread = 0; renderBadge(); renderEvents();
    say("History cleared.");
  });

  // ---- tab plumbing (same pattern as discord.js) --------------------------------------------------------------------
  function activate() {
    S.log.seq = 0; S.events = []; S.firstLog = true; S.unread = 0; renderBadge();
    loadProfiles().then(renderEvents);
    tickStatus();
    tickLog();
  }
  const prev = window.switchView;
  window.switchView = function (v) {
    prev.apply(this, arguments);
    const view = $("securityView");
    if (!view) return;
    if (v === "security") {
      $("chatInterfaceView").style.display = "none";
      view.style.display = "block";
      S.active = true;
      go("cams");
      activate();
    } else {
      view.style.display = "none";
      S.active = false;
      clearTimeout(S.timer);
      clearTimeout(S.log.timer);
      stopFeed();                                   // the camera keeps running on the PC, only the picture stops
    }
  };

  window.addEventListener("load", () => {
    TAB_NAMES.security = "SECURITY";
    try {
      const sv = JSON.parse(localStorage.getItem("rasputinTabZoom") || "{}");
      tabZoom.security = (sv.security >= 0.5 && sv.security <= 3) ? sv.security : (tabZoom.chat || 1);
    } catch (e) { tabZoom.security = tabZoom.chat || 1; }
  });
})();
