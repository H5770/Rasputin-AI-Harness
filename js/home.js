// Home tab: a per-room control panel for Hue and WiZ lights. Talks only to this app's /home/... routes,
// which in turn talk only to devices on your own network. Loaded after the Navigation script in index.html.
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const H = { data: null, timer: null, active: false, busy: false, hold: 0, drag: false, setupSeen: false };
  const UNASSIGNED = "Unassigned";

  // ---- small helpers -------------------------------------------------------
  async function api(path, body) {
    const opt = body === undefined ? {} :
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
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
  function throttle(fn, ms) {
    let last = 0, timer = 0, args;
    return function () {
      args = arguments;
      const wait = ms - (Date.now() - last);
      if (wait <= 0) { last = Date.now(); fn.apply(null, args); }
      else if (!timer) timer = setTimeout(() => { timer = 0; last = Date.now(); fn.apply(null, args); }, wait);
    };
  }
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function say(text, bad) {
    const m = $("hmMsg");
    m.textContent = text || "";
    m.className = "hm-msg" + (bad ? " bad" : "");
    m.style.display = text ? "block" : "none";
  }

  // Sends a command, then re-reads the real state a moment later (bulbs need a beat to report back).
  async function send(url, body) {
    H.hold = Date.now() + 1500;
    const r = await api(url, body);
    if (r.error) say(r.error, true);
    else if (r.message) say(r.message, true);
    else say("");
    H.hold = Date.now() + 300;
    setTimeout(() => load(true), 350);
    return r;
  }

  // ---- controls ------------------------------------------------------------
  function switchEl(checked, onChange, disabled) {
    const w = el("label", "hm-sw"), i = document.createElement("input");
    i.type = "checkbox"; i.checked = !!checked; i.disabled = !!disabled;
    i.addEventListener("change", () => onChange(i.checked));
    w.append(i, document.createElement("i"));
    return w;
  }
  function rangeEl(min, max, value, onInput, title, disabled) {
    const r = document.createElement("input");
    r.type = "range"; r.min = min; r.max = max; r.value = value; r.className = "hm-range";
    r.title = title || ""; r.disabled = !!disabled;
    const live = throttle(onInput, 250);
    r.addEventListener("input", () => live(+r.value));
    r.addEventListener("change", () => onInput(+r.value));
    return r;
  }
  function colorEl(value, onPick, disabled) {
    const c = document.createElement("input");
    c.type = "color"; c.value = value || "#ffffff"; c.className = "hm-color"; c.title = "Color"; c.disabled = !!disabled;
    const live = throttle(onPick, 300);
    c.addEventListener("input", () => live(c.value));
    c.addEventListener("change", () => onPick(c.value));
    return c;
  }
  function btn(text, title, fn) {
    const b = el("button", "hm-btn hm-small", text);
    b.type = "button"; b.title = title || "";
    b.addEventListener("click", fn);
    return b;
  }

  // ---- rendering -----------------------------------------------------------
  function interacting() {
    const a = document.activeElement;
    return H.drag || (a && a.closest && a.closest("#hmRooms") && (a.type === "range" || a.type === "color" || a.tagName === "SELECT"));
  }

  function roomCard(room, lights, d) {
    const card = el("section", "hm-room");
    const head = el("div", "hm-room-head");
    const anyOn = lights.some(l => l.on);
    const live = lights.filter(l => l.reachable);
    const onCount = lights.filter(l => l.on).length;

    head.append(switchEl(anyOn, on => send("/home/room", { room, on }), !live.length));
    const t = el("div", "hm-room-title");
    t.append(el("span", "hm-room-name", room),
             el("span", "hm-room-count", lights.length ? onCount + " of " + lights.length + " on" : "empty"));
    head.append(t, el("span", "hm-sp"));

    if (live.length) {
      const dimmable = live.filter(l => l.caps.dim), colorful = live.filter(l => l.caps.color), whites = live.filter(l => l.caps.ct);
      const onLights = dimmable.filter(l => l.on);
      const avg = onLights.length ? Math.round(onLights.reduce((s, l) => s + l.bri, 0) / onLights.length) : 100;
      if (dimmable.length) head.append(rangeEl(1, 100, avg, v => send("/home/room", { room, bri: v }), "Room brightness"));
      if (colorful.length) {
        const first = colorful.find(l => l.color);
        head.append(colorEl(first ? first.color : "#ffffff", v => send("/home/room", { room, color: v })));
      }
      if (whites.length) {
        head.append(btn("WARM", "Warm white", () => send("/home/room", { room, kelvin: 2700 })),
                    btn("NEUTRAL", "Neutral white", () => send("/home/room", { room, kelvin: 4000 })),
                    btn("COOL", "Cool white", () => send("/home/room", { room, kelvin: 6000 })));
      }
    }
    if (room !== UNASSIGNED) {
      head.append(btn("✎", "Rename room", async () => {
        const n = prompt("Rename room:", room);
        if (n && n.trim() && n.trim() !== room) { await api("/home/rooms", { action: "rename", name: room, new: n.trim() }); load(true); }
      }), btn("🗑", "Delete room (its lights move to Unassigned)", async () => {
        if (!confirm("Delete the room \"" + room + "\"? Its lights are kept and moved to Unassigned.")) return;
        await api("/home/rooms", { action: "delete", name: room }); load(true);
      }));
    }
    card.append(head);

    if (!lights.length) card.append(el("div", "hm-empty", "No lights here yet. Use the room menu on any light to move it into this room."));
    lights.forEach(l => card.append(lightRow(l, d)));
    return card;
  }

  function lightRow(l, d) {
    const row = el("div", "hm-light" + (l.reachable ? "" : " off-line"));
    row.append(switchEl(l.on, on => send("/home/light", { id: l.id, on }), !l.reachable));

    const dot = el("span", "hm-dot");
    if (l.on && l.color) dot.style.background = l.color, dot.style.boxShadow = "0 0 10px " + l.color;
    row.append(dot);

    const name = el("div", "hm-name");
    const nm = el("span", "hm-name-text", l.name);
    nm.title = "Click to rename";
    nm.addEventListener("click", async () => {
      const n = prompt("Name this light (leave empty to reset):", l.name);
      if (n === null) return;
      await api("/home/rename-light", { id: l.id, name: n.trim() }); load(true);
    });
    name.append(nm, el("span", "hm-tag", l.kind === "hue" ? "HUE" : "WIZ"));
    if (!l.reachable) name.append(el("span", "hm-tag bad", "OFFLINE"));
    row.append(name);

    if (l.caps.dim) row.append(rangeEl(1, 100, l.bri, v => send("/home/light", { id: l.id, bri: v }), "Brightness", !l.reachable));
    else row.append(el("span", "hm-range-ph"));
    if (l.caps.color) row.append(colorEl(l.color, v => send("/home/light", { id: l.id, color: v }), !l.reachable));
    if (l.caps.ct) {
      const k = rangeEl(2200, 6500, l.kelvin || 3000, v => send("/home/light", { id: l.id, kelvin: v }), "White temperature (warm to cool)", !l.reachable);
      k.classList.add("hm-kelvin");
      row.append(k);
    }

    const sel = document.createElement("select");
    sel.className = "hm-in hm-sel"; sel.title = "Room";
    d.rooms.concat(d.rooms.includes(UNASSIGNED) ? [] : [UNASSIGNED]).forEach(r => {
      const o = document.createElement("option");
      o.value = r; o.textContent = r; sel.append(o);
    });
    sel.value = l.room;
    sel.addEventListener("change", async () => { await api("/home/assign", { id: l.id, room: sel.value }); load(true); });
    row.append(sel);

    if (l.kind === "wiz") row.append(btn("✕", "Forget this bulb", async () => {
      if (!confirm("Forget " + l.name + "? You can find it again with SCAN NETWORK.")) return;
      await api("/home/wiz/forget", { id: l.id }); load(true);
    }));
    return row;
  }

  function render(d) {
    const root = $("hmRooms");
    root.textContent = "";
    if (!d.rooms.length && !d.lights.length) {
      root.append(el("div", "hm-empty big", "No lights yet. Press SCAN NETWORK to find your WiZ bulbs and Hue Bridge, or open SETUP."));
    }
    d.rooms.forEach(room => root.append(roomCard(room, d.lights.filter(l => l.room === room), d)));

    const hueTxt = !d.hue.paired ? "NOT CONNECTED" : d.hue.error ? "BRIDGE UNREACHABLE" : "CONNECTED" + (d.hue.name ? " (" + d.hue.name + ")" : "");
    const hueN = d.lights.filter(l => l.kind === "hue").length;
    const on = d.lights.filter(l => l.on).length;
    $("hmStatus").textContent = "● " + d.lights.length + " LIGHTS // " + on + " ON // HUE " + hueTxt + " // WIZ " + d.wiz_count;
    $("hmHueState").textContent = (d.hue.paired ? "Paired with the bridge at " + d.hue.ip + ". " + hueN + " lights found." : "Not connected.") + (d.hue.error ? " " + d.hue.error : "");
    $("hmHueBtn").style.display = d.hue.paired ? "none" : "";
    $("hmHueForget").style.display = d.hue.paired || d.hue.ip ? "" : "none";
    if (d.hue.ip && !$("hmHueIp").value) $("hmHueIp").value = d.hue.ip;
    $("hmWizState").textContent = d.wiz_count ? d.wiz_count + " bulbs saved." : "No bulbs saved yet.";

    if (!H.setupSeen) {                       // first visit with nothing set up: open SETUP once
      H.setupSeen = true;
      if (!d.hue.paired && !d.wiz_count) $("hmSetup").style.display = "grid";
    }
  }

  async function load(force) {
    if (!H.active || H.busy) return;
    if (!force && (Date.now() < H.hold || interacting())) return;
    H.busy = true;
    try {
      const d = await api("/home/state");
      if (d.error) throw new Error(d.error);
      H.data = d;
      if (!interacting()) render(d);
    } catch (e) {
      $("hmStatus").textContent = "● OFFLINE // " + e.message;
    } finally { H.busy = false; }
  }

  // ---- toolbar and setup ---------------------------------------------------
  async function scan() {
    const b = $("hmScan");
    b.disabled = true; b.textContent = "SCANNING...";
    say("Listening on your Wi-Fi for WiZ bulbs and a Hue Bridge (about 3 seconds)...");
    try {
      const r = await api("/home/scan", {});
      if (r.error) { say(r.error, true); return; }
      const parts = [r.wiz_found + " WiZ bulb" + (r.wiz_found === 1 ? "" : "s") + " found" + (r.wiz_new ? " (" + r.wiz_new + " new)" : "")];
      if (r.hue_bridges && r.hue_bridges.length) parts.push("Hue Bridge at " + r.hue_bridges[0]);
      else parts.push("no Hue Bridge answered");
      say(parts.join(" // ") + (r.wiz_found ? "" : ". If your WiZ bulbs are on, enable Allow local communication in the WiZ app."));
      if (r.hue_bridges && r.hue_bridges.length) $("hmHueIp").value = r.hue_bridges[0];
      H.setupSeen = true;
      await load(true);
    } finally { b.disabled = false; b.textContent = "SCAN NETWORK"; }
  }

  async function hueConnect() {
    const b = $("hmHueBtn"), ip = $("hmHueIp").value.trim();
    b.disabled = true;
    try {
      say("Press the round link button on top of your Hue Bridge now. Waiting up to 30 seconds...");
      for (let i = 0; i < 15; i++) {
        const r = await api("/home/hue/connect", { ip });
        if (r.status === "ok") { say("Hue Bridge connected."); await load(true); return; }
        if (r.status !== "link_button") { say(r.error || "Could not pair with the bridge.", true); return; }
        await sleep(2000);
      }
      say("The link button was not pressed in time. Try again.", true);
    } finally { b.disabled = false; }
  }

  async function wizAdd() {
    const ip = $("hmWizIp").value.trim();
    if (!ip) { say("Enter the bulb's IP address, or use SCAN NETWORK to find them all.", true); return; }
    const r = await api("/home/wiz/add", { ip });
    if (r.error) { say(r.error, true); return; }
    say(r.new ? "Bulb added." : "That bulb was already saved. Its address was refreshed.");
    $("hmWizIp").value = "";
    load(true);
  }

  function wire() {
    $("hmScan").addEventListener("click", scan);
    $("hmAllOn").addEventListener("click", () => send("/home/room", { room: "*", on: true }));
    $("hmAllOff").addEventListener("click", () => send("/home/room", { room: "*", on: false }));
    $("hmAddRoom").addEventListener("click", async () => {
      const n = prompt("New room name:");
      if (!n || !n.trim()) return;
      const r = await api("/home/rooms", { action: "add", name: n.trim() });
      if (r.error) say(r.error, true); else load(true);
    });
    $("hmSetupBtn").addEventListener("click", () => {
      const s = $("hmSetup");
      s.style.display = s.style.display === "none" ? "grid" : "none";
    });
    $("hmHueBtn").addEventListener("click", hueConnect);
    $("hmHueForget").addEventListener("click", async () => {
      if (!confirm("Forget the Hue Bridge? Its lights disappear from this panel until you connect again.")) return;
      await api("/home/hue/forget", {}); $("hmHueIp").value = ""; load(true);
    });
    $("hmWizBtn").addEventListener("click", wizAdd);
    $("hmWizIp").addEventListener("keydown", e => { if (e.key === "Enter") wizAdd(); });
    $("hmHueIp").addEventListener("keydown", e => { if (e.key === "Enter") hueConnect(); });
    // while a slider or color wheel is being dragged, the background refresh must not rebuild it
    document.addEventListener("pointerdown", e => { if (e.target.closest && e.target.closest("#hmRooms") && /^(range|color)$/.test(e.target.type || "")) H.drag = true; });
    document.addEventListener("pointerup", () => { if (H.drag) { H.drag = false; H.hold = Date.now() + 600; } });
  }

  // ---- tab plumbing --------------------------------------------------------
  const prev = window.switchView;
  window.switchView = function (v) {
    prev(v);
    const view = $("homeView");
    if (!view) return;
    if (v === "home") {
      $("chatInterfaceView").style.display = "none";
      view.style.display = "block";
      H.active = true;
      load(true);
      if (!H.timer) H.timer = setInterval(() => { if (!document.hidden) load(false); }, 3000);
    } else {
      view.style.display = "none";
      H.active = false;
      clearInterval(H.timer); H.timer = null;
    }
  };

  window.addEventListener("load", () => {
    // let zoom and back/forward treat Home as a normal tab
    TAB_NAMES.home = "HOME";
    try {
      const sv = JSON.parse(localStorage.getItem("rasputinTabZoom") || "{}");
      tabZoom.home = (sv.home >= 0.5 && sv.home <= 3) ? sv.home : (tabZoom.chat || 1);
    } catch (e) { tabZoom.home = tabZoom.chat || 1; }
  });

  wire();
})();
