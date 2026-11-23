// Per-account settings + sign-out. Loaded in <head>, BEFORE every other script, because the other scripts
// read localStorage while the page is still loading.
//
// What it does:
//  1. Copies this account's saved settings from the server into localStorage (so the existing code sees them).
//  2. Whenever one of those settings is written, quietly saves it back to the server (account settings follow
//     the person to any device, and survive the PC window clearing its browser data on exit).
//  3. If the server ever says "sign in again" (status 401), goes to the sign-in page.
//
// Zoom is remembered separately for the PC window and for phones (their screens differ). The microphone and
// browser voice choice stay on each device, because they only make sense there.
(function () {
  "use strict";
  var A = window.RASPUTIN_ACCOUNT || {};

  // ---- 401 -> sign-in page --------------------------------------------------
  if (window.fetch) {
    var realFetch = window.fetch;
    window.fetch = function () {
      return realFetch.apply(this, arguments).then(function (r) {
        if (r.status === 401 && r.headers && r.headers.get("X-Rasputin-Auth") === "login" && location.pathname !== "/login") location.href = "/login";
        return r;
      });
    };
  }

  window.signOut = function () {
    flush(true);
    fetch("/logout", { method: "POST", credentials: "same-origin" }).catch(function () {}).then(function () { location.href = "/login"; });
  };

  if (!A.id) return;                                   // not signed in (should not happen on this page)

  var ls;
  try { ls = window.localStorage; ls.getItem("rasputinProbe"); } catch (e) { return; }   // storage blocked: nothing to sync

  var SYNC = {                                          // local key -> how it is saved
    rasputinVoicePrefs: { keepLocal: ["micId", "voiceURI"] },
    rasputinChessPrefs: {},
    rasputinTabZoom: { perDevice: true },
    rasputinAutoZoomV1: { perDevice: true }
  };
  var cls = window.matchMedia && window.matchMedia("(pointer: coarse) and (max-width: 820px)").matches ? "phone" : "pc";
  var OWNER = "rasputinLocalOwner";
  var rawSet = Storage.prototype.setItem, rawRemove = Storage.prototype.removeItem;

  function serverKey(k) { return SYNC[k].perDevice ? k + "@" + cls : k; }
  function parse(s) { try { var o = JSON.parse(s); return o && typeof o === "object" ? o : null; } catch (e) { return null; } }

  // what gets sent to the server for a local value (device-only fields removed)
  function outgoing(k, value) {
    var keep = SYNC[k].keepLocal;
    if (!keep) return value;
    var o = parse(value);
    if (!o) return value;
    keep.forEach(function (f) { delete o[f]; });
    return JSON.stringify(o);
  }

  // ---- 1. server -> localStorage ---------------------------------------------
  var pending = {}, timer = 0, failures = 0;
  var owner = ls.getItem(OWNER);
  if (owner !== null && owner !== String(A.id)) {        // someone else used this browser: do not leak their settings
    Object.keys(SYNC).forEach(function (k) { rawRemove.call(ls, k); });
  }
  var saved = A.settings || {};
  Object.keys(SYNC).forEach(function (k) {
    var sv = saved[serverKey(k)], lv = ls.getItem(k);
    if (typeof sv === "string") {
      var keep = SYNC[k].keepLocal, merged = sv;
      if (keep && lv) {                                  // keep this device's own mic / voice choice
        var s = parse(sv), l = parse(lv);
        if (s && l) { keep.forEach(function (f) { if (f in l) s[f] = l[f]; }); merged = JSON.stringify(s); }
      }
      rawSet.call(ls, k, merged);
    } else if (lv !== null) {                            // first sign-in with settings already on this device: save them to the account
      pending[serverKey(k)] = outgoing(k, lv);
    }
  });
  rawSet.call(ls, OWNER, String(A.id));

  // ---- 2. localStorage -> server ---------------------------------------------
  function flush(now) {
    clearTimeout(timer); timer = 0;
    var keys = Object.keys(pending || {});
    if (!keys.length) return;
    var batch = pending; pending = {};
    fetch("/account/settings", {
      method: "PUT", credentials: "same-origin", keepalive: true,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ settings: batch })
    }).then(function (r) {
      if (r.status >= 500) throw new Error("save failed");   // 4xx will not get better by retrying
      failures = 0;
    }).catch(function () {                               // offline or busy: keep it and try again
      keys.forEach(function (k) { if (!(k in pending)) pending[k] = batch[k]; });
      failures++;
      if (!now) timer = setTimeout(flush, Math.min(60000, 3000 * failures));
    });
  }
  function queue(k, value) {
    pending[serverKey(k)] = value === null ? null : outgoing(k, value);
    clearTimeout(timer);
    timer = setTimeout(flush, 700);
  }
  Storage.prototype.setItem = function (k, v) {
    rawSet.apply(this, arguments);
    if (this === ls && SYNC[k]) queue(k, String(v));
  };
  Storage.prototype.removeItem = function (k) {
    rawRemove.apply(this, arguments);
    if (this === ls && SYNC[k]) queue(k, null);
  };
  window.addEventListener("pagehide", function () { flush(true); });
  document.addEventListener("visibilitychange", function () { if (document.hidden) flush(true); });
  if (Object.keys(pending).length) timer = setTimeout(flush, 1500);
})();
