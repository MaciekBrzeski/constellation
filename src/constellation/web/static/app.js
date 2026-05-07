const $ = (id) => document.getElementById(id);

let lastBeatTs = 0;
let beatTimer = null;

function setConnected(ok) {
  const el = $("conn");
  el.textContent = ok ? "connected" : "disconnected";
  el.classList.toggle("online", ok);
  el.classList.toggle("offline", !ok);
}

function setTier(tier) {
  const el = $("tier");
  el.textContent = tier;
  el.classList.remove("tier-low", "tier-medium", "tier-high");
  el.classList.add("tier-" + tier);
}

function flashBeat() {
  const el = $("beat");
  el.classList.add("active");
  if (beatTimer) clearTimeout(beatTimer);
  beatTimer = setTimeout(() => el.classList.remove("active"), 120);
}

function renderMaster(m) {
  setTier(m.tier);
  $("bpm").textContent = m.bpm > 0 ? m.bpm.toFixed(1) : "—";
  $("strength").textContent = m.last_beat_strength.toFixed(2);
  $("floor").textContent = m.noise_floor.toFixed(4);
  const aubio = $("aubio");
  aubio.textContent = m.aubio_active ? "active" : "off";
  aubio.classList.toggle("online", m.aubio_active);
  aubio.classList.toggle("offline", !m.aubio_active);

  if (m.last_beat_audio_ms !== lastBeatTs) {
    lastBeatTs = m.last_beat_audio_ms;
    if (lastBeatTs > 0) flashBeat();
  }
}

function renderWorkers(workers) {
  const list = $("worker-list");
  if (!workers.length) {
    list.innerHTML = '<p class="empty">waiting for heartbeats…</p>';
    return;
  }

  // Stable order by worker_id
  workers.sort((a, b) => a.worker_id.localeCompare(b.worker_id));

  list.innerHTML = "";
  for (const w of workers) {
    const card = document.createElement("div");
    card.className = "worker";

    const head = document.createElement("div");
    head.className = "head";
    head.innerHTML = `
      <div>
        <span class="name">${escapeHtml(w.worker_id)}</span>
        <span class="ip">${escapeHtml(w.ip)}</span>
      </div>
      <span class="badge ${w.online ? "online" : "offline"}">
        ${w.online ? "online" : "offline"}
      </span>`;
    card.appendChild(head);

    const meta = document.createElement("div");
    meta.style.cssText = "color:var(--muted);font-size:12px;margin-bottom:10px;";
    meta.textContent = `tier=${w.tier} bpm=${w.bpm.toFixed(0)} (last seen ${w.last_seen_ago_s.toFixed(1)} s ago)`;
    card.appendChild(meta);

    const controls = document.createElement("div");
    controls.className = "controls";

    const animalSel = makeSelect(w.available_animals, w.animal);
    animalSel.addEventListener("change", () => {
      sendCmd(`/api/worker/${encodeURIComponent(w.worker_id)}/animal`, animalSel.value);
    });
    controls.appendChild(wrapField("animal", animalSel));

    const actionsForCurrent = w.available_actions_by_animal[w.animal] || [];
    const actionSel = makeSelect(actionsForCurrent, w.action);
    actionSel.addEventListener("change", () => {
      sendCmd(`/api/worker/${encodeURIComponent(w.worker_id)}/action`, actionSel.value);
    });
    controls.appendChild(wrapField("action", actionSel));

    card.appendChild(controls);
    list.appendChild(card);
  }
}

function wrapField(label, input) {
  const div = document.createElement("div");
  const lab = document.createElement("label");
  lab.textContent = label;
  div.appendChild(lab);
  div.appendChild(input);
  return div;
}

function makeSelect(options, current) {
  const sel = document.createElement("select");
  for (const o of options) {
    const opt = document.createElement("option");
    opt.value = o;
    opt.textContent = o;
    if (o === current) opt.selected = true;
    sel.appendChild(opt);
  }
  if (!options.length) sel.disabled = true;
  return sel;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

async function sendCmd(url, value) {
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
  } catch (e) {
    console.warn("cmd failed", e);
  }
}

// Sensitivity slider — debounced
const sens = $("sensitivity");
const sensVal = $("sens-val");
let sensTimer = null;
sens.addEventListener("input", () => {
  sensVal.textContent = parseFloat(sens.value).toFixed(2);
  if (sensTimer) clearTimeout(sensTimer);
  sensTimer = setTimeout(() => {
    sendCmd("/api/master/sensitivity", parseFloat(sens.value));
  }, 100);
});

// WebSocket loop with auto-reconnect.
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => setConnected(true);
  ws.onclose = () => {
    setConnected(false);
    setTimeout(connect, 1000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    renderMaster(data.master);
    renderWorkers(data.workers);
    // Sync slider once at first message; user input takes over after that.
    if (sensVal.dataset.synced !== "1") {
      sens.value = data.master.sensitivity;
      sensVal.textContent = data.master.sensitivity.toFixed(2);
      sensVal.dataset.synced = "1";
    }
  };
}

connect();
