/* Sort X - shared by every page: the home screen, .X and .Image.
   Gives each page the same header (app switcher + theme button), helpers, dialogs and
   the connection to the local Sort X server. Pages use it through window.SX. */
(function () {
  "use strict";
  const TOKEN = new URLSearchParams(location.search).get("t") || "";
  const settings = (window.SORTX_SETTINGS && typeof window.SORTX_SETTINGS === "object") ? window.SORTX_SETTINGS : {};

  // apply the saved theme before the page draws
  if (settings.theme === "light" || settings.theme === "dark") {
    document.documentElement.setAttribute("data-theme", settings.theme);
  }

  const $ = (id) => document.getElementById(id);

  // ---------- icons ----------
  const P = {
    Images: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
    Videos: '<rect x="2" y="6" width="14" height="12" rx="2"/><path d="m22 8-6 4 6 4z"/>',
    Audio: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    Documents: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
    Spreadsheets: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/>',
    Presentations: '<rect x="2" y="3" width="20" height="13" rx="2"/><path d="M12 16v5M8 21h8"/>',
    Archives: '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4"/>',
    Installers: '<path d="M12 3v12m0 0-4-4m4 4 4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    Code: '<path d="m8 6-6 6 6 6M16 6l6 6-6 6"/>',
    Fonts: '<path d="M4 20 10 4h2l6 16M6.5 14h9"/>',
    Other: '<circle cx="12" cy="12" r="9"/><path d="M8 12h.01M12 12h.01M16 12h.01"/>',
    Duplicates: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
    Trash: '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6"/>',
    reveal: '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    check: '<path d="m5 12 5 5L20 7"/>',
    eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    warn: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    home: '<path d="m3 11 9-7 9 7"/><path d="M5 10v10h14V10"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    left: '<path d="m15 6-6 6 6 6"/>',
    right: '<path d="m9 6 6 6-6 6"/>',
    restore: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  };
  const icon = (k) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${P[k] || P.Other}</svg>`;

  // ---------- text helpers ----------
  const h = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const size = (n) => {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + " " + u[i];
  };
  const plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? "" : "s"}`;
  const base = (p) => (p || "").replace(/\/+$/, "").split("/").pop() || p;
  const tilde = (p) => SX.home && p && p.startsWith(SX.home) ? "~" + p.slice(SX.home.length) : p;
  const date = (secs) => new Date(secs * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  const withToken = (url) => url + (url.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(TOKEN);

  // ---------- server ----------
  async function api(path, body) {
    const res = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "X-Token": TOKEN, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  // Settings are saved in a file on the Mac, so they survive restarts. Each key is saved on its own.
  const store = {
    get(k) { return settings[k] ?? null; },
    set(k, v) {
      settings[k] = v;
      api("/api/settings", { settings: { [k]: v } }).catch(() => {});
    },
  };

  // ---------- toast & confirm dialog ----------
  function ensureRoots() {
    for (const id of ["modalRoot", "toastRoot"]) {
      if (!$(id)) { const d = document.createElement("div"); d.id = id; document.body.appendChild(d); }
    }
  }
  function toast(msg, err = false) {
    ensureRoots();
    const el = document.createElement("div");
    el.className = "toast" + (err ? " err" : "");
    el.textContent = msg;
    $("toastRoot").replaceChildren(el);
    setTimeout(() => el.remove(), err ? 6000 : 3200);
  }
  function confirmBox({ title, body, path, ok = "Continue", danger = false }) {
    ensureRoots();
    return new Promise((resolve) => {
      $("modalRoot").innerHTML = `
        <div class="scrim"><div class="modal" role="dialog" aria-modal="true">
          <h3>${h(title)}</h3><p>${body}</p>
          ${path ? `<div class="m-path">${h(path)}</div>` : ""}
          <div class="m-actions">
            <button class="btn" data-a="no">Cancel</button>
            <button class="btn ${danger ? "danger" : "primary"}" data-a="yes">${h(ok)}</button>
          </div></div></div>`;
      const done = (v) => { $("modalRoot").innerHTML = ""; document.removeEventListener("keydown", key, true); resolve(v); };
      const key = (e) => {
        if (e.key === "Escape") { e.stopPropagation(); done(false); }
        if (e.key === "Enter") { e.preventDefault(); done(true); }
      };
      document.addEventListener("keydown", key, true);
      $("modalRoot").querySelector('[data-a="yes"]').focus();
      $("modalRoot").onclick = (e) => {
        const a = e.target.closest("[data-a]");
        if (a) done(a.dataset.a === "yes");
        else if (e.target.classList.contains("scrim")) done(false);
      };
    });
  }

  // ---------- header: brand, app switcher, theme ----------
  const APPS = {
    home:  { href: "/",      logo: '<span class="l-sort">Sort</span><span class="l-x">X</span>',      title: 'Sort <span class="brand-x">X</span>', sub: "Your tools for tidy folders" },
    x:     { href: "/x",     logo: ".X",     title: ".X",     sub: "Sort files into folders and set aside duplicates" },
    image: { href: "/image", logo: ".Image", title: ".Image", sub: "Find duplicate and similar pictures" },
  };
  const NAV = [
    { id: "home", label: `${icon("home")}<span>Home</span>` },
    { id: "x", label: `<span class="nav-mark mark-x">.X</span><span>Files</span>` },
    { id: "image", label: `<span class="nav-mark mark-image">.Image</span><span>Pictures</span>` },
  ];

  function renderHeader() {
    const header = document.querySelector("header.top");
    if (!header) return;
    const app = document.body.dataset.app || "home";
    const a = APPS[app];
    const extras = [...header.children];   // page-specific buttons (e.g. Undo) go next to the theme button
    header.innerHTML = `
      <a class="brand" href="${h(withToken("/"))}" title="Sort X home">
        <div class="logo logo-${app}" aria-hidden="true">${a.logo}</div>
        <div>
          ${app === "home" ? "" : `<div class="eyebrow">Sort X</div>`}
          <h1>${a.title}</h1>
          <p class="sub">${h(a.sub)}</p>
        </div>
      </a>
      <nav class="appnav" aria-label="Sort X apps">
        ${NAV.map((n) => `<a href="${h(withToken(APPS[n.id].href))}" class="${n.id === app ? "on" : ""}" ${n.id === app ? 'aria-current="page"' : ""}>${n.label}</a>`).join("")}
      </nav>
      <div class="top-actions">
        <button class="theme-btn" id="themeBtn" type="button">
          <svg class="i" id="themeIcon" viewBox="0 0 24 24"></svg><span id="themeLabel">Dark mode</span>
        </button>
      </div>`;
    const actions = header.querySelector(".top-actions");
    extras.forEach((el) => actions.insertBefore(el, actions.firstChild));
    $("themeBtn").onclick = () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      store.set("theme", next);
      renderThemeBtn();
    };
    renderThemeBtn();
  }

  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const currentTheme = () => document.documentElement.getAttribute("data-theme") || (darkQuery.matches ? "dark" : "light");
  function renderThemeBtn() {
    if (!$("themeBtn")) return;
    const dark = currentTheme() === "dark";   // the button offers the other theme
    $("themeIcon").innerHTML = dark ? P.sun : P.moon;
    $("themeLabel").textContent = dark ? "Light mode" : "Dark mode";
    $("themeBtn").title = dark ? "Switch to light mode" : "Switch to dark mode";
  }
  darkQuery.addEventListener("change", renderThemeBtn);   // follows the Mac until you pick one

  // ---------- keep the local server running while a Sort X window is open ----------
  const ping = () => api("/api/ping", {}).then((d) => { SX.home = d.home; return d; });
  const ready = ping().catch(() => ({}));
  setInterval(() => ping().catch(() => {}), 15000);
  window.addEventListener("pagehide", () => navigator.sendBeacon(`/api/bye?t=${encodeURIComponent(TOKEN)}`));

  const SX = {
    TOKEN, $, P, icon, h, size, plural, base, tilde, date, withToken,
    api, store, toast, confirmBox, ready, home: "",
  };
  window.SX = SX;

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => { renderHeader(); ensureRoots(); });
  else { renderHeader(); ensureRoots(); }
})();
