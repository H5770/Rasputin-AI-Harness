// Admin tab: create accounts, see/reset/delete them, and see the web addresses (home Wi-Fi + optional tunnel).
// Only exists on the PC window for signed-in accounts that have admin access: the server leaves this tab (and this
// script) out of the page otherwise, and refuses the /admin/api/... calls for anyone else.
// Loaded after home.js in index.html.
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const A = { active: false, loaded: false, busy: false };

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
    const m = $("adMsg");
    m.textContent = text || "";
    m.className = "ad-msg" + (bad ? " bad" : "");
    m.style.display = text ? "block" : "none";
  }
  function when(s) {                                   // server times are UTC "YYYY-MM-DD HH:MM:SS"
    if (!s) return "never";
    const d = new Date(s.replace(" ", "T") + "Z");
    return isNaN(d) ? s : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  }

  function renderAccounts(d) {
    const fill = $("adLevel"), keep = fill.value;
    fill.textContent = "";
    d.levels.forEach(l => { const o = el("option", "", l.label); o.value = l.value; fill.append(o); });
    if ([...fill.options].some(o => o.value === keep)) fill.value = keep;

    const list = $("adList");
    list.textContent = "";
    if (!d.accounts.length) list.append(el("div", "ad-empty", "No accounts yet."));
    d.accounts.forEach(a => {
      const row = el("div", "ad-row");
      const name = el("div", "ad-name");
      name.append(el("span", "", a.username), el("span", "ad-tag" + (a.level >= 2 ? " owner" : ""), a.level_name.toUpperCase()));
      if (a.you) name.append(el("span", "ad-tag good", "YOU"));
      row.append(name, el("div", "ad-meta", "Created " + when(a.created_at) + " // Last sign-in " + when(a.last_login)));
      const act = el("div", "ad-actions");
      if (a.can_manage || a.you) {
        const pw = el("button", "ad-btn", "RESET PASSWORD");
        pw.type = "button";
        pw.addEventListener("click", () => resetPassword(a));
        act.append(pw);
      }
      if (a.can_manage && !a.you) {
        const del = el("button", "ad-btn danger", "DELETE");
        del.type = "button";
        del.addEventListener("click", () => removeAccount(a));
        act.append(del);
      }
      row.append(act);
      list.append(row);
    });
    $("adStatus").textContent = "● " + d.accounts.length + " ACCOUNT" + (d.accounts.length === 1 ? "" : "S") +
      " // SIGNED IN AS " + d.me.username.toUpperCase() + " (" + d.me.level_name.toUpperCase() + ")";
  }

  // ---- Web Addresses: the local address + the (switchable) tunnel ------------------------------
  const QR_SRC = "/static/img/tunnel-qr.png";
  let lastInfo = null, tun = { state: "off", on: false, address: "https://welldecked-collins-nonsecretive.ngrok-free.dev" }, poll = null;

  function copyBtn(text) {
    const b = el("button", "ad-btn", "COPY");
    b.type = "button";
    b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(text); b.textContent = "COPIED"; setTimeout(() => b.textContent = "COPY", 1500); } catch (e) {}
    });
    return b;
  }

  function openQr() {
    if ($("adQr")) return;
    const wrap = el("div", "ad-qr-back");
    wrap.id = "adQr";
    const card = el("div", "ad-qr-card");
    const img = el("img");
    img.src = QR_SRC; img.alt = "QR code for the tunnel address";
    img.addEventListener("error", () => { img.replaceWith(el("div", "ad-qr-miss", "QR image not found. Put tunnel-qr.png in static/img/")); });
    const url = el("code", "", tun.address);
    const close = el("button", "ad-btn primary", "CLOSE");
    close.type = "button";
    const shut = () => { wrap.remove(); document.removeEventListener("keydown", esc); };
    const esc = e => { if (e.key === "Escape") shut(); };
    close.addEventListener("click", shut);
    wrap.addEventListener("click", e => { if (e.target === wrap) shut(); });
    document.addEventListener("keydown", esc);
    card.append(img, url);
    if (!tun.on) card.append(el("div", "ad-qr-note", "The tunnel is OFF. Switch it on before scanning."));
    card.append(close);
    wrap.append(card);
    $("adminView").append(wrap);
  }

  async function setTunnel(on) {
    const sw = $("adTunSwitch");
    if (sw) sw.disabled = true;
    tun = Object.assign({}, tun, { state: on ? "starting" : tun.state });
    renderInfo(lastInfo);
    const r = await api("/admin/api/tunnel", "POST", { on: on });
    if (r.error && !r.state) r.state = "error";
    tun = Object.assign({}, tun, r);
    renderInfo(lastInfo);
    watchTunnel();
  }

  async function setPublic(on) {
    if (on && !confirm("Open the tunnel to ANYONE on the internet?\n\nAnyone who has the link will be able to reach the Rasputin sign-in page, not just your home connection. Only your accounts' passwords will protect it.\n\nIt switches itself off when you close Rasputin or turn the tunnel off.")) {
      renderInfo(lastInfo);                              // put the switch back
      return;
    }
    const r = await api("/admin/api/tunnel", "POST", { public: on });
    tun = Object.assign({}, tun, r.state ? r : {});
    if (r.error) say(r.error, true);
    renderInfo(lastInfo);
  }

  function watchTunnel() {                              // while ngrok is starting, ask again until it is on or failed
    clearTimeout(poll);
    if (tun.state !== "starting" || !A.active) return;
    poll = setTimeout(async () => {
      const r = await api("/admin/api/tunnel");
      if (r.state) { tun = Object.assign({}, tun, r); renderInfo(lastInfo); }
      watchTunnel();
    }, 1500);
  }

  function renderInfo(d) {
    lastInfo = d || lastInfo || {};
    d = lastInfo;
    const box = $("adAddr");
    box.textContent = "";

    // 1. local address
    const local = d.local_url || "https://192.168.4.129:5443";
    const r1 = el("div", "ad-url");
    r1.append(el("div", "ad-url-k", "LOCAL"), el("code", "", local),
              el("span", "ad-tag good", d.urls && d.urls.length ? "HOME WI-FI" : "NOT RUNNING"), copyBtn(local));
    box.append(r1);

    // 2. tunnel address + switch + QR
    const st = tun.state || "off";
    const r2 = el("div", "ad-url ad-tunnel");
    const tag = el("span", "ad-tag" + (st === "on" && !tun.public ? " good" : st === "error" || tun.public ? " bad" : ""),
                   st === "on" ? (tun.public ? "ONLINE // OPEN" : "ONLINE") : st === "starting" ? "STARTING…" : st === "error" ? "ERROR" : "OFF");
    const qr = el("button", "ad-btn", "QR CODE");
    qr.type = "button";
    qr.addEventListener("click", openQr);
    r2.append(el("div", "ad-url-k", "TUNNEL"), el("code", "", tun.address), tag, copyBtn(tun.address), qr);
    box.append(r2);

    const ctl = el("label", "ad-switch");
    const sw = el("input");
    sw.type = "checkbox"; sw.id = "adTunSwitch";
    sw.checked = st === "on" || st === "starting";
    sw.disabled = st === "starting";
    sw.addEventListener("change", () => setTunnel(sw.checked));
    ctl.append(sw, el("span", "ad-slider"), el("span", "ad-switch-t", "INTERNET TUNNEL"),
               el("span", "ad-switch-s", st === "on" ? "ON" : st === "starting" ? "STARTING" : "OFF"));
    box.append(ctl);

    const pub = el("label", "ad-switch warn");
    const psw = el("input");
    psw.type = "checkbox"; psw.id = "adPubSwitch";
    psw.checked = st === "on" && !!tun.public;
    psw.disabled = st !== "on";
    psw.addEventListener("change", () => setPublic(psw.checked));
    pub.append(psw, el("span", "ad-slider"), el("span", "ad-switch-t", "OPEN TO ANYONE"),
               el("span", "ad-switch-s", st !== "on" ? "TUNNEL OFF" : tun.public ? "ON" : "HOME ONLY"));
    box.append(pub);

    if (st === "error" && tun.error) box.append(el("div", "ad-note bad", tun.error));
    else if (st !== "on") box.append(el("div", "ad-note", "The tunnel is off, so nothing is exposed to the internet. It opens only when you switch it on, and it closes again when you switch it off or exit Rasputin."));
    else if (tun.public) box.append(el("div", "ad-note bad", "OPEN: anyone with the link can reach the sign-in page. Switch OPEN TO ANYONE off to limit it to your home connection again."));
    else box.append(el("div", "ad-note", "The tunnel is live, but it only answers requests that come from your home internet connection. Switch OPEN TO ANYONE on to let people outside your Wi-Fi in."));

    if (d.setup_urls && d.setup_urls.length) {
      const n = el("div", "ad-note");
      n.append("First time on a phone: open ");
      n.append(el("b", "", d.setup_urls[0]));
      n.append(" to install the certificate, then open the LOCAL address, sign in, and add it to the Home Screen.");
      box.append(n);
    }
  }

  async function load() {
    if (!A.active || A.busy) return;
    A.busy = true;
    try {
      const [acc, info, tn] = await Promise.all([api("/admin/api/accounts"), api("/admin/api/info"), api("/admin/api/tunnel")]);
      if (tn && tn.state) tun = Object.assign({}, tun, tn);
      if (acc.error) { $("adStatus").textContent = "● " + acc.error; return; }
      renderAccounts(acc);
      renderInfo(info);
      watchTunnel();
      A.loaded = true;
    } catch (e) {
      $("adStatus").textContent = "● OFFLINE // " + e.message;
    } finally { A.busy = false; }
  }

  async function createAccount() {
    const user = $("adUser"), pass = $("adPass"), level = $("adLevel"), b = $("adCreate");
    if (!user.value.trim() || !pass.value) { say("Enter a username and a password.", true); return; }
    b.disabled = true;
    try {
      const r = await api("/admin/api/accounts", "POST", { username: user.value.trim(), password: pass.value, level: +level.value });
      if (r.error) { say(r.error, true); return; }
      say("Account \"" + r.account.username + "\" created (" + r.account.level_name + ").");
      user.value = ""; pass.value = "";
      await load();
    } finally { b.disabled = false; }
  }

  async function resetPassword(a) {
    const pw = prompt("New password for " + a.username + " (at least 6 characters).\nThey will be signed out everywhere:");
    if (pw === null) return;
    const r = await api("/admin/api/accounts/" + a.id + "/password", "POST", { password: pw });
    say(r.error || "Password changed for " + a.username + ".", !!r.error);
  }

  async function removeAccount(a) {
    if (!confirm("Delete the account \"" + a.username + "\" and its saved settings?\nTheir chats are not deleted.")) return;
    const r = await api("/admin/api/accounts/" + a.id, "DELETE");
    say(r.error || "Account \"" + a.username + "\" deleted.", !!r.error);
    load();
  }

  function wire() {
    $("adCreate").addEventListener("click", createAccount);
    $("adRefresh").addEventListener("click", () => { say(""); load(); });
    $("adShow").addEventListener("change", e => { $("adPass").type = e.target.checked ? "text" : "password"; });
    [$("adUser"), $("adPass")].forEach(i => i.addEventListener("keydown", e => { if (e.key === "Enter") createAccount(); }));
  }

  // ---- tab plumbing (same pattern as home.js) ------------------------------
  const prev = window.switchView;
  window.switchView = function (v) {
    prev.apply(this, arguments);
    const view = $("adminView");
    if (!view) return;
    if (v === "admin") {
      $("chatInterfaceView").style.display = "none";
      view.style.display = "block";
      A.active = true;
      load();
    } else {
      view.style.display = "none";
      A.active = false;
      clearTimeout(poll);
    }
  };

  window.addEventListener("load", () => {
    TAB_NAMES.admin = "ADMIN";                         // lets zoom and back/forward treat Admin as a normal tab
    try {
      const sv = JSON.parse(localStorage.getItem("rasputinTabZoom") || "{}");
      tabZoom.admin = (sv.admin >= 0.5 && sv.admin <= 3) ? sv.admin : (tabZoom.chat || 1);
    } catch (e) { tabZoom.admin = tabZoom.chat || 1; }
  });

  wire();
})();
