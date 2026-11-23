// Overrides the chat functions from index.html (loaded after them). Same endpoints, same globals.
(function () {
  const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // ---- tiny syntax highlighter (no external libs; works offline) ----------
  const KW = new Set(("def class return if elif else for while in not and or is import from as with try except finally raise lambda " +
    "None True False pass break continue yield async await function const let var new this typeof null undefined true false " +
    "export default switch case throw int float double char void bool string public private static struct include using namespace " +
    "fn mut impl match use pub package func interface extends").split(" "));
  const HASH = new Set(["python", "py", "bash", "sh", "shell", "yaml", "yml", "ruby", "rb", "toml", ""]);
  function highlight(code, lang) {
    const cm = HASH.has(lang) ? "\\/\\/[^\\n]*|#[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/" : "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/";
    const re = new RegExp(`(${cm})|("""[\\s\\S]*?"""|"(?:\\\\.|[^"\\\\\\n])*"|'(?:\\\\.|[^'\\\\\\n])*'|\`(?:\\\\.|[^\`\\\\])*\`)|(\\b\\d+(?:\\.\\d+)?\\b)|([A-Za-z_]\\w*)(?=\\s*\\()|([A-Za-z_]\\w*)`, "g");
    let out = "", last = 0, m;
    while ((m = re.exec(code))) {
      out += esc(code.slice(last, m.index));
      const [t, c, s, n, f, w] = m;
      if (c) out += `<span class="tk-c">${esc(t)}</span>`;
      else if (s) out += `<span class="tk-s">${esc(t)}</span>`;
      else if (n) out += `<span class="tk-n">${esc(t)}</span>`;
      else if (f) out += KW.has(t) ? `<span class="tk-k">${t}</span>` : `<span class="tk-f">${t}</span>`;
      else out += KW.has(w) ? `<span class="tk-k">${t}</span>` : esc(t);
      last = m.index + t.length;
    }
    return out + esc(code.slice(last));
  }

  // ---- message rendering: fenced code -> code window, rest -> paragraphs ----
  const LANG = { py: "Python", python: "Python", js: "JavaScript", javascript: "JavaScript", ts: "TypeScript", html: "HTML", css: "CSS",
    sh: "Bash", bash: "Bash", json: "JSON", cpp: "C++", c: "C", java: "Java", sql: "SQL", rs: "Rust", go: "Go", cs: "C#" };
  const COPY_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';

  function renderText(t) {
    return esc(t).split(/\n{2,}/).map(p => p.trim()).filter(Boolean).map(p =>
      "<p>" + p.replace(/`([^`\n]+)`/g, '<code class="inline">$1</code>')
               .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/\n/g, "<br>") + "</p>").join("");
  }
  function renderMarkdown(text) {
    let html = "", last = 0, m;
    const re = /```([\w+#-]*)[^\S\n]*\n?([\s\S]*?)(?:```|$)/g;
    while ((m = re.exec(text)) && m[0]) {
      html += renderText(text.slice(last, m.index));
      const key = (m[1] || "").toLowerCase(), code = m[2].replace(/\n$/, "");
      html += `<div class="code-win"><div class="code-head"><span class="code-lang">${esc(LANG[key] || key || "Code")}</span>` +
        `<button class="copy-btn" type="button">${COPY_ICON}<span>Copy</span></button></div>` +
        `<pre><code>${highlight(code, key)}</code></pre></div>`;
      last = m.index + m[0].length;
    }
    return html + renderText(text.slice(last));
  }

  // ---- copy button with "Copied!" feedback (event delegation) --------------
  async function copyText(t) {
    try { await navigator.clipboard.writeText(t); }
    catch (e) { const a = document.createElement("textarea"); a.value = t; document.body.appendChild(a); a.select(); document.execCommand("copy"); a.remove(); }
  }
  document.addEventListener("click", async e => {
    const btn = e.target.closest(".copy-btn");
    if (!btn) return;
    await copyText(btn.closest(".code-win").querySelector("code").innerText);
    btn.classList.add("done");
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6 9 17l-5-5"/></svg><span>Copied!</span>';
    setTimeout(() => { btn.classList.remove("done"); btn.innerHTML = COPY_ICON + "<span>Copy</span>"; }, 2000);
  });


  // ---- generated images: blurred placeholder -> live preview -> finished image ----
  const IMG_MARK = /^\[\[image:(\d+)\]\]$/;
  const DL_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M5 21h14"/></svg>';

  const imgUrl = id => "/image/" + id + "?v=2";                      // ?v=2 skips anything cached under the old 1-year header
  async function saveImage(id) {
    const blob = await (await fetch(imgUrl(id))).blob();       // the full-size PNG from SQLite
    const name = "rasputin-image-" + id + ".png";
    if (window.showSaveFilePicker) {                                 // Save As dialog: you pick the folder
      try {
        const h = await window.showSaveFilePicker({ suggestedName: name, types: [{ description: "PNG image", accept: { "image/png": [".png"] } }] });
        const w = await h.createWritable(); await w.write(blob); await w.close();
        return;
      } catch (e) { if (e && e.name === "AbortError") return; }     // cancelled -> do nothing; other errors -> fallback
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }

  function makeImageCard(id) {
    const card = document.createElement("div");
    card.className = "img-card pending";
    card.innerHTML = '<img class="img-el" alt="Generated image" draggable="false">' +
      '<div class="img-overlay"><div class="img-spinner"></div><div class="img-status">Starting...</div></div>' +
      '<button class="img-dl" type="button" title="Download">' + DL_ICON + '</button>';
    const img = card.querySelector(".img-el"), label = card.querySelector(".img-status");
    card.querySelector(".img-dl").onclick = () => saveImage(id);
    let lastStep = -1;
    const poll = async () => {
      if (!card.isConnected) return;                                 // chat was switched: stop polling
      let s;
      try { s = await (await fetch("/image/" + id + "/status")).json(); }
      catch (e) { return setTimeout(poll, 1500); }
      if (s.status === "done") {
        img.onload = () => { card.classList.remove("pending"); card.classList.add("done"); };
        img.src = imgUrl(id);
      } else if (s.status === "error") {
        card.classList.remove("pending"); card.classList.add("failed");
        card.innerHTML = '<div class="img-error">Image generation failed.<br><small></small></div>';
        card.querySelector("small").textContent = s.error || "";
      } else {
        label.textContent = s.step > 0 ? Math.round(100 * s.step / s.total) + "%" : "Loading model...";
        if (s.preview && s.step !== lastStep) { lastStep = s.step; img.src = "/image/" + id + "/preview?s=" + s.step; card.classList.add("has-preview"); }
        setTimeout(poll, 500);
      }
    };
    setTimeout(poll, 50);          // wait until the caller has put the card in the page (poll stops if it isn't attached)
    return card;
  }

  // ---- overrides of the original globals ----------------------------------
  window.appendMessage = function (sender, text) {
    const box = document.getElementById("chatbox");
    const isUser = sender === "You";
    const row = document.createElement("div");
    row.className = "msg-row " + (isUser ? "user" : "bot");
    row.innerHTML = (isUser ? "" : '<div class="avatar">R</div>') + '<div class="msg-body"></div>';
    const body = row.querySelector(".msg-body");
    const im = !isUser && IMG_MARK.exec(text);
    if (isUser) body.textContent = text; else if (im) body.appendChild(makeImageCard(+im[1])); else body.innerHTML = renderMarkdown(text);
    box.appendChild(row);
    box.scrollTop = box.scrollHeight;
    return row;
  };

  function typingRow() {
    const box = document.getElementById("chatbox"), row = document.createElement("div");
    row.className = "msg-row bot";
    row.innerHTML = '<div class="avatar">R</div><div class="msg-body"><div class="typing"><i></i><i></i><i></i></div></div>';
    box.appendChild(row); box.scrollTop = box.scrollHeight;
    return row;
  }

  // The send button is an arrow; while the AI is answering it becomes a stop square, and clicking it cancels the reply.
  const SEND_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m13 6 6 6-6 6"/></svg>';
  const STOP_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="2.5"/></svg>';
  let busy = false, aborter = null;
  function setBtn(generating) {
    const btn = document.getElementById("sendBtn");
    if (!btn) return;
    btn.innerHTML = generating ? STOP_ICON : SEND_ICON;
    btn.title = generating ? "Stop" : "Send";
    btn.setAttribute("aria-label", generating ? "Stop generating" : "Send message");
    btn.disabled = false;
    btn.style.display = "inline-flex"; btn.style.alignItems = "center"; btn.style.justifyContent = "center";
  }
  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("sendBtn");
    if (!btn) return;
    setBtn(false);
    // Capture phase, so while a reply is streaming the click means "stop" and never reaches the send handler.
    btn.addEventListener("click", ev => {
      if (!busy) return;
      ev.preventDefault(); ev.stopImmediatePropagation();
      if (aborter) aborter.abort();
    }, true);
  });

  window.sendMessage = async function () {
    const input = document.getElementById("userInput"), btn = document.getElementById("sendBtn");
    if (busy) { if (aborter) aborter.abort(); return; }   // Enter (or any send) while answering = stop
    const message = input.value.trim();
    if (!message) return;
    busy = true; aborter = new AbortController(); setBtn(true);
    appendMessage("You", message);
    input.value = ""; input.style.height = "auto";
    const row = typingRow(), body = row.querySelector(".msg-body"), box = document.getElementById("chatbox");
    let text = "", raf = 0, got = false;
    const paint = () => { raf = 0; body.innerHTML = renderMarkdown(text); box.scrollTop = box.scrollHeight; };
    const finish = () => { busy = false; aborter = null; setBtn(false); input.focus(); };
    try {
      const res = await fetch("/ask-stream", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, chat_id: currentChatId }), signal: aborter.signal });
      if (!res.ok || !res.body) throw new Error("Request failed (" + res.status + ")");
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {                                   // tokens arrive as they are generated
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
          if (!chunk.startsWith("data: ")) continue;
          const ev = JSON.parse(chunk.slice(6));
          if (ev.chat_id) { currentChatId = ev.chat_id; navSync(); }
          if (ev.error) throw new Error(ev.error);
          if (ev.t) { text += ev.t; got = true; if (!raf) raf = requestAnimationFrame(paint); }
          if (ev.done) { if (raf) cancelAnimationFrame(raf); text = ev.response; const im = IMG_MARK.exec(text);
            if (im) { body.replaceChildren(makeImageCard(+im[1])); box.scrollTop = box.scrollHeight; } else paint();
            finish(); refreshChatList(); }
          if (ev.title) refreshChatList();         // the title is made after the reply is already shown
        }
      }
    } catch (err) {
      if (raf) cancelAnimationFrame(raf);
      if (err && err.name === "AbortError") {          // the user pressed stop: keep what has been said so far
        if (got) paint(); else row.remove();
        refreshChatList();
        return;
      }
      text = got ? text + "\n\n[Connection fault: " + err.message + "]" : "Connection fault: " + err.message;
      paint();
    } finally { finish(); }
  };

  window.refreshChatList = async function () {
    const list = document.getElementById("chatList");
    if (!list) return;
    const chats = await (await fetch("/chats")).json();
    list.innerHTML = "";
    chats.forEach(c => {
      const li = document.createElement("li");
      li.className = "pc-item";
      li.innerHTML = `<div class="chat-entry${c.id === currentChatId ? " active" : ""}" style="display:flex;align-items:center">
        <a href="#" class="pc-link" style="flex:1;min-width:0"><span class="pc-mtext"></span></a>
        <div class="chat-actions"><button title="Rename">✎</button><button title="Delete">🗑</button></div></div>`;
      li.querySelector(".pc-mtext").textContent = c.title;
      li.querySelector("a").onclick = ev => { ev.preventDefault(); loadChat(c.id).then(refreshChatList); };
      const [rn, del] = li.querySelectorAll(".chat-actions button");
      rn.onclick = () => renameChat(c.id); del.onclick = () => deleteChat(c.id);
      list.appendChild(li);
    });
  };

  document.addEventListener("DOMContentLoaded", () => {
    const input = document.getElementById("userInput");
    input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; });
  });
})();

// ---- Terminal tab, safe exit, and screen-aware sizing ----
(function () {
  // Terminal: poll the server's captured console output while the tab is visible
  const term = { timer: null, since: 0, busy: false };
  const lineClass = t => /traceback|error|exception|critical|fatal/i.test(t) ? "tl-err"
    : /warn/i.test(t) ? "tl-warn" : /" \d{3} -/.test(t) ? "tl-dim" : "";
  function status(on) {
    const el = document.getElementById("termStatus");
    if (el) { el.className = on ? "on" : "off"; el.textContent = on ? "\u25CF LIVE" : "\u25CF OFFLINE"; }
  }
  async function poll() {
    if (term.busy) return;
    term.busy = true;
    try {
      const d = await (await fetch("/terminal-log?since=" + term.since)).json();
      const out = document.getElementById("termOut");
      if (d.lines.length) {
        const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 40;
        const frag = document.createDocumentFragment();
        d.lines.forEach(l => {
          const e = document.createElement("div");
          e.className = "tl " + lineClass(l.t);
          e.textContent = l.t || " ";
          frag.appendChild(e);
        });
        out.appendChild(frag);
        while (out.childElementCount > 3000) out.removeChild(out.firstChild);
        if (stick && document.getElementById("termAuto").checked) out.scrollTop = out.scrollHeight;
      }
      term.since = d.next;
      status(true);
    } catch (e) { status(false); }
    finally { term.busy = false; }
  }
  window.clearTerm = () => { document.getElementById("termOut").innerHTML = ""; };

  // Add the Terminal view on top of the original switchView
  const origSwitch = window.switchView;
  window.switchView = function (v) {
    origSwitch(v);
    const t = document.getElementById("terminalView");
    if (v === "terminal") {
      document.getElementById("chatInterfaceView").style.display = "none";
      t.style.display = "block";
      if (!term.timer) { poll(); term.timer = setInterval(poll, 700); }
    } else {
      t.style.display = "none";
      clearInterval(term.timer); term.timer = null;
    }
    fitChat(); setTimeout(fitChat, 100);
  };

  // Safe exit: stop mic/speech, ask the server to close everything, then the window closes
  window.exitApp = async function () {
    if (!confirm("Shut down Rasputin?")) return;
    try { stopVoice(); } catch (e) {}
    try { stopChessSpeech(); } catch (e) {}
    const ov = document.createElement("div");
    ov.className = "exit-overlay";
    ov.innerHTML = "<div><b>Shutting down...</b><br>Closing everything safely.</div>";
    document.body.appendChild(ov);
    try { await fetch("/shutdown", { method: "POST", headers: { "X-Rasputin-Action": "shutdown" } }); } catch (e) {}
    setTimeout(() => { ov.innerHTML = "<div><b>Rasputin has shut down.</b><br>You can close this window.</div>"; }, 4000);
  };

  // Fit panes to the window by measuring: shrink if the page overflows, grow if there is a gap above the footer
  const PANES = [[".chat-shell", "chatInterfaceView"], [".term-shell", "terminalView"]];
  const px = v => parseFloat(v) || 0;
  function fitChat() {
    const content = document.querySelector(".pc-content"), footer = document.querySelector(".pc-footer");
    const container = document.querySelector(".pc-container");
    PANES.forEach(([sel, vid]) => {
      const shell = document.querySelector(sel), view = document.getElementById(vid);
      if (!shell || !view || view.offsetParent === null || !content || !footer) return;
      const cc = container ? getComputedStyle(container) : null, fc = getComputedStyle(footer);
      const baseline = (cc ? px(cc.paddingBottom) + px(cc.marginBottom) : 0) + px(getComputedStyle(content).marginBottom) + px(fc.marginTop);
      const measure = () => ({
        cur: shell.getBoundingClientRect().height,
        over: document.documentElement.scrollHeight - window.innerHeight,
        slack: footer.getBoundingClientRect().top - content.getBoundingClientRect().bottom - baseline
      });
      const apply = (m, delta) => {
        const want = Math.max(300, m.cur + delta);
        if (Math.abs(want - m.cur) < 1) return false;
        const local = parseFloat(shell.style.height) || shell.offsetHeight;
        shell.style.height = (local * want / m.cur) + "px"; // ratio keeps this correct however the webview applies CSS zoom
        return true;
      };
      for (let i = 0; i < 5; i++) {
        const m = measure();
        const delta = m.over > 1 ? -m.over : (m.slack > 1 ? m.slack : 0);
        if (!delta || !apply(m, delta)) break;
      }
      const m = measure();                               // never end overflowing: the footer must stay visible
      if (m.over > 0) apply(m, -m.over);
    });
  }
  function autoZoom() {
    const raw = Math.min(window.innerWidth / 1400, window.innerHeight / 850);
    const c = Math.max(0.8, Math.min(1.5, raw));
    return ZOOM_STEPS.reduce((a, b) => Math.abs(b - c) < Math.abs(a - c) ? b : a);
  }
  window.autoFitZoom = function () { setTabZoom(autoZoom()); setTimeout(fitChat, 50); };
  window.fitChat = fitChat;

  let t;
  window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(fitChat, 60); });
  window.addEventListener("load", () => {
    TAB_NAMES.terminal = "TERMINAL"; // lets zoom and back/forward treat Terminal as a normal tab
    try {
      const sv = JSON.parse(localStorage.getItem("rasputinTabZoom") || "{}");
      tabZoom.terminal = (sv.terminal >= 0.5 && sv.terminal <= 3) ? sv.terminal : 1;
      if (!localStorage.getItem("rasputinAutoZoomV1")) { // first launch: pick a zoom for this screen once
        const z = autoZoom();
        Object.keys(tabZoom).forEach(k => tabZoom[k] = z);
        localStorage.setItem("rasputinTabZoom", JSON.stringify(tabZoom));
        localStorage.setItem("rasputinAutoZoomV1", "1");
      }
    } catch (e) { if (!("terminal" in tabZoom)) tabZoom.terminal = 1; }
    const origApply = window.applyTabZoom;             // re-fit immediately whenever the zoom changes
    window.applyTabZoom = function (v) { origApply(v); fitChat(); setTimeout(fitChat, 120); };
    applyTabZoom(NAV.view);
    setTimeout(fitChat, 80);
  });
})();

// ---- LLM backend chip (GPU / CPU) ----
(function () {
  async function chip() {
    const el = document.getElementById("llmChip");
    if (!el) return;
    try {
      const s = await (await fetch("/llm-status")).json();
      if (s.backend === "GPU") {
        el.className = "gpu";
        el.textContent = "LLM // GPU" + (s.vram_mb ? " " + (s.vram_mb / 1024).toFixed(1) + " GB" : "") + (s.tok_s ? " " + s.tok_s + " tok/s" : "");
      } else if (s.backend === "CPU") {
        el.className = "cpu";
        el.textContent = "LLM // CPU ONLY" + (s.tok_s ? " " + s.tok_s + " tok/s" : "");
      } else { el.textContent = "LLM // LOADING"; setTimeout(chip, 3000); }
    } catch (e) { setTimeout(chip, 3000); }
  }
  window.addEventListener("load", chip);
})();
