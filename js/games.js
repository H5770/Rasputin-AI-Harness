// Games tab shell: a sub-tab strip + one panel per game. Add a game by calling Games.register({id, name, mount(el), unmount()})
// from its own script (see mindcraft.js) and listing it in python/games/__init__.py.
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const G = window.Games = {
    list: {}, cur: null, active: false,
    register(g) { this.list[g.id] = g; if ($("gmTabs")) draw(); },
    open(id) {
      if (this.cur && this.cur !== id && this.list[this.cur]) this.list[this.cur].unmount();
      this.cur = id;
      const panel = $("gmPanel");
      panel.textContent = "";
      if (this.list[id]) this.list[id].mount(panel);
      draw();
    },
    async api(path, method, body) {                       // shared by game panels
      const opt = { method: method || "GET", credentials: "same-origin", headers: {} };
      if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
      const r = await fetch(path, opt);
      let d = {};
      try { d = await r.json(); } catch (e) {}
      if (!r.ok && !d.error) d.error = "Request failed (" + r.status + ")";
      return d;
    },
  };

  function draw() {
    const bar = $("gmTabs");
    bar.textContent = "";
    Object.values(G.list).forEach(g => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = g.name;
      b.className = "gm-tab" + (g.id === G.cur ? " on" : "");
      b.onclick = () => G.open(g.id);
      bar.appendChild(b);
    });
  }

  // tab plumbing (same pattern as discord.js)
  const prev = window.switchView;
  window.switchView = function (v) {
    prev.apply(this, arguments);
    const view = $("gamesView");
    if (!view) return;
    if (v === "games") {
      $("chatInterfaceView").style.display = "none";
      view.style.display = "block";
      G.active = true;
      const first = G.cur || Object.keys(G.list)[0];
      if (first) G.open(first);
    } else {
      view.style.display = "none";
      if (G.active && G.cur && G.list[G.cur]) G.list[G.cur].unmount();   // stop polling while hidden
      G.active = false;
    }
  };

  window.addEventListener("load", () => {
    TAB_NAMES.games = "GAMES";
    try {
      const sv = JSON.parse(localStorage.getItem("rasputinTabZoom") || "{}");
      tabZoom.games = (sv.games >= 0.5 && sv.games <= 3) ? sv.games : (tabZoom.chat || 1);
    } catch (e) { tabZoom.games = tabZoom.chat || 1; }
  });
})();
