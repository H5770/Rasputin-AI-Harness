// Mindcraft panel: power switch, first-person preview, SERVER / BOT terminal tabs. Loaded after games.js.
(function () {
  "use strict";
  const BASE = "/games/api/mindcraft";
  const $ = id => document.getElementById(id);
  const MAX = 1500;
  let S, root;

  const HTML = `
    <div class="mc-bar">
      <b>MINDCRAFT</b><span class="mc-tag" id="mcTag">OFF</span><span class="mc-info" id="mcInfo"></span>
      <span class="mc-sp"></span>
      <label class="mc-switch" title="Start / stop Minecraft server + Mindcraft bot">
        <input type="checkbox" id="mcPower"><span class="mc-track"><span class="mc-knob"></span></span>
        <span class="mc-sw-t" id="mcSw">OFF</span>
      </label>
    </div>
    <div class="mc-msg" id="mcMsg" style="display:none"></div>
    <div class="mc-grid">
      <section class="mc-view">
        <div class="mc-screen">
          <iframe id="mcFrame" title="Bot view" referrerpolicy="no-referrer"></iframe>
          <div class="mc-idle" id="mcIdle">BOT OFFLINE</div>
        </div>
        <div class="mc-cap">AI VIEW // first-person</div>
      </section>
      <section class="mc-term">
        <div class="mc-tt">
          <button type="button" class="mc-tb on" data-src="server">SERVER</button>
          <button type="button" class="mc-tb" data-src="bot">MINDCRAFT BOT</button>
          <span class="mc-sp"></span>
          <label><input type="checkbox" id="mcAuto" checked> Auto-scroll</label>
          <button type="button" class="mc-btn" id="mcClear">CLEAR</button>
        </div>
        <div class="mc-out" id="mcOut_server"></div>
        <div class="mc-out" id="mcOut_bot" style="display:none"></div>
        <div class="mc-cmd" id="mcCmdRow">
          <input id="mcCmd" autocomplete="off" spellcheck="false" placeholder="server command, e.g. op Andy">
          <button type="button" class="mc-btn" id="mcSend">SEND</button>
        </div>
      </section>
    </div>`;

  function say(t, bad) {
    const m = $("mcMsg");
    m.textContent = t || ""; m.className = "mc-msg" + (bad ? " bad" : ""); m.style.display = t ? "block" : "none";
  }

  function paint(st) {
    const s = st.state, on = s === "starting" || s === "running";
    const tag = $("mcTag");
    tag.textContent = s === "running" ? "ONLINE" : s === "starting" ? (st.stage || "STARTING").toUpperCase() + "…" :
                      s === "stopping" ? "STOPPING…" : s === "error" ? "ERROR" : "OFF";
    tag.className = "mc-tag" + (s === "running" ? " good" : s === "error" ? " bad" : s === "off" ? "" : " busy");
    $("mcInfo").textContent = "Minecraft " + st.mc_version + " // port " + st.mc_port;
    const pw = $("mcPower");
    pw.checked = on; pw.disabled = s === "stopping";
    $("mcSw").textContent = on ? "ON" : "OFF";
    say(s === "error" ? st.error : "", true);
    $("mcCmdRow").style.display = S.src === "server" ? "flex" : "none";

    const fr = $("mcFrame"), idle = $("mcIdle");
    if (st.viewer_ready) {
      // Ports do not make a different "site": 127.0.0.1:3000 inside a 127.0.0.1:5000 page runs on the SAME browser
      // thread as this page, so any work here stalls the viewer (that was the ~2 fps). localhost <-> 127.0.0.1 are
      // different sites, so the browser gives the viewer its own process.
      const h = location.hostname, vh = h === "127.0.0.1" ? "localhost" : h === "localhost" ? "127.0.0.1" : h;
      const url = location.protocol + "//" + vh + ":" + st.viewer_port;
      if (S.frameUrl !== url) { fr.src = url; S.frameUrl = url; }
      idle.style.display = "none";
    } else {
      if (S.frameUrl) { fr.src = "about:blank"; S.frameUrl = ""; }
      idle.style.display = "flex";
      idle.textContent = s === "running" ? "WAITING FOR BOT TO SPAWN…" : s === "starting" ? (st.stage || "STARTING").toUpperCase() + "…" : "BOT OFFLINE";
    }
  }

  const pane = src => $("mcOut_" + src);

  function showPane() {
    ["server", "bot"].forEach(k => { pane(k).style.display = k === S.src ? "block" : "none"; });
    const out = pane(S.src);
    if ($("mcAuto").checked) out.scrollTop = out.scrollHeight;
  }

  function lineClass(t) {
    return /error|exception|Unhandled|\bat .*\(.*:\d+:\d+\)|failed/i.test(t) ? "tl-err" : /warn/i.test(t) ? "tl-warn" : "";
  }

  async function pull(src) {
    const d = await Games.api(BASE + "/log?src=" + src + "&since=" + S.seq[src]);
    if (!d.lines) return;
    S.seq[src] = d.next;
    if (!d.lines.length) return;
    const out = pane(src);
    // stick to the bottom only if the reader is already at the bottom (measured BEFORE appending)
    const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 40;
    const frag = document.createDocumentFragment();
    d.lines.forEach(l => {
      const e = document.createElement("div");
      e.className = "tl " + lineClass(l.t);
      e.textContent = l.t;
      frag.appendChild(e);
    });
    out.appendChild(frag);
    while (out.childElementCount > MAX) out.removeChild(out.firstChild);
    if (stick && $("mcAuto").checked) out.scrollTop = out.scrollHeight;
  }

  async function tick() {
    if (!S || S.dead) return;
    if (document.hidden) { S.timer = setTimeout(tick, 2000); return; }
    try {
      const st = await Games.api(BASE + "/status");
      if (S.dead) return;
      if (!st.error || st.state) paint(st);
      await Promise.all([pull("server"), pull("bot")]);
    } catch (e) {}
    if (!S.dead) S.timer = setTimeout(tick, 1000);
  }

  function mount(el) {
    root = el;
    el.innerHTML = HTML;
    S = { src: "server", seq: { server: 0, bot: 0 }, frameUrl: "", timer: null, dead: false };

    $("mcPower").addEventListener("change", async e => {
      e.target.disabled = true;
      const st = await Games.api(BASE + "/power", "POST", { on: e.target.checked });
      if (st.error && !st.state) say(st.error, true); else paint(st);
    });
    el.querySelectorAll(".mc-tb").forEach(b => b.addEventListener("click", () => {
      S.src = b.dataset.src;
      el.querySelectorAll(".mc-tb").forEach(x => x.classList.toggle("on", x === b));
      $("mcCmdRow").style.display = S.src === "server" ? "flex" : "none";
      showPane();
    }));
    $("mcAuto").addEventListener("change", showPane);
    $("mcClear").addEventListener("click", async () => {
      await Games.api(BASE + "/clear", "POST", { src: S.src });
      pane(S.src).textContent = "";
    });
    const send = async () => {
      const i = $("mcCmd"), t = i.value.trim();
      if (!t) return;
      i.value = "";
      const d = await Games.api(BASE + "/command", "POST", { text: t });
      if (d.error) say(d.error, true);
    };
    $("mcSend").addEventListener("click", send);
    $("mcCmd").addEventListener("keydown", e => { if (e.key === "Enter") send(); });
    tick();
  }

  function unmount() {
    if (S) { S.dead = true; clearTimeout(S.timer); }
    S = null;
  }

  Games.register({ id: "mindcraft", name: "Mindcraft", mount, unmount });
})();
