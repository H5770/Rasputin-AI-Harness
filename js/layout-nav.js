// Sidebar hide/show (Ctrl+B) and the collapsible "Apps" group. The group is always closed when the page starts.
(function () {
  "use strict";
  const KEY = "rasputinSidebarHidden";
  const $ = id => document.getElementById(id);

  // ---- hide / show the left bar ----
  function setHidden(h) {
    document.body.classList.toggle("sb-hidden", h);
    const b = $("sbToggle");
    if (b) b.innerHTML = h ? "&#9776; MENU" : "&#9664; HIDE MENU";
    try { localStorage.setItem(KEY, h ? "1" : "0"); } catch (e) {}
    setTimeout(() => window.dispatchEvent(new Event("resize")), 280);   // lets the 3D core / chess re-measure
  }
  window.toggleSidebar = function (show) {
    const hidden = document.body.classList.contains("sb-hidden");
    setHidden(show === undefined ? !hidden : !show);
  };
  let saved = false;
  try { saved = localStorage.getItem(KEY) === "1"; } catch (e) {}
  setHidden(saved);
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "b") {
      e.preventDefault(); window.toggleSidebar();
    }
  });

  // ---- Apps group (PC) ----
  const group = $("appsGroup"), head = $("appsHead");
  function setOpen(o) {
    if (!group) return;
    group.classList.toggle("open", o);
    head.setAttribute("aria-expanded", o ? "true" : "false");
  }
  if (head) head.addEventListener("click", () => setOpen(!group.classList.contains("open")));
  setOpen(false);                                           // closed on every start

  // ---- mark the current tab ----
  function viewOf(a) { const m = /switchView\('(\w+)'\)/.exec(a.getAttribute("onclick") || ""); return m ? m[1] : ""; }
  const links = group ? [...group.querySelectorAll(".ap-list a")] : [];
  links.forEach(a => { a.dataset.view = viewOf(a); });
  function mark(v) {
    let any = false;
    links.forEach(a => { const on = a.dataset.view === v; a.classList.toggle("active", on); any = any || on; });
    if (group) group.classList.toggle("has-active", any);
  }
  const prev = window.switchView;
  window.switchView = function (v) {
    const r = prev.apply(this, arguments);
    mark(v);
    return r;
  };

  // ---- Apps group (phone menu) ----
  const ph = $("phoneAppsHead"), pb = $("phoneApps");
  if (ph && pb) ph.addEventListener("click", () => {
    const o = !pb.classList.contains("open");
    pb.classList.toggle("open", o);
    ph.setAttribute("aria-expanded", o ? "true" : "false");
  });
})();
