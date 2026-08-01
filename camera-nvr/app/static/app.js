"use strict";

const grid = document.getElementById("grid");
const cardTpl = document.getElementById("cardTpl");
const cards = new Map(); // id -> {el, dot, badge, res, ratio}

let editingId = null; // null = neue Kamera, sonst id der bearbeiteten

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.status + " " + res.statusText;
    try { const j = await res.json(); if (j.detail) detail = j.detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// -- Live-Gitter --------------------------------------------------------------

function ptzSend(id, params) {
  const q = new URLSearchParams(params).toString();
  fetch(`/api/ptz/${id}?${q}`, { method: "POST" }).catch(() => {});
}

function wirePtz(id, el) {
  const speed = 0.5;
  const map = {
    up:   { tilt: speed },
    down: { tilt: -speed },
    left: { pan: -speed },
    right:{ pan: speed },
    zin:  { zoom: speed },
    zout: { zoom: -speed },
  };
  el.querySelectorAll(".ptz button").forEach((btn) => {
    const dir = btn.dataset.dir;
    if (dir === "stop") {
      btn.addEventListener("click", () => ptzSend(id, { stop: true }));
      return;
    }
    const start = (e) => { e.preventDefault(); ptzSend(id, map[dir]); };
    const stop = () => ptzSend(id, { stop: true });
    btn.addEventListener("mousedown", start);
    btn.addEventListener("touchstart", start, { passive: false });
    btn.addEventListener("mouseup", stop);
    btn.addEventListener("mouseleave", stop);
    btn.addEventListener("touchend", stop);
  });
}

/** Setzt das Seitenverhaeltnis der Kachel. Sehr breite Kameras (z.B. der
 *  Reolink-Substream mit 1920x576) bekommen zwei Spalten, damit sie nicht als
 *  schmaler Streifen erscheinen. */
function applyAspect(card, ratio) {
  if (!ratio || !isFinite(ratio) || ratio <= 0) return;
  if (Math.abs((card.ratio || 0) - ratio) < 0.001) return;
  card.ratio = ratio;
  card.el.querySelector(".video-wrap").style.aspectRatio = String(ratio);
  card.el.classList.toggle("wide", ratio >= 2.2);
  card.el.classList.toggle("tall", ratio <= 0.9);
}

function buildCard(cam) {
  const node = cardTpl.content.cloneNode(true);
  const el = node.querySelector(".card");
  el.querySelector(".name").textContent = cam.name;
  const img = el.querySelector(".video");
  img.src = `/api/stream/${cam.id}`;
  img.onerror = () => { setTimeout(() => { img.src = `/api/stream/${cam.id}?t=` + Date.now(); }, 3000); };

  if (cam.ptz) {
    el.querySelector(".ptz").hidden = false;
    wirePtz(cam.id, el);
  }
  grid.appendChild(node);
  const card = {
    el,
    dot: el.querySelector(".dot"),
    badge: el.querySelector(".motion-badge"),
    res: el.querySelector(".res"),
    ratio: 0,
  };
  cards.set(cam.id, card);
  return card;
}

async function refreshStatus() {
  let list;
  try {
    list = await api("/api/cameras");
  } catch (e) {
    return; // naechster Tick versucht es erneut
  }
  const alive = new Set();
  for (const cam of list) {
    alive.add(cam.id);
    const c = cards.has(cam.id) ? cards.get(cam.id) : buildCard(cam);
    c.el.querySelector(".name").textContent = cam.name;
    c.dot.classList.toggle("online", cam.connected);
    c.dot.classList.toggle("offline", !cam.connected);
    c.res.textContent = cam.width ? `${cam.width}×${cam.height}` : "";
    applyAspect(c, cam.aspect_ratio);
    const recent = cam.last_motion && (Date.now() / 1000 - cam.last_motion) < 8;
    c.badge.classList.toggle("active", !!recent);
  }
  // Entfernte oder abgeschaltete Kameras aus dem Gitter nehmen.
  for (const [id, c] of cards) {
    if (!alive.has(id)) { c.el.remove(); cards.delete(id); }
  }
  document.body.classList.toggle("empty", list.length === 0);
}

// "Bild fuellen": zuschneiden statt Balken (pro Betrachter, wird gemerkt).
const fitToggle = document.getElementById("fitToggle");
fitToggle.checked = localStorage.getItem("nvrFill") === "1";
const applyFill = () => {
  document.body.classList.toggle("fill", fitToggle.checked);
  localStorage.setItem("nvrFill", fitToggle.checked ? "1" : "0");
};
fitToggle.addEventListener("change", applyFill);
applyFill();

// -- Verwaltung ---------------------------------------------------------------

const dlg = document.getElementById("manageDlg");
const form = document.getElementById("camForm");
const F = {
  name: document.getElementById("fName"),
  host: document.getElementById("fHost"),
  user: document.getElementById("fUser"),
  pass: document.getElementById("fPass"),
  port: document.getElementById("fPort"),
  aspect: document.getElementById("fAspect"),
  aspectCustom: document.getElementById("fAspectCustom"),
  main: document.getElementById("fMain"),
  sub: document.getElementById("fSub"),
  ptz: document.getElementById("fPtz"),
  motion: document.getElementById("fMotion"),
  enabled: document.getElementById("fEnabled"),
};

F.aspect.addEventListener("change", () => {
  const custom = F.aspect.value === "__custom";
  F.aspectCustom.hidden = !custom;
  F.aspectCustom.parentElement.hidden = !custom;
});

function setAspectField(value) {
  const v = value || "auto";
  const known = [...F.aspect.options].some((o) => o.value === v);
  F.aspect.value = known ? v : "__custom";
  F.aspectCustom.value = known ? "" : v;
  F.aspect.dispatchEvent(new Event("change"));
}

function currentAspect() {
  return F.aspect.value === "__custom" ? F.aspectCustom.value.trim() || "auto" : F.aspect.value;
}

async function renderList() {
  const box = document.getElementById("camList");
  try {
    const data = await api("/api/config/cameras");
    if (!data.cameras.length) {
      box.innerHTML = '<p class="hint">Noch keine Kamera eingerichtet. ' +
        'Lege sie unten einzeln an oder lass das Netz durchsuchen.</p>';
      return;
    }
    box.innerHTML = data.cameras.map((c) => `
      <div class="cam-row${c.enabled === false ? " off" : ""}">
        <div class="cam-row-main">
          <b>${esc(c.name)}</b> <span class="hint">${esc(c.host)}${c.onvif_port ? ":" + c.onvif_port : ""}</span><br>
          <span class="hint">Format: ${esc(c.aspect || "auto")}${c.ptz ? " · PTZ" : ""}${c.enabled === false ? " · abgeschaltet" : ""}</span>
        </div>
        <div class="cam-row-btns">
          <button data-edit="${esc(c.id)}">Bearbeiten</button>
          <button data-del="${esc(c.id)}" class="danger">Entfernen</button>
        </div>
      </div>`).join("");

    box.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", () => openForm(data.cameras.find((c) => c.id === b.dataset.edit))));
    box.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", async () => {
        const cam = data.cameras.find((c) => c.id === b.dataset.del);
        if (!confirm(`Kamera "${cam.name}" wirklich entfernen?`)) return;
        try {
          await api(`/api/config/cameras/${encodeURIComponent(cam.id)}`, { method: "DELETE" });
          await renderList();
          refreshStatus();
        } catch (e) { alert("Entfernen fehlgeschlagen: " + e.message); }
      }));
  } catch (e) {
    box.innerHTML = `<p class="hint">Liste nicht ladbar: ${esc(e.message)}</p>`;
  }
}

function openForm(cam) {
  editingId = cam ? cam.id : null;
  document.getElementById("formTitle").textContent = cam ? `Kamera "${cam.name}" bearbeiten` : "Neue Kamera";
  F.name.value = cam ? cam.name || "" : "";
  F.host.value = cam ? cam.host || "" : "";
  F.user.value = cam ? cam.username || "admin" : "admin";
  F.pass.value = "";
  F.pass.placeholder = cam && cam.has_password ? "(leer = unveraendert)" : "Passwort";
  F.port.value = cam ? cam.onvif_port || 80 : 80;
  F.main.value = cam ? cam.rtsp_main || "" : "";
  F.sub.value = cam ? cam.rtsp_sub || "" : "";
  F.ptz.checked = cam ? !!cam.ptz : false;
  F.motion.checked = cam ? !(cam.motion && cam.motion.enabled === false) : true;
  F.enabled.checked = cam ? cam.enabled !== false : true;
  setAspectField(cam ? cam.aspect : "auto");
  document.getElementById("formMsg").textContent = "";
  document.getElementById("probeMsg").textContent = "";
  form.hidden = false;
  F.name.focus();
}

document.getElementById("manageBtn").addEventListener("click", async () => {
  form.hidden = true;
  document.getElementById("scanResult").innerHTML = "";
  document.getElementById("yamlView").hidden = true;
  await renderList();
  dlg.showModal();
});

document.getElementById("addBtn").addEventListener("click", () => openForm(null));
document.getElementById("cancelBtn").addEventListener("click", () => { form.hidden = true; });

// RTSP-Pfade fuer GENAU DIESE Kamera per ONVIF holen.
document.getElementById("probeBtn").addEventListener("click", async () => {
  const msg = document.getElementById("probeMsg");
  if (!F.host.value.trim()) { msg.textContent = "Erst die IP-Adresse eintragen."; return; }
  msg.textContent = "Frage Kamera ab …";
  const q = new URLSearchParams({
    host: F.host.value.trim(),
    user: F.user.value.trim(),
    password: F.pass.value,
    onvif_port: F.port.value || 0,
  }).toString();
  try {
    const data = await api(`/api/probe?${q}`, { method: "POST" });
    const c = data.camera;
    F.main.value = c.rtsp_main || F.main.value;
    F.sub.value = c.rtsp_sub || F.sub.value;
    F.port.value = c.onvif_port || F.port.value;
    F.ptz.checked = !!c.ptz;
    if (!F.name.value.trim()) F.name.value = c.name || "";
    setAspectField(c.aspect || "auto");
    msg.textContent = `✅ ${c.manufacturer} ${c.model}`.trim() +
      (c.resolution ? ` · ${c.resolution} (${c.aspect})` : "");
  } catch (e) {
    msg.textContent = "⚠️ " + e.message;
  }
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = document.getElementById("formMsg");
  const payload = {
    name: F.name.value.trim(),
    host: F.host.value.trim(),
    username: F.user.value.trim() || "admin",
    password: F.pass.value,
    onvif_port: parseInt(F.port.value, 10) || 80,
    rtsp_main: F.main.value.trim(),
    rtsp_sub: F.sub.value.trim(),
    ptz: F.ptz.checked,
    enabled: F.enabled.checked,
    aspect: currentAspect(),
    motion: { enabled: F.motion.checked },
  };
  msg.textContent = "Speichere …";
  try {
    if (editingId) {
      await api(`/api/config/cameras/${encodeURIComponent(editingId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      await api("/api/config/cameras", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
    msg.textContent = "✅ Gespeichert.";
    form.hidden = true;
    await renderList();
    refreshStatus();
  } catch (err) {
    msg.textContent = "⚠️ " + err.message;
  }
});

// Netzsuche: findet mehrere Kameras, uebernommen wird ERGAENZEND.
document.getElementById("scanBtn").addEventListener("click", async () => {
  const out = document.getElementById("scanResult");
  const user = prompt("ONVIF-Benutzer fuer die Suche (leer = gaengige Werksdaten durchprobieren):", "admin");
  if (user === null) return;
  const pass = user ? prompt("Passwort fuer " + user + ":", "") : "";
  if (user && pass === null) return;

  out.innerHTML = '<p class="hint">⏳ Suche im Netz … (kann ~15 Sek. dauern)</p>';
  const q = new URLSearchParams({ user: user || "", password: pass || "" }).toString();
  try {
    const data = await api(`/api/autoconfig?${q}`, { method: "POST" });
    if (!data.count) {
      out.innerHTML = '<p class="hint">Keine Kamera erkannt. Zugangsdaten pruefen, ONVIF an der ' +
        'Kamera aktivieren, oder die Kamera unten von Hand anlegen.</p>';
      return;
    }
    out.innerHTML =
      `<p class="hint">${data.count} Kamera(s) gefunden — auswaehlen und uebernehmen:</p>` +
      data.cameras.map((c, i) => `
        <label class="found">
          <input type="checkbox" data-found="${i}" ${c.already_configured ? "" : "checked"} />
          <b>${esc(c.name)}</b> <code>${esc(c.host)}:${c.onvif_port}</code>
          ${c.resolution ? `· ${esc(c.resolution)} (${esc(c.aspect)})` : ""}
          ${c.ptz ? "· PTZ" : ""}
          ${c.already_configured ? '<span class="hint">· schon eingerichtet</span>' : ""}
        </label>`).join("") +
      '<div class="actions"><button id="takeOver" class="primary">Ausgewaehlte uebernehmen</button>' +
      '<span id="scanMsg" class="hint"></span></div>';

    document.getElementById("takeOver").addEventListener("click", async () => {
      const picked = [...out.querySelectorAll("[data-found]")]
        .filter((cb) => cb.checked)
        .map((cb) => data.cameras[Number(cb.dataset.found)]);
      const smsg = document.getElementById("scanMsg");
      if (!picked.length) { smsg.textContent = "Nichts ausgewaehlt."; return; }
      smsg.textContent = "Uebernehme …";
      try {
        const r = await api("/api/config/cameras/bulk", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cameras: picked }),
        });
        smsg.textContent = `✅ ${r.added.length} uebernommen, ${r.cameras} aktiv.`;
        await renderList();
        refreshStatus();
      } catch (e) { smsg.textContent = "⚠️ " + e.message; }
    });
  } catch (e) {
    out.innerHTML = `<p class="hint">Suche fehlgeschlagen: ${esc(e.message)}</p>`;
  }
});

document.getElementById("yamlBtn").addEventListener("click", async () => {
  const ta = document.getElementById("yamlView");
  if (!ta.hidden) { ta.hidden = true; return; }
  try {
    const data = await api("/api/config/yaml");
    ta.value = data.config_yaml;
    ta.hidden = false;
  } catch (e) { alert("Nicht ladbar: " + e.message); }
});

async function checkSetup() {
  try {
    const st = await api("/api/state");
    if (st.setup_mode || st.cameras === 0) {
      const banner = document.createElement("div");
      banner.className = "banner";
      banner.innerHTML = "👋 <b>Willkommen!</b> Noch keine Kameras eingerichtet. " +
        "Lege sie unter <b>Kameras verwalten</b> einzeln an — oder lass das Netz durchsuchen.";
      grid.before(banner);
      document.getElementById("manageBtn").click();
    }
  } catch (e) { /* ignorieren */ }
}

checkSetup();
refreshStatus();
setInterval(refreshStatus, 3000);
