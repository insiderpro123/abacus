let DATA = null;
let filter = "Active";
let actionsOnly = false;   // "Actions only" mode: open WPs showing just the task boards
let meetingNotes = false;  // "Meeting notes" mode: open WPs showing their Jamie notes
const openWPs = new Set();
const openPhases = new Set(); // key: wpId + ":" + phaseNum
const collapsedSecs = new Set(); // collapsed panel sections, key: wpId + ":tasks" | ":steps"
let syncPoll = null;

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
  const stacked = actionsOnly || meetingNotes;
  document.getElementById("matrix").classList.toggle("stacked", stacked);

  // header - the 12-step process columns (hidden in the stacked modes)
  if (!stacked) {
    const htr = el("tr");
    htr.appendChild(el("th", "wp-col", ""));
    DATA.processes.forEach((p) => {
      const th = el("th", "proc-th");
      th.title = p.num + ". " + p.title;
      th.innerHTML = `<span class="pnum">${p.num}</span><span class="ptitle">${esc(p.title)}</span>`;
      htr.appendChild(th);
    });
    thead.appendChild(htr);
  }

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
      if (!stacked) tbody.appendChild(renderWpRow(w));   // no 12-step row in stacked modes
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
        ${w.jira_total > 0 ? `<span class="jira-badge" title="Story points from Jira${w.jira_synced_at ? " · synced " + esc(w.jira_synced_at) : ""}">Jira ${w.jira_done}/${w.jira_total} pts</span>` : ""}
        ${w.task_total > 0 ? `<span class="task-badge${w.task_done === w.task_total ? " all-done" : ""}" title="Task board">✓ ${w.task_done}/${w.task_total} tasks</span>` : ""}
      </span>
    </div>`;
  tr.appendChild(nameTd);

  w.phases.forEach((ph) => {
    const td = el("td");
    td.appendChild(phasePill(ph));
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
    p.style.width = (actionsOnly || meetingNotes) ? "" : Math.max(wrap.clientWidth - 24, 320) + "px";
  });
}

function toggleWP(id) {
  if (openWPs.has(id)) {
    openWPs.delete(id);
  } else {
    openWPs.add(id);
    // start with the three sections collapsed each time a work package is opened
    ["steps", "subwps", "tasks"].forEach((k) => collapsedSecs.add(id + ":" + k));
  }
  render();
  if (openWPs.has(id)) {
    setTimeout(() => {
      const p = document.getElementById("panel-" + id);
      if (p) p.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 50);
  }
}

// Wrap content in a collapsible section with a clickable header (caret + title).
// Collapse state is remembered per work package + section so it survives re-renders.
function collapsibleSection(wpId, secKey, title, bodyEl) {
  const key = wpId + ":" + secKey;
  const wrap = el("div", "psec");
  wrap.dataset.seckey = key;
  if (collapsedSecs.has(key)) wrap.classList.add("collapsed");
  const head = el("div", "psec-head");
  head.innerHTML = `<span class="psec-caret">▸</span><span class="psec-title">${esc(title)}</span>`;
  head.addEventListener("click", (e) => {
    e.stopPropagation();
    const nowCollapsed = wrap.classList.toggle("collapsed");
    if (nowCollapsed) collapsedSecs.add(key); else collapsedSecs.delete(key);
  });
  const body = el("div", "psec-body");
  body.appendChild(bodyEl);
  wrap.appendChild(head);
  wrap.appendChild(body);
  return wrap;
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
        ${isActive ? "" : `<span class="badge ${esc(w.status)}">${esc(w.status)}</span>`}
        <span class="panel-overall" title="Overall progress">${w.overall}% complete</span>
        <span class="panel-links">
          <button class="linkbtn disabled" disabled title="Coming soon - not linked yet">📁 Dropbox files</button>
          <button class="linkbtn disabled" disabled title="Coming soon - not linked yet">📄 Confluence data</button>
        </span>
        ${w.jira_total > 0 ? `<span class="jira-badge" title="Story points from Jira epic ${esc(w.jira_project_key)}${w.jira_synced_at ? " · synced " + esc(w.jira_synced_at) : ""}">Jira ${w.jira_done}/${w.jira_total} pts</span>` : ""}
        ${locked ? '<span class="lock-note">🔒 Locked - make active to edit</span>' : ""}
        ${w.description ? `<div class="panel-desc">${esc(w.description)}</div>` : ""}
      </div>
      <span class="panel-head-actions">
        <button class="dup-wp" title="Create a copy of this project">Duplicate</button>
        ${locked ? "" : '<button class="edit-wp">Edit</button>'}
        <button class="status-toggle">${toggleLabel}</button>
        ${locked ? '<button class="delete-wp" title="Permanently delete this project">Delete</button>' : ""}
        <span class="close" title="Collapse">✕</span>
      </span>`;
  head.querySelector(".dup-wp").addEventListener("click", (e) => {
    e.stopPropagation();
    duplicateProject(w);
  });
  if (locked) {
    head.querySelector(".delete-wp").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteProject(w);
    });
  }
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
  head.querySelector(".close").addEventListener("click", () => {
    if (actionsOnly || meetingNotes) {
      // collapse the section in place instead of removing the (rowless) package
      const sec = panel.querySelector(".psec");
      if (sec) {
        const collapsed = sec.classList.toggle("collapsed");
        const key = sec.dataset.seckey;
        if (key) { if (collapsed) collapsedSecs.add(key); else collapsedSecs.delete(key); }
      }
    } else {
      toggleWP(w.wp_id);
    }
  });
  panel.appendChild(head);

  if (actionsOnly || meetingNotes) {
    // Stacked modes: one collapsible section (task board OR meeting notes). There
    // is no matrix row to reopen from, so ✕ collapses in place rather than vanish.
    const body = meetingNotes ? buildMeetingNotes(w) : buildTaskBoard(w);
    panel.appendChild(collapsibleSection(
      w.wp_id, meetingNotes ? "notes" : "tasks",
      meetingNotes ? "Meeting notes" : "Tasks", body));
    return panel;
  }

  // Normal mode: collapsible sections in order - 12 steps, sub-workpackages, then tasks
  panel.appendChild(collapsibleSection(w.wp_id, "steps", "Abacus - 12 steps", buildGantt(w)));
  panel.appendChild(collapsibleSection(w.wp_id, "subwps",
    "Sub-workpackages" + (w.children && w.children.length ? " (" + w.children.length + ")" : ""),
    buildSubWpFolder(w)));
  panel.appendChild(collapsibleSection(w.wp_id, "tasks", "Tasks", buildTaskBoard(w)));
  return panel;
}

// The 12-step detail (timeline strip + segmented phase rows + sub-point lists) for
// a work package. Extracted so a parent's own steps and each sub-work-package's
// steps use exactly the same clickable editor.
function buildGantt(w) {
  const locked = (w.status || "").toLowerCase() !== "active";
  const gantt = el("div", "gantt");

  // Detailed segmented gantt rows (the overview strip is omitted - the colour
  // bars in the row above already show the same 12-step summary)
  w.phases.forEach((ph) => {
    const key = w.wp_id + ":" + ph.num;
    const isOpen = openPhases.has(key);

    const row = el("div", "grow" + (isOpen ? " open" : "") + (ph.status === "nr" ? " nr" : ""));
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
      track.appendChild(el("div", ph.status === "nr" ? "seg nr" : "seg na"));
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

    const pct = el("div", "gpct", ph.status === "nr" ? "×" : (ph.status === "na" ? "–" : ph.pct + "%"));

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

  return gantt;
}

/* ------------------------------------------------------------------ */
/* Sub-work-packages: a folder of nested 12-step mini-trackers.        */
/* ------------------------------------------------------------------ */
function buildSubWpFolder(parent) {
  const box = el("div", "subwp-folder");
  const kids = parent.children || [];
  kids.forEach((c) => box.appendChild(subWpItem(c)));
  if (!kids.length) box.appendChild(el("div", "subwp-empty", "No sub-workpackages yet."));
  const add = el("button", "subwp-add", "＋ Add sub-workpackage");
  add.addEventListener("click", (e) => { e.stopPropagation(); openSubWpForm(parent, null); });
  box.appendChild(add);
  return box;
}

// One phase's colour pill (shared by the matrix row and the sub-WP colour bars).
// An all-"Not required" phase (status "nr") is left empty for CSS to cross out.
function phasePill(ph) {
  const pill = el("div", "cell-pill " + ph.status);
  pill.title = `${ph.num}. ${ph.title} - ${ph.status === "nr" ? "Not required" : ph.pct + "%"}`;
  if (ph.status === "nr") {
    pill.innerHTML = `<span>×</span>`;
  } else if (ph.status !== "na") {
    // fill reflects the actual % for every status; the colour still marks the state
    pill.innerHTML = `<div class="fill ${ph.status}" style="width:${ph.pct}%"></div><span>${ph.pct}%</span>`;
  } else {
    pill.innerHTML = `<span>–</span>`;
  }
  return pill;
}

// A row of the 12 phase colour bars for a work package (same look as the matrix row).
function miniCells(w) {
  const wrap = el("div", "subwp-cells");
  w.phases.forEach((ph) => wrap.appendChild(phasePill(ph)));
  return wrap;
}

function subWpItem(c) {
  const open = openWPs.has(c.wp_id);
  const wrap = el("div", "subwp" + (open ? " open" : ""));

  // Collapsed row: caret + number + name + the 12-step colour bars (like the matrix row)
  const head = el("div", "subwp-head");
  head.appendChild(el("span", "subwp-caret", "▶"));
  if (c.sub_num) head.appendChild(el("span", "subwp-num", String(c.sub_num).padStart(4, "0")));
  head.appendChild(el("span", "subwp-name", esc(c.name)));
  head.appendChild(miniCells(c));
  head.addEventListener("click", () => toggleWP(c.wp_id));
  wrap.appendChild(head);

  // Expanded: a toolbar (status, %, Duplicate / Edit / Delete) + the full 12-step editor
  if (open) {
    const body = el("div", "subwp-body");
    const bar = el("div", "subwp-bodybar");
    bar.innerHTML = `<span class="badge ${esc(c.status)}">${esc(c.status)}</span>
      <span class="subwp-overall">${c.overall}% complete</span>
      <span class="subwp-body-actions">
        <button class="subwp-dup" title="Copy to the next free number">Duplicate</button>
        <button class="subwp-edit">Edit</button>
        <button class="subwp-del">Delete</button>
      </span>`;
    bar.querySelector(".subwp-dup").addEventListener("click", (e) => { e.stopPropagation(); duplicateProject(c); });
    bar.querySelector(".subwp-edit").addEventListener("click", (e) => { e.stopPropagation(); openSubWpForm(null, c); });
    bar.querySelector(".subwp-del").addEventListener("click", (e) => { e.stopPropagation(); deleteProject(c); });
    body.appendChild(bar);
    body.appendChild(buildGantt(c));
    wrap.appendChild(body);
  }
  return wrap;
}

// Minimal add/edit dialog for a sub-work-package: just a name + which of the 12
// steps apply (no icon / Jira / links / Jamie - those are for top-level projects).
function openSubWpForm(parent, child) {
  closeEditor();
  const isEdit = !!child;
  const template = ((isEdit ? child : parent) || {}).phases || (DATA.work_packages[0] || {}).phases || [];
  const curNr = new Set();
  if (isEdit) child.phases.forEach((ph) => ph.subs.forEach((s) => { if (s.status === "nr") curNr.add(s.code); }));

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

  const modal = el("div", "modal");
  modal.innerHTML = `
    <div class="modal-head">
      <div>
        <div class="modal-code">${isEdit ? "Edit sub-workpackage" : "New sub-workpackage"}</div>
        <h3>${isEdit ? "Edit sub-workpackage" : "Add sub-workpackage"}</h3>
      </div>
      <span class="close" title="Close">✕</span>
    </div>
    <div class="modal-body">
      <div class="field-label">Name <span class="field-hint">short title, max 40 characters</span></div>
      <input class="text-input" id="sw-name" placeholder="e.g. Phase 1 - Discovery" autocomplete="off" maxlength="40"
        value="${isEdit ? esc(child.name) : ""}" />
      <div class="field-label">Which of the 12 steps apply?
        <span class="field-hint">leave ticked = applies · <b>untick</b> anything not required</span>
      </div>
      <div class="nr-picker">${groupsHtml}</div>
    </div>
    <div class="modal-foot">
      <span class="modal-note">${isEdit ? "Changes are saved automatically." : "Added as an <b>Active</b> sub-workpackage."}</span>
      <div>
        <button class="btn" id="sw-cancel">Cancel</button>
        <button class="btn btn-primary" id="sw-save">${isEdit ? "Save changes" : "Add"}</button>
      </div>
    </div>`;

  const syncItem = (cb) => cb.closest(".nr-item").classList.toggle("nr-off", !cb.checked);
  modal.querySelectorAll('.nr-item input[type="checkbox"]').forEach((cb) =>
    cb.addEventListener("change", () => syncItem(cb)));
  modal.querySelectorAll(".nr-all").forEach((b) => b.addEventListener("click", () => {
    const boxes = b.closest(".nr-group").querySelectorAll('input[type="checkbox"]');
    const anyOff = [...boxes].some((x) => !x.checked);
    boxes.forEach((x) => { x.checked = anyOff; syncItem(x); });
  }));

  modal.querySelector(".close").addEventListener("click", closeEditor);
  modal.querySelector("#sw-cancel").addEventListener("click", closeEditor);
  modal.querySelector("#sw-save").addEventListener("click", async () => {
    const name = modal.querySelector("#sw-name").value.trim();
    if (!name) { alert("Please enter a name."); return; }
    const nr_codes = [...modal.querySelectorAll('input[type="checkbox"]:not(:checked)')].map((x) => x.dataset.code);
    const btn = modal.querySelector("#sw-save");
    btn.disabled = true;
    btn.textContent = isEdit ? "Saving…" : "Adding…";
    try {
      let url, payload;
      if (isEdit) {
        url = "/api/edit_project";
        payload = { wp_id: child.wp_id, client: child.client, name, description: child.description || "",
          icon: child.icon || "", nr_codes, jira_project_key: child.jira_project_key || "",
          confluence_url: child.confluence_url || "", dropbox_url: child.dropbox_url || "",
          jamie_tag: child.jamie_tag || "" };
      } else {
        url = "/api/add_project";
        payload = { parent_id: parent.wp_id, client: parent.client, name, nr_codes };
      }
      const res = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      const json = await res.json();
      if (json.error) throw new Error(json.error);
      closeEditor();
      if (!isEdit && json.wp_id) openWPs.add(String(json.wp_id));
      await load();
      startSyncPoll();
    } catch (e) {
      btn.disabled = false;
      btn.textContent = isEdit ? "Save changes" : "Add";
      alert("Could not save: " + e.message);
    }
  });

  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  modal.querySelector("#sw-name").focus();
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

async function deleteProject(w) {
  if (!confirm(`Permanently delete "${w.client} - ${w.name}"?\n\nThis removes the project and all of its progress. This cannot be undone.`)) return;
  try {
    const res = await fetch("/api/delete_project", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wp_id: w.wp_id }),
    });
    const json = await res.json();
    if (json.error) throw new Error(json.error);
    openWPs.delete(String(w.wp_id));
    await load();
    startSyncPoll();
  } catch (e) {
    alert("Could not delete: " + e.message);
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
  const curJira = isEdit && wp ? (wp.jira_project_key || "") : "";

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
  // pre-fill the "type your own" box only when the current icon isn't in the grid
  const customInit = EMOJI_CHOICES.includes(curIcon) ? "" : curIcon;

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
      <div class="field-label">Confluence link <span class="field-hint">optional · paste the page URL</span></div>
      <input class="text-input" id="ap-confluence" placeholder="https://insiderpro.atlassian.net/wiki/spaces/PRAC/pages/…" autocomplete="off"
        value="${isEdit ? esc(wp.confluence_url || "") : ""}" />
      <div class="field-label">Dropbox link <span class="field-hint">optional · leave blank to auto-fill from the client name</span></div>
      <input class="text-input" id="ap-dropbox" placeholder="https://www.dropbox.com/work/Clients/…" autocomplete="off"
        value="${isEdit ? esc(wp.dropbox_url || "") : ""}" />
      <div class="field-label">Jamie tag <span class="field-hint">optional · links this project's meeting notes</span></div>
      <select class="text-input" id="ap-jamie">
        <option value="">none</option>
      </select>
      <div class="field-label">Icon <span class="field-hint">required · pick an emoji (a picture, not text)</span></div>
      <div class="emoji-row">
        ${emojiBtns}
      </div>
      <div class="emoji-custom">
        <span>Or type your own:</span>
        <input class="emoji-input" id="ap-emoji" maxlength="8" placeholder="🙂" value="${esc(customInit)}" />
      </div>
      <div class="field-label" id="ap-jira-label" style="display:none">Jira epic
        <span class="field-hint">optional · pulls completed / total story points from this epic</span>
      </div>
      <select class="text-input" id="ap-jira" style="display:none">
        <option value="">none</option>
      </select>
      <div class="field-label">Which points apply?
        <span class="field-hint">leave ticked = required · <b>untick</b> anything that is Not required</span>
      </div>
      <div class="nr-picker">${groupsHtml}</div>
    </div>
    <div class="modal-foot">
      <span class="modal-note">${isEdit ? "Changes are saved automatically." : "Added as an <b>Active</b> project."}</span>
      <div>
        <button class="btn" id="ap-cancel">Cancel</button>
        <button class="btn btn-primary" id="ap-save">${isEdit ? "Save changes" : "Add project"}</button>
      </div>
    </div>`;

  const emojiInput = modal.querySelector("#ap-emoji");
  // single source of truth for the chosen icon (grid pick OR typed custom emoji)
  let chosenEmoji = curIcon;
  const syncEmojiButtons = () => {
    modal.querySelectorAll(".emoji-btn").forEach((b) =>
      b.classList.toggle("sel", b.dataset.emoji === chosenEmoji));
  };
  modal.querySelectorAll(".emoji-btn").forEach((b) => {
    b.addEventListener("click", () => {
      // toggle: click the selected one again to clear it
      chosenEmoji = chosenEmoji === b.dataset.emoji ? "" : b.dataset.emoji;
      emojiInput.value = "";          // a grid pick is not a custom entry
      syncEmojiButtons();
    });
  });
  emojiInput.addEventListener("input", () => {
    // typing a custom emoji overrides any grid selection
    chosenEmoji = emojiInput.value.trim();
    syncEmojiButtons();
  });
  syncEmojiButtons();

  // Jira epic dropdown - shown only when the server has Jira configured.
  // Epics are grouped by their Jira project via <optgroup>.
  // Populate the Jamie tag dropdown (preselect the current tag on edit)
  const jamieSel = modal.querySelector("#ap-jamie");
  const curJamie = isEdit ? (wp.jamie_tag || "") : "";
  fetch("/api/jamie/tags").then((r) => r.json()).then((j) => {
    (j.tags || []).forEach((t) => {
      const o = document.createElement("option");
      o.value = t; o.textContent = t;
      if (t === curJamie) o.selected = true;
      jamieSel.appendChild(o);
    });
    // keep a previously-set tag selectable even if it's no longer returned
    if (curJamie && ![...jamieSel.options].some((o) => o.value === curJamie)) {
      const o = document.createElement("option");
      o.value = curJamie; o.textContent = curJamie; o.selected = true;
      jamieSel.appendChild(o);
    }
  }).catch(() => {});

  const jiraSel = modal.querySelector("#ap-jira");
  const jiraLabel = modal.querySelector("#ap-jira-label");
  // Show the dropdown straight away whenever Jira is configured (known from the
  // already-loaded data), so it appears consistently for every project even
  // before the epics list finishes loading; then fill in the options.
  if (DATA && DATA.jira_configured) {
    jiraLabel.style.display = "";
    jiraSel.style.display = "";
    const loading = document.createElement("option");
    loading.textContent = "Loading epics…";
    loading.disabled = true;
    loading.className = "jira-loading";
    jiraSel.appendChild(loading);
  }
  fetch("/api/jira/epics").then((r) => r.json()).then((j) => {
    jiraSel.querySelectorAll(".jira-loading").forEach((o) => o.remove());
    if (!j.configured) return;
    jiraLabel.style.display = ""; jiraSel.style.display = "";
    const groups = {};
    (j.epics || []).forEach((e) => { (groups[e.project] = groups[e.project] || []).push(e); });
    Object.keys(groups).sort().forEach((proj) => {
      const og = document.createElement("optgroup");
      og.label = proj;
      groups[proj].forEach((e) => {
        const o = document.createElement("option");
        o.value = e.key;
        o.textContent = `${e.key}: ${e.summary}`.replace(/—/g, "-");
        if (e.key === curJira) o.selected = true;
        og.appendChild(o);
      });
      jiraSel.appendChild(og);
    });
    // keep a previously-linked epic selectable even if it's no longer returned
    if (curJira && ![...jiraSel.options].some((o) => o.value === curJira)) {
      const o = document.createElement("option");
      o.value = curJira; o.textContent = curJira; o.selected = true;
      jiraSel.appendChild(o);
    }
  }).catch(() => { jiraSel.querySelectorAll(".jira-loading").forEach((o) => o.remove()); });

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
    const icon = chosenEmoji.trim();
    // validation
    if (!client || !name) { alert("Please enter both a client and a project name."); return; }
    if (!icon) { alert("Please choose an emoji icon."); return; }
    if (/^[\x00-\x7F]+$/.test(icon)) { alert("The icon must be an emoji (a picture), not text."); return; }
    if (wordCount(description) > 60) { alert("The description is too long: please keep it to 60 words or fewer."); return; }
    // unticked = Not required
    const nr_codes = [...modal.querySelectorAll('input[type="checkbox"]:not(:checked)')].map((x) => x.dataset.code);
    const jira_project_key = jiraSel ? jiraSel.value : "";
    const confluence_url = modal.querySelector("#ap-confluence").value.trim();
    const dropbox_url = modal.querySelector("#ap-dropbox").value.trim();
    const jamie_tag = modal.querySelector("#ap-jamie").value.trim();
    const btn = modal.querySelector("#ap-save");
    btn.disabled = true;
    btn.textContent = isEdit ? "Saving…" : "Adding…";
    try {
      const url = isEdit ? "/api/edit_project" : "/api/add_project";
      const payload = isEdit
        ? { wp_id: wp.wp_id, client, name, description, icon, nr_codes, jira_project_key, confluence_url, dropbox_url, jamie_tag }
        : { client, name, description, icon, nr_codes, jira_project_key, confluence_url, dropbox_url, jamie_tag };
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
      <span class="modal-note">Saved to the dashboard instantly.</span>
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
/* Task board - simple two-box Kanban per work package (embedded).     */
/*   Left "Working" box: To Do (red) + In Progress (amber).            */
/*   Right "Complete" box: Done (green).                               */
/*   Click a card to advance todo -> progress -> done -> todo.         */
/*   Each task carries modified-Fibonacci story points (max 21).       */
/* ------------------------------------------------------------------ */
const TASK_NEXT = { todo: "progress", progress: "done", done: "todo" };
const TASK_LABEL = { todo: "To Do", progress: "In Progress", done: "Complete" };
const POINT_CHOICES = [1, 2, 3, 5, 8, 13, 21];   // modified Fibonacci; minimum + default 1

function pointsSelectHtml(cls, current) {
  const cur = POINT_CHOICES.includes(current) ? current : 1;   // never blank/"-": default 1
  const opts = POINT_CHOICES.map((p) =>
    `<option value="${p}"${p === cur ? " selected" : ""}>${p} pts</option>`
  ).join("");
  return `<select class="${cls}" title="Story points (modified Fibonacci)">${opts}</select>`;
}

// Build the embedded task board element for a work package (open by default).
function buildTaskBoard(w) {
  const section = el("div", "taskboard-section");
  section.innerHTML = `
    <div class="taskboard">
      <div class="task-box working">
        <div class="task-box-head">Working <span class="tb-hint">click or drag a card →</span>
          <span class="tb-total working-total"></span></div>
        <div class="task-list tb-working"></div>
        <button class="tb-add-btn" title="Add a new task">+ Add task</button>
      </div>
      <div class="task-box complete">
        <div class="task-box-head">Complete ✓ <span class="tb-total complete-total"></span></div>
        <div class="task-list tb-complete"></div>
      </div>
    </div>`;

  const working = section.querySelector(".tb-working");
  const complete = section.querySelector(".tb-complete");
  const workingTotal = section.querySelector(".working-total");
  const completeTotal = section.querySelector(".complete-total");
  let tasksCache = [];

  // sub-work-packages this work package has, for the per-task link dropdown
  const subs = w.children || [];
  const subWpOptions = (current) => {
    const opts = ['<option value="">(no WP)</option>'];
    subs.forEach((c) => {
      const num = c.sub_num ? String(c.sub_num).padStart(4, "0") : "?";
      opts.push(`<option value="${c.wp_id}"${String(current) === String(c.wp_id) ? " selected" : ""}>${num}</option>`);
    });
    return opts.join("");
  };

  function cardEl(t) {
    const card = el("div", "task-card " + t.status);
    card.dataset.id = t.id;
    card.draggable = true;
    card.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", String(t.id));
      e.dataTransfer.effectAllowed = "move";
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
    card.innerHTML = `
      <span class="task-status-dot" title="${TASK_LABEL[t.status]}"></span>
      <span class="task-title">${esc(t.title)}</span>
      ${subs.length ? `<select class="task-subwp" title="Link to a sub-workpackage">${subWpOptions(t.sub_wp_id)}</select>` : ""}
      ${pointsSelectHtml("task-points", t.points || 0)}
      <button class="task-del" title="Delete task">✕</button>`;
    card.title = "Click to advance · double-click to rename";
    if (t.status !== "done") card.querySelector(".task-title").classList.add("renamable");
    // single click advances the status (To Do -> In Progress amber -> Complete);
    // double click edits the wording. A short timer disambiguates the two.
    const isControl = (e) => e.target.closest(".task-del") || e.target.closest(".task-points") || e.target.closest(".task-subwp");
    let clickTimer = null;
    card.addEventListener("click", (e) => {
      if (isControl(e) || clickTimer) return;
      clickTimer = setTimeout(() => { clickTimer = null; setTaskStatus(t, TASK_NEXT[t.status]); }, 220);
    });
    card.addEventListener("dblclick", (e) => {
      if (isControl(e)) return;
      if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }   // cancel the single-click advance
      if (t.status !== "done") startRename(t, card.querySelector(".task-title"));
    });
    const sel = card.querySelector(".task-points");
    sel.addEventListener("click", (e) => e.stopPropagation());
    sel.addEventListener("change", (e) => { e.stopPropagation(); setTaskPoints(t, parseInt(sel.value, 10)); });
    const subSel = card.querySelector(".task-subwp");
    if (subSel) {
      subSel.addEventListener("click", (e) => e.stopPropagation());
      subSel.addEventListener("change", (e) => { e.stopPropagation(); setTaskSubWp(t, subSel.value); });
    }
    card.querySelector(".task-del").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteTask(t);
    });
    return card;
  }

  function paint(tasks) {
    tasksCache = tasks;
    working.innerHTML = "";
    complete.innerHTML = "";
    let wp = 0, cp = 0;
    tasks.forEach((t) => {
      (t.status === "done" ? complete : working).appendChild(cardEl(t));
      if (t.status === "done") cp += (t.points || 0); else wp += (t.points || 0);
    });
    if (!working.children.length) working.innerHTML = '<div class="task-empty">No tasks yet - add one above.</div>';
    if (!complete.children.length) complete.innerHTML = '<div class="task-empty">Nothing complete yet.</div>';
    workingTotal.textContent = wp ? wp + " pts" : "";
    completeTotal.textContent = cp ? cp + " pts" : "";
    // keep the matrix row badge in sync without a full reload
    updateRowTaskBadge(w.wp_id, tasks);
  }

  async function refresh() {
    try {
      const res = await fetch("/api/tasks/" + w.wp_id);
      const json = await res.json();
      if (json.error) throw new Error(json.error);
      paint(json.tasks || []);
    } catch (e) {
      working.innerHTML = '<div class="task-empty">Could not load tasks: ' + esc(e.message) + "</div>";
    }
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const json = await res.json();
    if (json.error) throw new Error(json.error);
    return json;
  }

  async function setTaskStatus(t, status) {
    try { await post("/api/tasks/set_status", { task_id: t.id, status }); await refresh(); }
    catch (e) { alert("Could not update task: " + e.message); }
  }
  async function setTaskPoints(t, points) {
    try { await post("/api/tasks/set_points", { task_id: t.id, points }); await refresh(); }
    catch (e) { alert("Could not set points: " + e.message); }
  }
  async function setTaskSubWp(t, subWpId) {
    try { await post("/api/tasks/set_sub_wp", { task_id: t.id, sub_wp_id: subWpId || "" }); await refresh(); }
    catch (e) { alert("Could not link task: " + e.message); }
  }
  async function setTaskTitle(t, title) {
    try { await post("/api/tasks/set_title", { task_id: t.id, title }); await refresh(); }
    catch (e) { alert("Could not rename task: " + e.message); refresh(); }
  }
  // Turn the task wording into an inline edit box (double-click to rename).
  function startRename(t, titleEl) {
    const card = titleEl.closest(".task-card");
    if (card) card.draggable = false;   // let the text be selected without dragging
    const input = document.createElement("input");
    input.className = "task-rename";
    input.value = t.title;
    input.maxLength = 200;
    titleEl.replaceWith(input);
    input.focus();
    input.select();
    let closed = false;
    const commit = (save) => {
      if (closed) return;
      closed = true;
      const v = input.value.trim();
      if (save && v && v !== t.title) setTaskTitle(t, v);
      else refresh();   // cancel / no change: rebuild the card
    };
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); commit(true); }
      else if (e.key === "Escape") { e.preventDefault(); commit(false); }
    });
    input.addEventListener("click", (e) => e.stopPropagation());
    input.addEventListener("blur", () => commit(true));
  }
  async function deleteTask(t) {
    try { await post("/api/tasks/delete", { task_id: t.id }); await refresh(); }
    catch (e) { alert("Could not delete task: " + e.message); }
  }
  async function addTask(title, points) {
    try { await post("/api/tasks/add", { wp_id: w.wp_id, title, points }); await refresh(); }
    catch (e) { alert("Could not add task: " + e.message); }
  }

  // Small pop-out: type the task + pick points, then Add.
  function openAddPopout() {
    closeEditor();
    let chosen = 1;   // minimum + default
    const overlay = el("div", "modal-overlay");
    overlay.id = "editor";
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeEditor(); });
    const chips = POINT_CHOICES.map((p) =>
      `<button type="button" class="point-chip${p === chosen ? " sel" : ""}" data-p="${p}">${p}</button>`
    ).join("");
    const modal = el("div", "modal addtask-modal");
    modal.innerHTML = `
      <div class="modal-head">
        <div><div class="modal-code">New task</div><h3>${esc(w.client)} - ${esc(w.name)}</h3></div>
        <span class="close" title="Close">✕</span>
      </div>
      <div class="modal-body">
        <div class="field-label">Task</div>
        <input class="text-input" id="nt-title" placeholder="What needs doing?" autocomplete="off" maxlength="200" />
        <div class="field-label">Points <span class="field-hint">optional · modified Fibonacci</span></div>
        <div class="point-chips">${chips}</div>
      </div>
      <div class="modal-foot">
        <span class="modal-note">Added to <b>Working</b> as To&nbsp;Do.</span>
        <div>
          <button class="btn" id="nt-cancel">Cancel</button>
          <button class="btn btn-primary" id="nt-add">Add task</button>
        </div>
      </div>`;
    const titleEl = modal.querySelector("#nt-title");
    modal.querySelectorAll(".point-chip").forEach((b) => b.addEventListener("click", () => {
      chosen = parseInt(b.dataset.p, 10);
      modal.querySelectorAll(".point-chip").forEach((x) => x.classList.toggle("sel", x === b));
    }));
    const submit = async () => {
      const title = titleEl.value.trim();
      if (!title) { titleEl.focus(); return; }
      closeEditor();
      await addTask(title, chosen);
    };
    modal.querySelector(".close").addEventListener("click", closeEditor);
    modal.querySelector("#nt-cancel").addEventListener("click", closeEditor);
    modal.querySelector("#nt-add").addEventListener("click", submit);
    titleEl.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    titleEl.focus();
  }

  section.querySelector(".tb-add-btn").addEventListener("click", (e) => { e.stopPropagation(); openAddPopout(); });

  // Drag a card between the two boxes: drop in Complete = done, drop back in Working = to do.
  const workingBox = section.querySelector(".task-box.working");
  const completeBox = section.querySelector(".task-box.complete");
  function wireDrop(boxEl, target) {
    boxEl.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      boxEl.classList.add("drop-hover");
    });
    boxEl.addEventListener("dragleave", (e) => {
      if (!boxEl.contains(e.relatedTarget)) boxEl.classList.remove("drop-hover");
    });
    boxEl.addEventListener("drop", (e) => {
      e.preventDefault();
      boxEl.classList.remove("drop-hover");
      const id = e.dataTransfer.getData("text/plain");
      const t = tasksCache.find((x) => String(x.id) === String(id));
      if (!t) return;
      if (target === "done" && t.status !== "done") setTaskStatus(t, "done");
      else if (target === "working" && t.status === "done") setTaskStatus(t, "todo");
    });
  }
  wireDrop(workingBox, "working");
  wireDrop(completeBox, "done");

  // don't let clicks inside the board toggle the work-package row
  section.addEventListener("click", (e) => e.stopPropagation());

  refresh();
  return section;
}

// Update the small "✓ done/total tasks" badge on a matrix row (and DATA) in place.
function updateRowTaskBadge(wpId, tasks) {
  const total = tasks.length;
  const done = tasks.filter((t) => t.status === "done").length;
  const wpData = (DATA.work_packages || []).find((x) => x.wp_id === wpId);
  if (wpData) { wpData.task_total = total; wpData.task_done = done; }
  const row = document.querySelector(`.wp-row[data-id="${wpId}"] .wp-text`);
  if (!row) return;
  let badge = row.querySelector(".task-badge");
  if (total === 0) { if (badge) badge.remove(); return; }
  if (!badge) {
    badge = el("span", "task-badge");
    badge.title = "Task board";
    row.appendChild(badge);
  }
  badge.classList.toggle("all-done", done === total);
  badge.textContent = `✓ ${done}/${total} tasks`;
}

/* ------------------------------------------------------------------ */
/* Meeting notes (Jamie) - embedded per work package. Same content the  */
/* customer portal shows; staff see every project (each scoped to its   */
/* own Jamie tag). Overview (Summary / Full notes) + Key action items.  */
/* ------------------------------------------------------------------ */
function nFmtDate(iso, long) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleDateString(undefined,
    long ? { weekday: "short", day: "numeric", month: "long", year: "numeric" }
         : { day: "numeric", month: "short", year: "numeric" });
}
function nInitials(name) {
  const p = String(name || "").trim().split(/\s+/);
  return ((p[0]?.[0] || "") + (p[1]?.[0] || "")).toUpperCase() || "?";
}
const N_ACTION_RE = /(action items?|next steps?|follow[\s-]?ups?)/i;
function nStripActions(html) {
  if (!html) return html;
  const tmp = document.createElement("div");
  tmp.innerHTML = html;
  Array.from(tmp.querySelectorAll("h1,h2,h3,h4,h5")).forEach((h) => {
    if (!N_ACTION_RE.test(h.textContent || "")) return;
    const rm = [h]; let n = h.nextElementSibling;
    while (n && !/^H[1-5]$/.test(n.nodeName)) { rm.push(n); n = n.nextElementSibling; }
    rm.forEach((x) => x.remove());
  });
  return tmp.innerHTML;
}
function nExtractSummary(html) {
  const tmp = document.createElement("div");
  tmp.innerHTML = html;
  const out = []; let capturing = false, saw = false;
  for (const node of Array.from(tmp.childNodes)) {
    if (node.nodeName === "H1" || node.nodeName === "H2") {
      saw = true;
      const t = (node.textContent || "").toLowerCase();
      capturing = t.includes("executive") || (t.includes("summary") && !t.includes("full"));
      if (t.includes("full summary") || t.includes("attendees") || t.includes("transcript")) capturing = false;
    }
    if (capturing) out.push(node.outerHTML || node.textContent);
  }
  return (saw && out.length) ? out.join("") : html;
}
function nActionHtml(t) {
  const who = t.assignee || "";
  const av = who ? `<span class="note-assignee"><span class="avatar">${esc(nInitials(who))}</span>${esc(who)}</span>` : "";
  return `<li class="note-action ${t.completed ? "done" : ""}"><div class="na-text">${esc(t.text || "")}</div>${av}</li>`;
}

function buildMeetingNotes(w) {
  const box = el("div", "wp-notes");
  if (!w.jamie_tag) {
    box.innerHTML = `<div class="notes-empty">No Jamie tag set for this project. Click <b>Edit</b> and choose a Jamie tag to show its meeting notes.</div>`;
    return box;
  }
  box.innerHTML = `<div class="notes-layout">
      <div class="notes-list"><div class="notes-loading">Loading meetings…</div></div>
      <div class="notes-detail"></div>
    </div>`;
  const listEl = box.querySelector(".notes-list");
  const detailEl = box.querySelector(".notes-detail");
  const base = "/api/wp/" + w.wp_id;
  let meetings = [], currentId = null, currentDetail = null, view = "summary";

  function renderList() {
    listEl.innerHTML = "";
    meetings.forEach((m) => {
      const it = el("div", "note-item" + (m.id === currentId ? " active" : ""));
      it.innerHTML = `<div class="ni-title">${esc(m.title)}</div><div class="ni-date">${esc(nFmtDate(m.startTime))}</div>`;
      it.addEventListener("click", () => selectMeeting(m.id));
      listEl.appendChild(it);
    });
  }
  async function selectMeeting(id) {
    currentId = id; renderList();
    detailEl.innerHTML = `<div class="notes-loading">Loading notes…</div>`;
    try {
      const r = await fetch(base + "/meeting/" + encodeURIComponent(id));
      const j = await r.json(); if (j.error) throw new Error(j.error);
      currentDetail = j; renderDetail();
    } catch (e) { detailEl.innerHTML = `<div class="notes-empty">Could not load notes: ${esc(e.message)}</div>`; }
  }
  function renderDetail() {
    const d = currentDetail; if (!d) return;
    const isFirst = meetings.length && meetings[0].id === d.id;
    const tasks = d.tasks || [];
    const clean = nStripActions(d.summaryHtml || "");
    const overview = d.summaryHtml
      ? (view === "summary" ? nExtractSummary(clean) : clean)
      : `<p class="notes-empty">No overview available for this meeting.</p>`;
    const actions = tasks.length ? tasks.map(nActionHtml).join("") : `<li class="notes-empty">No action items captured.</li>`;
    detailEl.innerHTML = `
      <div class="note-badge">${isFirst ? "Latest meeting" : "Meeting"}</div>
      <h3 class="note-title">${esc(d.title)}</h3>
      <div class="note-meta">📅 ${esc(nFmtDate(d.startTime, true))}</div>
      <div class="note-toggle">
        <button data-v="summary" class="ntg${view === "summary" ? " active" : ""}">Summary</button>
        <button data-v="full" class="ntg${view === "full" ? " active" : ""}">Full notes</button>
      </div>
      <div class="note-overview">${overview}</div>
      <div class="note-actions-head">Key action items
        <span class="pill">${tasks.filter((t) => !t.completed).length} open / ${tasks.length} total</span></div>
      <ul class="note-actions">${actions}</ul>`;
    detailEl.querySelectorAll(".ntg").forEach((b) =>
      b.addEventListener("click", () => { view = b.dataset.v; renderDetail(); }));
  }

  fetch(base + "/meetings").then((r) => r.json()).then((j) => {
    if (j.error) { listEl.innerHTML = `<div class="notes-empty">Could not load meetings: ${esc(j.error)}</div>`; return; }
    meetings = j.meetings || [];
    if (!meetings.length) { listEl.innerHTML = `<div class="notes-empty">${esc(j.note || "No meetings found for this project.")}</div>`; return; }
    renderList();
    selectMeeting(meetings[0].id);
  }).catch((e) => { listEl.innerHTML = `<div class="notes-empty">Could not load meetings: ${esc(e.message)}</div>`; });

  return box;
}

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

// "Actions only": open every visible work package showing just its task board
$("#actions-only").addEventListener("change", (e) => {
  actionsOnly = e.target.checked;
  if (actionsOnly) {
    meetingNotes = false;
    const mn = document.getElementById("meeting-notes"); if (mn) mn.checked = false;
    visibleWPs().forEach((w) => { openWPs.add(w.wp_id); collapsedSecs.delete(w.wp_id + ":tasks"); });
  } else {
    openWPs.clear();
  }
  render();
});

// "Meeting notes": open every visible work package showing its Jamie meeting notes
$("#meeting-notes").addEventListener("change", (e) => {
  meetingNotes = e.target.checked;
  if (meetingNotes) {
    actionsOnly = false;
    const ao = document.getElementById("actions-only"); if (ao) ao.checked = false;
    visibleWPs().forEach((w) => { openWPs.add(w.wp_id); collapsedSecs.delete(w.wp_id + ":notes"); });
  } else {
    openWPs.clear();
  }
  render();
});

// One Refresh: re-pull Jira story points (if configured) and reload the data,
// which also re-fetches the Jamie meeting notes for any open work packages.
$("#refresh-btn").addEventListener("click", async () => {
  const btn = $("#refresh-btn");
  const sync = $("#sync");
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  let jiraMsg = "";
  try {
    if (DATA && DATA.jira_configured) {
      const res = await fetch("/api/jira/refresh", { method: "POST" });
      const json = await res.json();
      if (json.error) throw new Error(json.error);
      const failed = (json.errors || []).length;
      jiraMsg = `Jira ${json.updated}${failed ? `, ${failed} failed` : ""} · `;
    }
    await load();   // reloads the matrix + re-fetches Jamie notes for open panels
    if (sync) { sync.className = "sync ok"; sync.textContent = jiraMsg + "Refreshed"; }
  } catch (e) {
    if (sync) { sync.className = "sync error"; sync.textContent = "Refresh failed: " + e.message; }
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
});

window.addEventListener("resize", sizePanels);

load();
