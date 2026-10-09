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

/** Darstellung der Kachel: Seitenverhaeltnis, Breite und Bildanpassung.
 *  Breite: 0 = automatisch (sehr breite Bilder wie der Reolink-Substream mit
 *  1920x576 bekommen zwei Spalten), sonst die eingestellte Spaltenzahl. */
function applyLayout(card, cam) {
  const ratio = cam.aspect_ratio;
  const spalten = cam.columns ? cam.columns : (ratio >= 2.2 ? 2 : 1);
  const fill = cam.fill === "cover";
  const schluessel = `${ratio}|${spalten}|${fill}`;
  if (card.layout === schluessel) return;
  card.layout = schluessel;

  const wrap = card.el.querySelector(".video-wrap");
  if (ratio && isFinite(ratio) && ratio > 0) wrap.style.aspectRatio = String(ratio);
  // -1 = volle Zeilenbreite: nur so stehen zwei Kacheln garantiert
  // untereinander, unabhaengig von der Fensterbreite.
  card.el.style.gridColumn = spalten === -1 ? "1 / -1" : (spalten > 1 ? `span ${spalten}` : "");
  card.el.classList.toggle("fill", fill);
}

async function ladePositionen(id, el) {
  try {
    const d = await api(`/api/presets/${encodeURIComponent(id)}`);
    if (!d.presets.length) return;
    const box = el.querySelector(".presets");
    box.innerHTML = '<span class="hint">Positionen:</span>';
    d.presets.forEach((p) => {
      const b = document.createElement("button");
      b.textContent = p.name;
      b.title = "Kamera auf gespeicherte Position fahren";
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          const r = await api(`/api/preset/${encodeURIComponent(id)}?token=${encodeURIComponent(p.token)}`,
                              { method: "POST" });
          if (!r.ok) alert("Kamera hat die Fahrt abgelehnt.");
        } catch (e) { alert("Fehlgeschlagen: " + e.message); }
        setTimeout(() => { b.disabled = false; }, 1500);
      });
      box.appendChild(b);
    });
    box.hidden = false;
  } catch (e) { /* keine Positionen -> Leiste bleibt aus */ }
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
    // Nur die selbst benannten Positionen - die Werksplaetze ("Preset 12")
    // filtert die Schnittstelle heraus.
    ladePositionen(cam.id, el);
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
  list.forEach((cam, i) => {
    alive.add(cam.id);
    const c = cards.has(cam.id) ? cards.get(cam.id) : buildCard(cam);
    c.el.querySelector(".name").textContent = cam.name;
    c.dot.classList.toggle("online", cam.connected);
    c.dot.classList.toggle("offline", !cam.connected);
    c.res.textContent = cam.width ? `${cam.width}×${cam.height}` : "";
    // Reihenfolge kommt aus der Konfiguration - ohne das haengen neu
    // hinzugefuegte Kacheln immer hinten, egal wie sortiert wurde.
    c.el.style.order = String(i);
    applyLayout(c, cam);
    const recent = cam.last_motion && (Date.now() / 1000 - cam.last_motion) < 8;
    c.badge.classList.toggle("active", !!recent);
  });
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
  columns: document.getElementById("fColumns"),
  fill: document.getElementById("fFill"),
  onvifUser: document.getElementById("fOnvifUser"),
  onvifPass: document.getElementById("fOnvifPass"),
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
    const breite = (c) => (c.columns === -1 ? "volle Breite"
      : c.columns ? `${c.columns} Spalten` : "Breite automatisch");
    box.innerHTML = data.cameras.map((c, i) => `
      <div class="cam-row${c.enabled === false ? " off" : ""}">
        <div class="cam-row-sort">
          <button data-up="${esc(c.id)}" ${i === 0 ? "disabled" : ""} title="nach oben">&#9650;</button>
          <button data-down="${esc(c.id)}" ${i === data.cameras.length - 1 ? "disabled" : ""} title="nach unten">&#9660;</button>
        </div>
        <div class="cam-row-main">
          <b>${esc(c.name)}</b> <span class="hint">${esc(c.host)}${c.onvif_port ? ":" + c.onvif_port : ""}</span><br>
          <span class="hint">Format ${esc(c.aspect || "auto")} · ${breite(c)}${c.fill === "cover" ? " · zugeschnitten" : ""}${c.ptz ? " · PTZ" : ""}${c.enabled === false ? " · abgeschaltet" : ""}</span>
        </div>
        <div class="cam-row-btns">
          <button data-edit="${esc(c.id)}">Bearbeiten</button>
          <button data-del="${esc(c.id)}" class="danger">Entfernen</button>
        </div>
      </div>`).join("");

    const verschieben = async (id, richtung) => {
      const ids = data.cameras.map((c) => c.id);
      const i = ids.indexOf(id), j = i + richtung;
      if (i < 0 || j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      try {
        await api("/api/config/order", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids }),
        });
        await renderList();
        refreshStatus();
      } catch (e) { alert("Sortieren fehlgeschlagen: " + e.message); }
    };
    box.querySelectorAll("[data-up]").forEach((b) =>
      b.addEventListener("click", () => verschieben(b.dataset.up, -1)));
    box.querySelectorAll("[data-down]").forEach((b) =>
      b.addEventListener("click", () => verschieben(b.dataset.down, 1)));

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
  F.onvifUser.value = cam ? cam.onvif_user || "" : "";
  F.onvifPass.value = "";
  F.onvifPass.placeholder = cam && cam.has_onvif_password ? "(leer = unveraendert)" : "";
  F.columns.value = String(cam && cam.columns ? cam.columns : 0);
  F.fill.value = cam && cam.fill === "cover" ? "cover" : "contain";
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
    onvif_user: F.onvifUser.value.trim(),
    onvif_password: F.onvifPass.value,
    rtsp_main: F.main.value.trim(),
    rtsp_sub: F.sub.value.trim(),
    ptz: F.ptz.checked,
    enabled: F.enabled.checked,
    aspect: currentAspect(),
    columns: parseInt(F.columns.value, 10) || 0,
    fill: F.fill.value,
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
