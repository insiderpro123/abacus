let DATA = null;
let filter = "Active";
const openWPs = new Set();
const openPhases = new Set(); // key: wpId + ":" + phaseNum
let syncPoll = null;

/* --------------------------------------------------------------------- */
/* Session guard: single seat + 5-minute idle cutoff.                     */
/* If the server drops our session (idle too long, or someone else took   */
/* the seat), any API call returns 401 -> send the user back to login.    */
/* A client-side idle timer also returns to login after 5 minutes of no   */
/* interaction, so the screen reflects being cut off without a click.     */
/* --------------------------------------------------------------------- */
const IDLE_MS = 5 * 60 * 1000;
(function installSessionGuard() {
  const nativeFetch = window.fetch.bind(window);
  let bouncing = false;
  window.fetch = async (...args) => {
    const res = await nativeFetch(...args);
    if (res.status === 401 && !bouncing) {
      bouncing = true;
      window.location.href = "/login";
    }
    return res;
  };

  let idleTimer = null;
  const resetIdle = () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { window.location.href = "/logout"; }, IDLE_MS);
  };
  ["mousemove", "mousedown", "keydown", "scroll", "touchstart"].forEach((evt) =>
    window.addEventListener(evt, resetIdle, { passive: true })
  );
  resetIdle();
})();

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

// Turn bare URLs into links (used for the reference/assets fields)
const linkify = (s) =>
  esc(s).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
        .replace(/\n/g, "<br>");

async function load() {
  const status = $("#status");
  status.className = "status";
  status.textContent = "Loading…";
  try {
    const res = await fetch("/api/data");
    const json = await res.json();
    if (json.error) throw new Error(json.error);
    DATA = json;
    status.textContent = "";
    render();
    updateSyncBadge(json.pending, json.last_flush);
    if (json.pending > 0) startSyncPoll();
  } catch (e) {
    status.className = "status error";
    status.textContent = "Could not load data: " + e.message;
  }
}

function visibleWPs() {
  return DATA.work_packages.filter((w) =>
    filter === "all" ? true : (w.status || "").toLowerCase() === filter.toLowerCase()
  );
}

function render() {
  renderMatrix();
}

function renderMatrix() {
  const thead = $("#matrix thead");
  const tbody = $("#matrix tbody");
  thead.innerHTML = "";
  tbody.innerHTML = "";

  // header
  const htr = el("tr");
  htr.appendChild(el("th", "wp-col", ""));
  DATA.processes.forEach((p) => {
    const th = el("th", "proc-th");
    th.title = p.num + ". " + p.title;
    th.innerHTML = `<span class="pnum">${p.num}</span><span class="ptitle">${esc(p.title)}</span>`;
    htr.appendChild(th);
  });
  thead.appendChild(htr);

  const wps = visibleWPs();
  const nCols = DATA.processes.length + 1;
  if (!wps.length) {
    const tr = el("tr");
    const td = el("td", "empty");
    td.colSpan = nCols;
    td.textContent = "No work packages match this filter.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  // Group into sections by status (Active first, then Inactive, then the rest)
  const order = ["Active", "Inactive"];
  const groups = {};
  wps.forEach((w) => { (groups[w.status] = groups[w.status] || []).push(w); });
  const statuses = Object.keys(groups).sort(
    (a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99) || a.localeCompare(b)
  );

  let addBtnPlaced = false;
  statuses.forEach((status) => {
    const secTr = el("tr", "section-row");
    const secTd = el("td");
    secTd.colSpan = nCols;
    secTd.innerHTML = `<div class="section-head">${esc(status)}
      <span class="section-count">${groups[status].length}</span></div>`;
    secTr.appendChild(secTd);
    tbody.appendChild(secTr);

    groups[status].forEach((w) => {
      tbody.appendChild(renderWpRow(w));
      appendPanel(tbody, w);
    });

    // "+ Add project" button directly below the Active projects
    if (status === "Active") {
      tbody.appendChild(addProjectRow(nCols));
      addBtnPlaced = true;
    }
  });
  if (!addBtnPlaced) tbody.appendChild(addProjectRow(nCols));

  sizePanels();
}

function addProjectRow(nCols) {
  const tr = el("tr", "add-row");
  const td = el("td");
  td.colSpan = nCols;
  td.innerHTML = `<button class="add-project-btn">+ Add project</button>`;
  td.querySelector("button").addEventListener("click", openAddProject);
  tr.appendChild(td);
  return tr;
}

function appendPanel(tbody, w) {
  if (!openWPs.has(w.wp_id)) return;
  const ptr = el("tr", "panel-row");
  const td = el("td");
  td.colSpan = DATA.processes.length + 1;
  td.appendChild(buildPanel(w));
  ptr.appendChild(td);
  tbody.appendChild(ptr);
}

function renderWpRow(w) {
  const color = clientColor(w.client);
  const tr = el("tr", "wp-row" + (openWPs.has(w.wp_id) ? " open" : ""));
  tr.dataset.id = w.wp_id;
  tr.style.setProperty("--stripe", color);

  const nameTd = el("td", "wp-col");
  nameTd.innerHTML = `
    <div class="wp-cell">
      <span class="wp-caret">▶</span>
      <span class="wp-icon${w.icon ? " emoji" : ""}" style="background:${color}">${w.icon ? esc(w.icon) : esc(initials(w.client))}</span>
      <span class="wp-text">
        <span class="wp-client">${esc(w.client)}</span>
        <span class="wp-title">${esc(w.name)}</span>
      </span>
    </div>`;
  tr.appendChild(nameTd);

  w.phases.forEach((ph) => {
    const td = el("td");
    const pill = el("div", "cell-pill " + ph.status);
    pill.title = `${ph.num}. ${ph.title} - ${ph.pct}%`;
    if (ph.status !== "na") {
      pill.innerHTML = `<div class="fill ${ph.status}" style="width:${ph.pct}%"></div><span>${ph.pct}%</span>`;
    } else {
      pill.innerHTML = `<span>–</span>`;
    }
    td.appendChild(pill);
    tr.appendChild(td);
  });

  tr.addEventListener("click", () => toggleWP(w.wp_id));
  return tr;
}

// deterministic brand colour per client
const CLIENT_COLORS = [
  "#4caf7d", "#e0a92e", "#35b0a7", "#e2703a", "#c0432f",
  "#7b4fb0", "#4a76c4", "#c65b9b", "#5a9e3f", "#2f9e8f",
];
function clientColor(name) {
  let h = 0;
  for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return CLIENT_COLORS[h % CLIENT_COLORS.length];
}
function initials(name) {
  const parts = String(name).trim().split(/\s+/);
  return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || "•";
}

// Keep inline panels sized to the visible table area so they don't
// disappear when the matrix is scrolled horizontally.
function sizePanels() {
  const wrap = document.querySelector(".matrix-wrap");
  if (!wrap) return;
  document.querySelectorAll(".panel-row .panel").forEach((p) => {
    p.style.width = Math.max(wrap.clientWidth - 24, 320) + "px";
  });
}

function toggleWP(id) {
  if (openWPs.has(id)) openWPs.delete(id);
  else openWPs.add(id);
  render();
  if (openWPs.has(id)) {
    setTimeout(() => {
      const p = document.getElementById("panel-" + id);
      if (p) p.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 50);
  }
}

function buildPanel(w) {
  const panel = el("div", "panel");
  panel.id = "panel-" + w.wp_id;

  const head = el("div", "panel-head");
  const isActive = (w.status || "").toLowerCase() === "active";
  const locked = !isActive;               // inactive -> read-only (locked)
  if (locked) panel.classList.add("locked");
  const toggleLabel = isActive ? "Make inactive" : "Make active";
  const toggleTo = isActive ? "Inactive" : "Active";
  head.innerHTML = `<div class="panel-head-left">
        <h2>${esc(w.client)} - ${esc(w.name)}</h2>
        <span class="badge ${esc(w.status)}">${esc(w.status)}</span>
        <span class="panel-overall" title="Overall progress">${w.overall}% complete</span>
        ${locked ? '<span class="lock-note">🔒 Locked - make active to edit</span>' : ""}
        ${w.description ? `<div class="panel-desc">${esc(w.description)}</div>` : ""}
      </div>
      <span class="panel-head-actions">
        <button class="dup-wp" title="Create a copy of this project">Duplicate</button>
        ${locked ? "" : '<button class="edit-wp">Edit</button>'}
        <button class="status-toggle">${toggleLabel}</button>
        <span class="close" title="Collapse">✕</span>
      </span>`;
  head.querySelector(".dup-wp").addEventListener("click", (e) => {
    e.stopPropagation();
    duplicateProject(w);
  });
  if (!locked) {
    head.querySelector(".edit-wp").addEventListener("click", (e) => {
      e.stopPropagation();
      openProjectForm("edit", w);
    });
  }
  head.querySelector(".status-toggle").addEventListener("click", (e) => {
    e.stopPropagation();
    setStatus(w, toggleTo);
  });
  head.querySelector(".close").addEventListener("click", () => toggleWP(w.wp_id));
  panel.appendChild(head);

  const gantt = el("div", "gantt");
  const scroll = el("div", "gantt-scroll");  // only the wide timeline strip scrolls

  // Timeline overview strip (12 phases flowing left -> right)
  const tl = el("div", "timeline");
  w.phases.forEach((ph) => {
    const c = el("div", "tl-phase");
    c.innerHTML = `
      <div class="tl-fill ${ph.status}" style="height:${ph.pct}%"></div>
      <div class="tl-dot ${ph.status}"></div>
      <div class="tl-num">${ph.num}</div>
      <div class="tl-pct">${ph.status === "na" ? "–" : ph.pct + "%"}</div>
      <div class="tl-title">${esc(ph.title)}</div>`;
    c.title = `${ph.num}. ${ph.title} - ${ph.pct}%`;
    c.addEventListener("click", () => togglePhase(w.wp_id, ph.num));
    tl.appendChild(c);
  });
  scroll.appendChild(tl);
  gantt.appendChild(scroll);

  // Detailed segmented gantt rows (full width, no horizontal clipping)
  w.phases.forEach((ph) => {
    const key = w.wp_id + ":" + ph.num;
    const isOpen = openPhases.has(key);

    const row = el("div", "grow" + (isOpen ? " open" : ""));
    row.addEventListener("click", () => togglePhase(w.wp_id, ph.num));
    const label = el("div", "grow-label");
    label.innerHTML = `<span class="gnum">${ph.num}</span>
      <span class="gtitle">${esc(ph.title)}</span>
      <span class="grow-actions">
        ${locked ? "" : '<button class="phase-setall" title="Mark every point in this phase">Set all ▾</button>'}
        <span class="gcaret">▶</span>
      </span>`;
    if (!locked && ph.subs.length) {
      const setAll = label.querySelector(".phase-setall");
      setAll.addEventListener("click", (e) => {
        e.stopPropagation();
        openStatusMenu(setAll, (value) => bulkSet(w, ph, value));
      });
    }

    const track = el("div", "gtrack");
    // 'Not required' points are excluded from the bar (they don't count)
    const barSubs = ph.subs.filter((s) => s.status !== "nr");
    if (barSubs.length === 0) {
      track.appendChild(el("div", "seg na"));
    } else {
      barSubs.forEach((s) => {
        const seg = el("div", "seg " + s.status);
        seg.title = `${s.code} ${s.label} - ${statusLabel(s.status)}${locked ? "" : " (click to edit)"}`;
        if (!locked) {
          seg.addEventListener("click", (e) => {
            e.stopPropagation();
            openEditor(w, s);
          });
        }
        track.appendChild(seg);
      });
    }

    const pct = el("div", "gpct", ph.status === "na" ? "–" : ph.pct + "%");

    row.appendChild(label);
    row.appendChild(track);
    row.appendChild(pct);
    gantt.appendChild(row);

    // sub-process detail
    const subs = el("div", "subs");
    if (isOpen) subs.style.display = "block";
    ph.subs.forEach((s) => {
      const item = el("div", "sub-item " + s.status);
      item.innerHTML = `<span class="scode">${esc(s.code)}</span>
        <span class="sdot ${s.status}"></span>
        <span class="slabel">${esc(s.label) || "(no description)"}</span>
        ${locked ? "" : '<span class="edit-hint">✎ edit</span>'}`;
      if (!locked) item.addEventListener("click", () => openEditor(w, s));
      subs.appendChild(item);
    });
    if (!ph.subs.length) subs.appendChild(el("div", "sub-item na", `<span class="slabel">No sub-processes defined.</span>`));
    gantt.appendChild(subs);
  });

  panel.appendChild(gantt);
  return panel;
}

function togglePhase(wpId, num) {
  const key = wpId + ":" + num;
  if (openPhases.has(key)) openPhases.delete(key);
  else openPhases.add(key);
  render();
}

function statusLabel(s) {
  return { done: "Complete", progress: "In progress", todo: "Outstanding", na: "Not started", nr: "Not required" }[s] || s;
}

/* ------------------------------------------------------------------ */
/* "Set all" quick menu for a whole phase                              */
/* ------------------------------------------------------------------ */
function openStatusMenu(anchor, onPick) {
  closeMenu();
  const menu = el("div", "mini-menu");
  menu.id = "mini-menu";
  menu.innerHTML = `<div class="mini-head">Mark all points as…</div>`;
  STATUS_OPTIONS.forEach((o) => {
    const b = el("button", "mini-opt");
    b.innerHTML = `<span class="sdot ${o.cls}"></span>${o.label}`;
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      closeMenu();
      onPick(o.value);
    });
    menu.appendChild(b);
  });
  document.body.appendChild(menu);

  const r = anchor.getBoundingClientRect();
  menu.style.top = window.scrollY + r.bottom + 4 + "px";
  menu.style.left = window.scrollX + r.left + "px";
  const mr = menu.getBoundingClientRect();
  if (mr.right > window.innerWidth - 8) {
    menu.style.left = window.scrollX + window.innerWidth - mr.width - 8 + "px";
  }
  setTimeout(() => document.addEventListener("click", closeMenu), 0);
}

function closeMenu() {
  const m = document.getElementById("mini-menu");
  if (m) m.remove();
  document.removeEventListener("click", closeMenu);
}

async function bulkSet(w, ph, value) {
  // 'Not required' points are left untouched by a bulk set
  const codes = ph.subs.filter((s) => s.status !== "nr").map((s) => s.code);
  if (!codes.length) return;
  try {
    const res = await fetch("/api/update_bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wp_id: w.wp_id, codes, value }),
    });
    const json = await res.json();
    if (json.error) throw new Error(json.error);
    await load();
    startSyncPoll();
    maybePromptComplete(w.wp_id);
  } catch (e) {
    alert("Could not update: " + e.message);
  }
}

async function setStatus(w, status) {
  try {
    const res = await fetch("/api/set_status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wp_id: w.wp_id, status }),
    });
    const json = await res.json();
    if (json.error) throw new Error(json.error);
    await load();
    startSyncPoll();
  } catch (e) {
    alert("Could not change status: " + e.message);
  }
}

async function duplicateProject(w) {
  if (!confirm(`Create a copy of "${w.client} - ${w.name}"?\n\nThe copy starts fresh (progress not copied) so you can adjust it.`)) return;
  try {
    const res = await fetch("/api/duplicate_project", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wp_id: w.wp_id }),
    });
    const json = await res.json();
    if (json.error) throw new Error(json.error);
    filter = "Active";
    document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c.dataset.filter === "Active"));
    if (json.wp_id) openWPs.add(String(json.wp_id));
    await load();
    startSyncPoll();
    // open the copy's edit form so client/name/year can be tweaked straight away
    const nw = (DATA.work_packages || []).find((x) => x.wp_id === String(json.wp_id));
    if (nw) openProjectForm("edit", nw);
  } catch (e) {
    alert("Could not duplicate: " + e.message);
  }
}

// When a project hits 100%, offer to mark it inactive.
async function maybePromptComplete(wpId) {
  const w = (DATA?.work_packages || []).find((x) => x.wp_id === String(wpId));
  if (w && w.overall >= 100 && (w.status || "").toLowerCase() === "active") {
    if (confirm(`"${w.client} - ${w.name}" is now 100% complete.\n\nMark it inactive?`)) {
      await setStatus(w, "Inactive");
    }
  }
}

/* ------------------------------------------------------------------ */
/* Add / edit project dialog                                           */
/* ------------------------------------------------------------------ */
const EMOJI_CHOICES = ["🏥","💊","🧪","🦷","🩺","🍽️","🧹","🚚","🏢","💷","⚙️","📊","🔬","📦","🛒","💡","🧾","🖥️","🚑","🏷️","🏭","🏦","🏪","🏨","🏗️","🏛️","✈️","🚗","🚢","🛢️","⚡","🌱","🍺","☕","👕","👟","💄","📱","🔌","🧴","🧼","🧬","🎓","🎬","⚖️","🔐","👥","📈","💎","🧭"];

function openAddProject() { openProjectForm("add"); }

function openProjectForm(mode, wp) {
  closeEditor();
  const isEdit = mode === "edit";
  // sub-point structure is shared by all WPs; borrow it from the given wp or the first one
  const template = ((isEdit && wp) ? wp : DATA.work_packages[0] || {}).phases || [];
  const curNr = new Set();
  if (isEdit && wp) wp.phases.forEach((ph) => ph.subs.forEach((s) => { if (s.status === "nr") curNr.add(s.code); }));
  const curIcon = isEdit && wp ? (wp.icon || "") : "";

  const overlay = el("div", "modal-overlay");
  overlay.id = "editor";
  overlay.addEventListener("click", (e) => { if (e.target === overlay) closeEditor(); });

  const groupsHtml = template.map((ph) => `
    <div class="nr-group">
      <div class="nr-group-head">
        <span class="gnum">${ph.num}</span> ${esc(ph.title)}
        <button type="button" class="nr-all">all</button>
      </div>
      <div class="nr-subs">
        ${ph.subs.map((s) => `
          <label class="nr-item${curNr.has(s.code) ? " nr-off" : ""}">
            <input type="checkbox" data-code="${s.code}" ${curNr.has(s.code) ? "" : "checked"} />
            <span class="scode">${esc(s.code)}</span> ${esc(s.label)}
          </label>`).join("")}
      </div>
    </div>`).join("");

  const emojiBtns = EMOJI_CHOICES.map((e) =>
    `<button type="button" class="emoji-btn${e === curIcon ? " sel" : ""}" data-emoji="${e}">${e}</button>`
  ).join("");

  const modal = el("div", "modal");
  modal.innerHTML = `
    <div class="modal-head">
      <div>
        <div class="modal-code">${isEdit ? "Edit work package" : "New work package"}</div>
        <h3>${isEdit ? "Edit project" : "Add project"}</h3>
      </div>
      <span class="close" title="Close">✕</span>
    </div>
    <div class="modal-body">
      <div class="field-label">Client</div>
      <input class="text-input" id="ap-client" placeholder="e.g. Bridgepoint" autocomplete="off"
        value="${isEdit ? esc(wp.client) : ""}" />
      <div class="field-label">Project name <span class="field-hint">short title, max 40 characters</span></div>
      <input class="text-input" id="ap-name" placeholder="e.g. Cost Base Review" autocomplete="off" maxlength="40"
        value="${isEdit ? esc(wp.name) : ""}" />
      <div class="field-label">Description
        <span class="field-hint">optional · max 60 words (<span id="ap-desc-count">0</span>/60)</span>
      </div>
      <textarea class="text-input" id="ap-desc" rows="2"
        placeholder="A bit more detail about this piece of work…">${isEdit ? esc(wp.description || "") : ""}</textarea>
      <div class="field-label">Icon <span class="field-hint">required · pick an emoji (a picture, not text)</span></div>
      <div class="emoji-row">
        <input class="emoji-input" id="ap-emoji" maxlength="8" placeholder="🙂" value="${esc(curIcon)}" />
        ${emojiBtns}
      </div>
      <div class="field-label">Which points apply?
        <span class="field-hint">leave ticked = required · <b>untick</b> anything that is Not required</span>
      </div>
      <div class="nr-picker">${groupsHtml}</div>
    </div>
    <div class="modal-foot">
      <span class="modal-note">${isEdit ? "Changes are saved and written to Excel." : "Added as an <b>Active</b> project and written to Excel."}</span>
      <div>
        <button class="btn" id="ap-cancel">Cancel</button>
        <button class="btn btn-primary" id="ap-save">${isEdit ? "Save changes" : "Add project"}</button>
      </div>
    </div>`;

  const emojiInput = modal.querySelector("#ap-emoji");
  const syncEmojiButtons = () => {
    modal.querySelectorAll(".emoji-btn").forEach((b) =>
      b.classList.toggle("sel", b.dataset.emoji === emojiInput.value.trim()));
  };
  modal.querySelectorAll(".emoji-btn").forEach((b) => {
    b.addEventListener("click", () => {
      emojiInput.value = emojiInput.value.trim() === b.dataset.emoji ? "" : b.dataset.emoji;
      syncEmojiButtons();
    });
  });
  emojiInput.addEventListener("input", syncEmojiButtons);

  // live word counter for the description (cap 35 words)
  const descEl = modal.querySelector("#ap-desc");
  const descCount = modal.querySelector("#ap-desc-count");
  const wordCount = (t) => (t.trim() ? t.trim().split(/\s+/).length : 0);
  const syncDesc = () => {
    const n = wordCount(descEl.value);
    descCount.textContent = n;
    descCount.parentElement.classList.toggle("over", n > 60);
  };
  descEl.addEventListener("input", syncDesc);
  syncDesc();

  // ticked = required, unticked = Not required. Show a struck-through cue when off.
  const syncItem = (cb) => cb.closest(".nr-item").classList.toggle("nr-off", !cb.checked);
  modal.querySelectorAll('.nr-item input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener("change", () => syncItem(cb));
  });

  // per-phase toggle: flip all points in the phase (all required <-> all Not required)
  modal.querySelectorAll(".nr-all").forEach((b) => {
    b.addEventListener("click", () => {
      const boxes = b.closest(".nr-group").querySelectorAll('input[type="checkbox"]');
      const anyOff = [...boxes].some((x) => !x.checked);
      boxes.forEach((x) => { x.checked = anyOff; syncItem(x); });
    });
  });

  modal.querySelector(".close").addEventListener("click", closeEditor);
  modal.querySelector("#ap-cancel").addEventListener("click", closeEditor);
  modal.querySelector("#ap-save").addEventListener("click", async () => {
    const client = modal.querySelector("#ap-client").value.trim();
    const name = modal.querySelector("#ap-name").value.trim();
    const description = descEl.value.trim();
    const icon = emojiInput.value.trim();
    // validation
    if (!client || !name) { alert("Please enter both a client and a project name."); return; }
    if (!icon) { alert("Please choose an emoji icon."); return; }
    if (/^[\x00-\x7F]+$/.test(icon)) { alert("The icon must be an emoji (a picture), not text."); return; }
    if (wordCount(description) > 60) { alert("The description is too long — please keep it to 60 words or fewer."); return; }
    // unticked = Not required
    const nr_codes = [...modal.querySelectorAll('input[type="checkbox"]:not(:checked)')].map((x) => x.dataset.code);
    const btn = modal.querySelector("#ap-save");
    btn.disabled = true;
    btn.textContent = isEdit ? "Saving…" : "Adding…";
    try {
      const url = isEdit ? "/api/edit_project" : "/api/add_project";
      const payload = isEdit
        ? { wp_id: wp.wp_id, client, name, description, icon, nr_codes }
        : { client, name, description, icon, nr_codes };
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const json = await res.json();
      if (json.error) throw new Error(json.error);
      closeEditor();
      if (!isEdit) {
        filter = "Active";
        document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c.dataset.filter === "Active"));
        if (json.wp_id) openWPs.add(String(json.wp_id));
      }
      await load();
      startSyncPoll();
    } catch (e) {
      btn.disabled = false;
      btn.textContent = isEdit ? "Save changes" : "Add project";
      alert("Could not save: " + e.message);
    }
  });

  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  modal.querySelector("#ap-client").focus();
}

/* ------------------------------------------------------------------ */
/* Edit dialog                                                         */
/* ------------------------------------------------------------------ */
const STATUS_OPTIONS = [
  { value: "3", cls: "done", label: "Complete" },
  { value: "2", cls: "progress", label: "In progress" },
  { value: "1", cls: "todo", label: "Outstanding" },
  { value: "", cls: "na", label: "Not started" },
  { value: "N/R", cls: "nr", label: "Not required" },
];
const STATUS_TO_VALUE = { done: "3", progress: "2", todo: "1", na: "", nr: "N/R" };

const GUIDANCE_FIELDS = [
  { key: "operational", label: "Operational relevant" },
  { key: "top_level", label: "Abacus Top level" },
  { key: "assets", label: "Existing Assets" },
  { key: "comment", label: "Comment" },
];

function openEditor(w, s) {
  closeEditor();
  const ref = (DATA.reference || {})[s.code] || {};
  const current = STATUS_TO_VALUE[s.status] ?? "";

  const overlay = el("div", "modal-overlay");
  overlay.id = "editor";
  overlay.addEventListener("click", (e) => { if (e.target === overlay) closeEditor(); });

  const fieldRow = (f) => `
    <div class="ref-row">
      <label class="ref-label" for="g-${f.key}">${esc(f.label)}</label>
      <textarea class="ref-input" id="g-${f.key}" data-field="${f.key}"
        rows="2" placeholder="-">${esc(ref[f.key] || "")}</textarea>
    </div>`;

  const modal = el("div", "modal");
  modal.innerHTML = `
    <div class="modal-head">
      <div>
        <div class="modal-code">${esc(s.code)} · ${esc(w.client)} - ${esc(w.name)}</div>
        <h3>${esc(s.label)}</h3>
      </div>
      <span class="close" title="Close">✕</span>
    </div>
    <div class="modal-body">
      <div class="field-label">Status</div>
      <div class="status-picker">
        ${STATUS_OPTIONS.map(o => `
          <button class="status-opt ${o.cls} ${o.value === current ? "selected" : ""}" data-value="${o.value}">
            <span class="sdot ${o.cls}"></span>${o.label}
          </button>`).join("")}
      </div>
      <div class="field-label">Guidance for this point
        <span class="field-hint">shared across all work packages · editable</span>
      </div>
      <div class="ref-block">
        ${GUIDANCE_FIELDS.map(fieldRow).join("")}
      </div>
    </div>
    <div class="modal-foot">
      <span class="modal-note">Saved to the dashboard instantly, then written back to Excel.</span>
      <div>
        <button class="btn" id="modal-cancel">Cancel</button>
        <button class="btn btn-primary" id="modal-save">Save</button>
      </div>
    </div>`;

  let chosen = current;
  modal.querySelectorAll(".status-opt").forEach((b) => {
    b.addEventListener("click", () => {
      modal.querySelectorAll(".status-opt").forEach((x) => x.classList.remove("selected"));
      b.classList.add("selected");
      chosen = b.dataset.value;
    });
  });
  // auto-grow textareas
  modal.querySelectorAll(".ref-input").forEach((t) => {
    const grow = () => { t.style.height = "auto"; t.style.height = t.scrollHeight + "px"; };
    t.addEventListener("input", grow);
    setTimeout(grow, 0);
  });

  modal.querySelector(".close").addEventListener("click", closeEditor);
  modal.querySelector("#modal-cancel").addEventListener("click", closeEditor);
  modal.querySelector("#modal-save").addEventListener("click", async () => {
    const btn = modal.querySelector("#modal-save");
    btn.disabled = true;
    btn.textContent = "Saving…";
    try {
      const gfields = {};
      modal.querySelectorAll(".ref-input").forEach((t) => { gfields[t.dataset.field] = t.value; });

      const calls = [];
      if (chosen !== current) {
        calls.push(fetch("/api/update", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ wp_id: w.wp_id, code: s.code, value: chosen }),
        }));
      }
      calls.push(fetch("/api/update_guidance", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: s.code, fields: gfields }),
      }));

      const results = await Promise.all(calls);
      for (const r of results) {
        const j = await r.json();
        if (j.error) throw new Error(j.error);
      }
      closeEditor();
      await load();
      startSyncPoll();
      maybePromptComplete(w.wp_id);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "Save";
      alert("Could not save: " + e.message);
    }
  });

  overlay.appendChild(modal);
  document.body.appendChild(overlay);
}

function closeEditor() {
  const ex = document.getElementById("editor");
  if (ex) ex.remove();
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeEditor(); });

/* ------------------------------------------------------------------ */
/* Excel sync indicator                                                */
/* ------------------------------------------------------------------ */
function updateSyncBadge(pending, lastFlush) {
  const badge = $("#sync");
  if (!badge) return;
  if (pending > 0) {
    badge.className = "sync pending";
    badge.textContent = `⏳ ${pending} change${pending > 1 ? "s" : ""} saving…`;
  } else if (lastFlush && lastFlush.ok === false) {
    badge.className = "sync error";
    badge.textContent = "⚠ " + lastFlush.message;
  } else if (lastFlush && lastFlush.ok) {
    badge.className = "sync ok";
    badge.textContent = `✓ Saved ${lastFlush.when}`;
  } else {
    badge.className = "sync";
    badge.textContent = "";
  }
}

function startSyncPoll() {
  if (syncPoll) clearInterval(syncPoll);
  syncPoll = setInterval(async () => {
    try {
      const res = await fetch("/api/sync_status");
      const json = await res.json();
      updateSyncBadge(json.pending, json.last_flush);
      if (json.pending === 0) {
        clearInterval(syncPoll);
        syncPoll = null;
      }
    } catch { /* transient */ }
  }, 3000);
}

/* ------------------------------------------------------------------ */
// filter chips
$("#filters").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
  chip.classList.add("active");
  filter = chip.dataset.filter;
  render();
});

window.addEventListener("resize", sizePanels);

load();
