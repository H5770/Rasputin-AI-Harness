// FlyBridge panel: power switch, brain-view preview, GAME / BRAIN terminal tabs. Loaded after games.js.
(function () {
  "use strict";
  const BASE = "/games/api/flybridge";
  const $ = id => document.getElementById(id);
  const MAX = 1500;
  let S, root;

  const HTML = `
    <div class="mc-bar">
      <b>FLYBRIDGE</b><span class="mc-tag" id="fbTag">OFF</span><span class="mc-info" id="fbInfo"></span>
      <span class="mc-sp"></span>
      <label class="mc-switch" title="Start / stop Minecraft + the fruit-fly brain">
        <input type="checkbox" id="fbPower"><span class="mc-track"><span class="mc-knob"></span></span>
        <span class="mc-sw-t" id="fbSw">OFF</span>
      </label>
    </div>
    <div class="mc-msg" id="fbMsg" style="display:none"></div>
    <div class="mc-grid">
      <section class="mc-view">
        <div class="mc-screen">
          <img id="fbFrame" alt="Brain view" style="display:none;position:absolute;inset:0;width:100%;height:100%;object-fit:contain;image-rendering:pixelated;background:#000">
          <div id="fbFps" style="display:none;position:absolute;top:8px;left:8px;z-index:2;padding:3px 8px;border-radius:6px;background:rgba(0,0,0,.65);color:#7dffa0;font:12px 'Cascadia Code',Consolas,monospace;letter-spacing:1px;pointer-events:none"></div>
          <div class="mc-idle" id="fbIdle">BRAIN OFFLINE</div>
        </div>
        <div class="mc-cap">PLAYER VIEW // first-person, updates each brain decision</div>
      </section>
      <section class="mc-term">
        <div class="mc-tt">
          <button type="button" class="mc-tb on" data-src="server">GAME</button>
          <button type="button" class="mc-tb" data-src="bot">BRAIN</button>
          <span class="mc-sp"></span>
          <label><input type="checkbox" id="fbAuto" checked> Auto-scroll</label>
          <button type="button" class="mc-btn" id="fbClear">CLEAR</button>
        </div>
        <div class="mc-out" id="fbOut_server"></div>
        <div class="mc-out" id="fbOut_bot" style="display:none"></div>
        <div class="mc-cmd" id="fbCmdRow">
          <input id="fbCmd" autocomplete="off" spellcheck="false" placeholder="type restart to run the brain again">
          <button type="button" class="mc-btn" id="fbSend">SEND</button>
        </div>
      </section>
    </div>`;

  function say(t, bad) {
    const m = $("fbMsg");
    m.textContent = t || ""; m.className = "mc-msg" + (bad ? " bad" : ""); m.style.display = t ? "block" : "none";
  }

  function paint(st) {
    const s = st.state, on = s === "starting" || s === "running";
    const tag = $("fbTag");
    tag.textContent = s === "running" ? "ONLINE" : s === "starting" ? (st.stage || "STARTING").toUpperCase() + "…" :
                      s === "stopping" ? "STOPPING…" : s === "error" ? "ERROR" : "OFF";
    tag.className = "mc-tag" + (s === "running" ? " good" : s === "error" ? " bad" : s === "off" ? "" : " busy");
    $("fbInfo").textContent = "Minecraft " + st.mc_version + " // Fabric + FlyBridge mod";
    const pw = $("fbPower");
    pw.checked = on; pw.disabled = s === "stopping";
    $("fbSw").textContent = on ? "ON" : "OFF";
    say(s === "error" ? st.error : "", true);
    $("fbCmdRow").style.display = S.src === "server" ? "flex" : "none";

    const fr = $("fbFrame"), idle = $("fbIdle"), fps = $("fbFps");
    fps.style.display = st.stream_ready ? "block" : "none";
    if (st.stream_ready) fps.textContent = "GAME " + Math.round(st.game_fps || 0) + " FPS  |  STREAM " + Math.round(st.stream_fps || 0) + " FPS";
    if (st.stream_ready) {
      // live 30 fps MJPEG of the game window (flybridge_stream.py). Different host name (localhost <-> 127.0.0.1) = its own browser process.
      const h = location.hostname, vh = h === "127.0.0.1" ? "localhost" : h === "localhost" ? "127.0.0.1" : h;
      const url = location.protocol + "//" + vh + ":" + st.stream_port + "/stream";
      S.live = false;
      if (S.streamUrl !== url) { fr.onload = fr.onerror = null; fr.src = url; S.streamUrl = url; }
      fr.style.imageRendering = "auto"; fr.style.display = "block"; idle.style.display = "none";
      return;
    }
    if (S.streamUrl) { fr.src = ""; S.streamUrl = ""; S.framing = false; }
    fr.style.imageRendering = "pixelated";
    S.live = !!st.viewer_ready;
    if (S.live) { idle.style.display = "none"; fr.style.display = "block"; if (!S.framing) frames(); }
    else {
      fr.style.display = "none"; idle.style.display = "flex";
      idle.textContent = s === "running" ? "WAITING FOR THE BRAIN'S FIRST FRAME…" : s === "starting" ? (st.stage || "STARTING").toUpperCase() + "…" : "BRAIN OFFLINE";
    }
  }

  // The brain writes a PNG per decision; show the newest one (a few per second is plenty).
  function frames() {
    if (!S || S.dead || !S.live) { if (S) S.framing = false; return; }
    S.framing = true;
    const fr = $("fbFrame");
    const done = () => { if (S && !S.dead) setTimeout(frames, document.hidden ? 2000 : 400); };
    fr.onload = done; fr.onerror = done;
    fr.src = BASE + "/frame?t=" + Date.now();
  }

  const pane = src => $("fbOut_" + src);

  function showPane() {
    ["server", "bot"].forEach(k => { pane(k).style.display = k === S.src ? "block" : "none"; });
    const out = pane(S.src);
    if ($("fbAuto").checked) out.scrollTop = out.scrollHeight;
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
    if (stick && $("fbAuto").checked) out.scrollTop = out.scrollHeight;
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
    S = { src: "server", seq: { server: 0, bot: 0 }, live: false, framing: false, streamUrl: "", timer: null, dead: false };

    $("fbPower").addEventListener("change", async e => {
      e.target.disabled = true;
      const st = await Games.api(BASE + "/power", "POST", { on: e.target.checked });
      if (st.error && !st.state) say(st.error, true); else paint(st);
    });
    el.querySelectorAll(".mc-tb").forEach(b => b.addEventListener("click", () => {
      S.src = b.dataset.src;
      el.querySelectorAll(".mc-tb").forEach(x => x.classList.toggle("on", x === b));
      $("fbCmdRow").style.display = S.src === "server" ? "flex" : "none";
      showPane();
    }));
    $("fbAuto").addEventListener("change", showPane);
    $("fbClear").addEventListener("click", async () => {
      await Games.api(BASE + "/clear", "POST", { src: S.src });
      pane(S.src).textContent = "";
    });
    const send = async () => {
      const i = $("fbCmd"), t = i.value.trim();
      if (!t) return;
      i.value = "";
      const d = await Games.api(BASE + "/command", "POST", { text: t });
      if (d.error) say(d.error, true);
    };
    $("fbSend").addEventListener("click", send);
    $("fbCmd").addEventListener("keydown", e => { if (e.key === "Enter") send(); });
    tick();
  }

  function unmount() {
    if (S) { S.dead = true; clearTimeout(S.timer); }
    S = null;
  }

  Games.register({ id: "flybridge", name: "FlyBridge", mount, unmount });
})();
