"use strict";

const grid = document.getElementById("grid");
const cardTpl = document.getElementById("cardTpl");
const cards = new Map(); // id -> {el, dot, badge}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(res.status + " " + res.statusText);
  return res.json();
}

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

function buildCard(cam) {
  const node = cardTpl.content.cloneNode(true);
  const el = node.querySelector(".card");
  el.querySelector(".name").textContent = cam.name;
  const img = el.querySelector(".video");
  img.src = `/api/stream/${cam.id}`;
  img.onerror = () => { setTimeout(() => { img.src = `/api/stream/${cam.id}?t=` + Date.now(); }, 3000); };

  if (cam.ptz) {
    const ptz = el.querySelector(".ptz");
    ptz.hidden = false;
    wirePtz(cam.id, el);
  }
  grid.appendChild(node);
  cards.set(cam.id, {
    el,
    dot: el.querySelector(".dot"),
    badge: el.querySelector(".motion-badge"),
  });
}

async function refreshStatus() {
  try {
    const list = await api("/api/cameras");
    for (const cam of list) {
      if (!cards.has(cam.id)) buildCard(cam);
      const c = cards.get(cam.id);
      c.dot.classList.toggle("online", cam.connected);
      c.dot.classList.toggle("offline", !cam.connected);
      const recent = cam.last_motion && (Date.now() / 1000 - cam.last_motion) < 8;
      c.badge.classList.toggle("active", !!recent);
    }
  } catch (e) {
    /* Netzwerkfehler ignorieren, naechster Tick versucht es erneut */
  }
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

document.getElementById("discoverBtn").addEventListener("click", () => {
  document.getElementById("discoverResult").innerHTML = "";
  document.getElementById("discoverDlg").showModal();
});

document.getElementById("acRun").addEventListener("click", async () => {
  const out = document.getElementById("discoverResult");
  const user = document.getElementById("acUser").value.trim();
  const pass = document.getElementById("acPass").value;
  const host = document.getElementById("acHost").value.trim();
  out.innerHTML = "⏳ Suche &amp; frage Kameras ab … (kann ~15 Sek. dauern)";
  const q = new URLSearchParams({ user, password: pass, host }).toString();
  try {
    const data = await api(`/api/autoconfig?${q}`, { method: "POST" });
    if (!data.count) {
      out.innerHTML =
        "Keine Kamera erkannt. Prüfe Zugangsdaten und ob ONVIF an der Kamera aktiviert ist. " +
        "Bei blockiertem Multicast eine konkrete IP eintragen.";
      return;
    }
    const list = data.cameras
      .map(
        (c) =>
          `<div class="found">&#128247; <b>${esc(c.manufacturer)} ${esc(c.model)}</b> ` +
          `(<code>${esc(c.host)}:${c.onvif_port}</code>)${c.ptz ? " · PTZ" : ""}<br>` +
          `<small>main:</small> <code>${esc(c.rtsp_main || "—")}</code></div>`
      )
      .join("");
    out.innerHTML =
      `<div>${data.count} Kamera(s) erkannt:</div>${list}` +
      `<p class="hint">Fertige Konfiguration — in <code>config/config.yaml</code> speichern und neu starten:</p>` +
      `<textarea class="yaml" readonly>${esc(data.config_yaml)}</textarea>` +
      `<button id="copyYaml">Config kopieren</button>`;
    const btn = document.getElementById("copyYaml");
    btn.addEventListener("click", () => {
      navigator.clipboard.writeText(data.config_yaml).then(() => (btn.textContent = "Kopiert ✓"));
    });
  } catch (e) {
    out.textContent = "Fehlgeschlagen: " + e.message;
  }
});

refreshStatus();
setInterval(refreshStatus, 3000);
