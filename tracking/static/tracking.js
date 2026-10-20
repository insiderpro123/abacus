/* ==========================================================================
   Tracking Dashboard - one screen.

   The board of live leads, the record panel and the reference drawer. Vanilla
   JS, no dependencies (same convention as the Abacus tracker).

   There are no filters and no number tiles: every live lead is on the board,
   in its stage column, all the time. Closed contacts are off the board behind
   one toggle, which is the only thing that changes what you can see.

   The rule that keeps it quick: /api/tracking/data is fetched once, on load.
   Every write returns the record it changed, and applyContact() patches that
   into DATA and redraws from memory. Nothing in here refetches the whole
   payload.
   ========================================================================== */

/* Every route this page calls lives under here, apart from the live site's. */
const API = "/api/tracking";

let DATA = null;
let selectedId = null;          // the contact whose record panel is open
let showClosed = false;         // the board is live leads only until you ask

/* ---------- helpers (same shape as Abacus static/app.js) ---------- */
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

const stageOf = (num) =>
  (DATA.stages || []).find((s) => s.num === num) || { num, label: num, short: num, tone: "cold" };
const isClosed = (stage) => (DATA.closed_stages || []).includes(stage);
const isWon = (stage) => (DATA.won_stages || []).includes(stage);   // Work agreed

/* There is no sign-in and no touch log: it is an Insider Pro tool. Where a
   company has got to is in its record: the people to contact, their red /
   amber / green, and one Comments box (8 Oct 2026). Since 20 Oct 2026 each
   card also says who is responsible for it (as initials), its sector and a
   few words on where it stands, all typed in the record. The chance of a win
   came off the card the same day; it still sorts each column. */

/* "Ann Other" -> "AO", "Ann" -> "A": the first letter of the first and the
   last word. */
function initials(name) {
  const words = String(name || "").trim().split(/\s+/).filter(Boolean)
    .map((w) => (w.match(/[\p{L}\d]/u) || [""])[0]).filter(Boolean);
  if (!words.length) return "";
  return (words[0] + (words.length > 1 ? words[words.length - 1] : "")).toUpperCase();
}

/* What a card's field has been set to anywhere on the board, once each
   (ignoring case), for the record's suggestion lists. */
function inUse(values) {
  const seen = new Map();
  values.forEach((v) => {
    const t = String(v || "").trim();
    if (t && !seen.has(t.toLowerCase())) seen.set(t.toLowerCase(), t);
  });
  return [...seen.values()].sort((a, b) => a.localeCompare(b));
}
const sectorOptions = () =>
  inUse([...(DATA.sector_suggestions || []), ...DATA.contacts.map((c) => c.sector)]);
const responsibleOptions = () => inUse(DATA.contacts.map((c) => c.responsible));

/* A text box with a list of suggestions under it; anything can still be typed. */
function suggestInput(id, options) {
  const input = el("input");
  input.type = "text";
  input.setAttribute("list", id);
  const dl = el("datalist");
  dl.id = id;
  options.forEach((o) => dl.appendChild(new Option(o, o)));
  return [input, dl];
}

/* One plain line under the name: how long a live card has sat in its stage,
   once that is worth flagging, or nothing. The next step's date used to come
   first, but the record has no next step since 8 Oct 2026, so an old date
   would read "Overdue" with no way to clear it. */
function statusOf(c) {
  if (isClosed(c.stage) || isWon(c.stage)) return null;
  if (c.ageing !== "ok") {
    return { cls: c.ageing, text: `${c.days_in_stage} days in ${stageOf(c.stage).short}` };
  }
  return null;
}

function setStatus(msg, isError) {
  const s = $("#status");
  if (!s) return;
  s.className = "status" + (isError ? " error" : "");
  s.textContent = msg || "";
}

async function api(path, body) {
  const res = await fetch(API + path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  // On the live site a 401 means the team login expired: sign in again, the
  // same as every other page there does. Locally there is no login to expire.
  if (res.status === 401) { window.location.href = "/login"; throw new Error("Signed out."); }
  let json = {};
  try { json = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok || json.error) throw new Error(json.error || `Request failed (${res.status})`);
  return json;
}

/* ---------- load, and the in-place patch that replaces reloading ---------- */
async function load() {
  setStatus("Loading...");
  try {
    DATA = await api("/data");
    setStatus("");
    render();
  } catch (e) {
    setStatus(e.message, true);
  }
}

function render() {
  if (!DATA) return;
  renderTop();
  renderBoard();
}

/* One write, one record back, one redraw from memory. `panel` is opt-in because
   rebuilding the record panel destroys whatever input the user is typing in. */
function applyContact(c, opts = {}) {
  if (!c) return;
  const i = DATA.contacts.findIndex((x) => x.id === c.id);
  if (i >= 0) DATA.contacts[i] = c; else DATA.contacts.push(c);
  renderTop();
  renderBoard();
  if (opts.panel && selectedId === c.id) renderDetail();
}

/* ---------- the one line above the board ----------
   The live count (agreed work is no longer a lead), and the toggle that brings
   the Closed column back when there is anything in it. Nothing here hides a
   live lead. */
function renderTop() {
  const live = DATA.contacts.filter((c) => !isClosed(c.stage) && !isWon(c.stage)).length;
  const closed = DATA.contacts.filter((c) => isClosed(c.stage)).length;
  $("#live-count").textContent = `${live} live lead${live === 1 ? "" : "s"}`;
  const btn = $("#show-closed");
  btn.hidden = !closed;
  btn.textContent = showClosed ? `Hide closed (${closed})` : `Closed (${closed})`;
  btn.title = showClosed ? "Take the Closed column off the board"
                         : "Put the Closed column back on the board";
  btn.classList.toggle("on", showClosed);
}

/* ---------- board ----------
   Every live lead, in its stage column, best chance at the top. The Closed
   column only appears when the toggle is on, so the board is the leads you are
   actually working. */
function renderBoard() {
  const board = $("#board");
  board.innerHTML = "";
  const columns = DATA.stages.filter((st) => showClosed || !isClosed(st.num));
  columns.forEach((st) => {
    const cards = DATA.contacts.filter((c) => c.stage === st.num).sort((a, b) =>
      (b.chance ?? -1) - (a.chance ?? -1) || (a.company || "").localeCompare(b.company || ""));
    const col = el("div", `col tone-${st.tone}${isClosed(st.num) ? " closed-stage" : ""}`);
    col.dataset.stage = st.num;
    col.appendChild(el("div", "col-head",
      `<span class="cn">${st.num}</span><span class="ct">${esc(st.short)}</span>` +
      `<span class="cc">${cards.length}</span>`));

    const body = el("div", "col-body");
    if (!cards.length) body.appendChild(el("div", "col-empty", "nothing here"));
    cards.forEach((c) => body.appendChild(cardNode(c)));
    col.appendChild(body);

    // drop target
    col.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("dragover"); });
    col.addEventListener("dragleave", () => col.classList.remove("dragover"));
    col.addEventListener("drop", async (e) => {
      e.preventDefault();
      col.classList.remove("dragover");
      const id = parseInt(e.dataTransfer.getData("text/plain"), 10);
      if (id) await moveStage(id, st.num);
    });
    board.appendChild(col);
  });
}

/* A card is who looks after it, where it stands and who to talk to: the
   company with the responsible person's initials, the few-word summary, the
   person, then the sector tag. The priority is the coloured left edge;
   everything else is in the record. */
function cardNode(c) {
  const card = el("div", `pcard p-${c.priority}${c.id === selectedId ? " sel" : ""}`);
  card.draggable = true;
  card.title = `${c.priority} priority. Click to open.`;
  const who = c.contact_name
    ? `${c.contact_name}${c.target_role ? " · " + c.target_role : ""}`
    : (c.target_role || c.cost_area || "");
  const ini = initials(c.responsible);
  const status = statusOf(c);
  card.innerHTML =
    `<div class="pc-top">` +
      `<div class="pc-co">${esc(c.company || "(no company)")}</div>` +
      (ini ? `<span class="pc-ini" title="${esc(c.responsible)} is responsible">${esc(ini)}</span>` : "") +
    `</div>` +
    (c.summary ? `<div class="pc-sum">${esc(c.summary)}</div>` : "") +
    (who ? `<div class="pc-who" title="${esc(who)}">${esc(who)}</div>` : "") +
    (status ? `<div class="pc-status ${status.cls}">${esc(status.text)}</div>` : "");

  /* The sector tag on the left, and the arrow that moves it on. */
  const next = (c.stage || 1) + 1;
  if (c.sector || next <= DATA.last_open_stage) {
    const foot = el("div", "pc-foot");
    if (c.sector) foot.appendChild(el("span", "pc-tag", esc(c.sector)));
    if (next <= DATA.last_open_stage) {
      const adv = el("button", "qk adv", "&rarr;");
      adv.title = `Move to the ${stageOf(next).label} column`;
      adv.onclick = (e) => { e.stopPropagation(); moveStage(c.id, next); };
      foot.appendChild(adv);
    }
    card.appendChild(foot);
  }

  card.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", String(c.id));
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));
  card.onclick = () => toggleContact(c.id);
  return card;
}

/* ---------- the two writes the board is made of ---------- */
async function moveStage(id, stage) {
  const c = DATA.contacts.find((x) => x.id === id);
  if (!c || c.stage === stage) return;
  try {
    const res = await api("/contact/set_stage", { id, stage });
    // closing something shows the Closed column, so nothing appears to vanish
    if (isClosed(stage)) showClosed = true;
    applyContact(res.contact, { panel: true });
    setStatus(`${c.company || "Contact"} moved to the ${stageOf(stage).label} column.` +
      (isClosed(stage) && !res.contact.outcome ? " Set the outcome in the record." : ""));
  } catch (e) { setStatus(e.message, true); }
}

async function saveFields(c, fields, redrawPanel) {
  try {
    const res = await api("/contact/update", { id: c.id, fields });
    applyContact(res.contact, { panel: !!redrawPanel });
    return res.contact;
  } catch (e) {
    setStatus(e.message, true);
    return null;
  }
}

/* ---------- slide-over plumbing, shared by the record and the drawer ------- */
function sheetShell(host, title, buildBody, buildActions) {
  releaseFocus(host);
  host.innerHTML = "";
  const back = el("div", "sheet-back");
  back.onclick = () => closeSheet(host);
  const panel = el("div", "sheet-panel");
  const head = el("div", "panel-head");
  head.appendChild(title);
  const acts = el("div", "panel-head-actions");
  if (buildActions) buildActions(acts);
  const close = el("button", "close", "&times;");
  close.title = "Close";
  close.onclick = () => closeSheet(host);
  acts.appendChild(close);
  head.appendChild(acts);
  panel.appendChild(head);
  const body = el("div", "sheet-body");
  buildBody(body);
  panel.appendChild(body);
  host.appendChild(back);
  host.appendChild(panel);
  host.hidden = false;
  document.body.classList.add("sheet-open");
}

/* Blur whatever is being typed in before a sheet is cleared, so its
   save-on-blur runs: removing a focused box does not reliably fire blur. */
function releaseFocus(host) {
  if (host.contains(document.activeElement)) document.activeElement.blur();
}

function closeSheet(host) {
  releaseFocus(host);
  host.hidden = true;
  host.innerHTML = "";
  if (host.id === "detail") { selectedId = null; renderBoard(); }
  if (!document.querySelector(".sheet:not([hidden])")) {
    document.body.classList.remove("sheet-open");
  }
}

function openContact(id) { selectedId = id; renderBoard(); renderDetail(); }
function toggleContact(id) {
  selectedId = selectedId === id ? null : id;
  renderBoard();
  renderDetail();
}

/* ---------- the record panel ----------
   The company's record (8 Oct 2026, the user's layout): the stage row, then
   five people to contact there, each with a contact status anyone can type
   and a red / amber / green, then one Comments box. The latest update, chance
   of a win, next step and Details it replaced are still in the database,
   unshown. People and Comments belong to the company, so every card at that
   company opens the same ones. */
const RAG = [
  { key: "red",   label: "Red" },
  { key: "amber", label: "Amber" },
  { key: "green", label: "Green" },
];

const companyOf = (c) => (DATA.companies || []).find((x) => x.id === c.company_id);

function renderDetail() {
  const host = $("#detail");
  const c = DATA.contacts.find((x) => x.id === selectedId);
  if (!c) { closeSheet(host); return; }
  const st = stageOf(c.stage);

  const title = el("div", null,
    `<h2>${esc(c.company || "(no company)")}</h2>` +
    `<div class="sub">${esc(c.target_role || "")}` +
    (c.contact_name ? ` &middot; <b>${esc(c.contact_name)}</b>` : "") +
    ` &middot; ${c.days_in_stage} day${c.days_in_stage === 1 ? "" : "s"} in ` +
    `<span class="stage-badge tone-${st.tone}">${esc(st.short)}</span>` +
    (c.outcome ? ` <span class="stage-badge o-${esc(c.outcome)}">${esc(c.outcome)}</span>` : "") +
    `</div>`);

  sheetShell(host, title, (body) => {
    body.appendChild(el("div", "seclabel", "Stage"));
    body.appendChild(stagePicker(c));
    if (isClosed(c.stage)) body.appendChild(outcomeRow(c));
    body.appendChild(el("div", "seclabel", "On the card"));
    body.appendChild(cardFields(c));

    const co = companyOf(c);
    if (!co) {
      body.appendChild(el("div", "note-hint no-co",
        "This contact has no company, so there is nowhere to keep people or comments."));
      return;
    }
    body.appendChild(el("div", "seclabel", "People to contact"));
    body.appendChild(peopleBlock(co));
    body.appendChild(el("div", "seclabel", "Comments"));
    body.appendChild(commentsBlock(co));
  }, (acts) => {
    if (c.linkedin_url) {
      const a = el("a", "btn btn-ghost btn-sm", "LinkedIn");
      a.href = c.linkedin_url; a.target = "_blank"; a.rel = "noopener";
      acts.appendChild(a);
    }
    if (c.email) {
      const a = el("a", "btn btn-ghost btn-sm", "Email");
      a.href = `mailto:${c.email}`;
      acts.appendChild(a);
    }
    const del = el("button", "btn btn-danger btn-sm", "Delete");
    del.onclick = async () => {
      if (!confirm(`Delete ${c.company || "this contact"}?`)) return;
      try {
        await api("/contact/delete", { id: c.id });
        DATA.contacts = DATA.contacts.filter((x) => x.id !== c.id);
        closeSheet($("#detail"));
        render();
        setStatus("Contact deleted.");
      } catch (e) { setStatus(e.message, true); }
    };
    acts.appendChild(del);
  });
}

/* The stage control inside the record: one labelled button per stage, so
   moving a card back or straight to Closed is a single click. */
function stagePicker(c) {
  const wrap = el("div", "spick");
  DATA.stages.forEach((s) => {
    const state = s.num === c.stage ? " at" : s.num < c.stage ? " past" : "";
    const b = el("button", `sp tone-${s.tone}${state}`, esc(s.short));
    b.title = s.num === c.stage ? `Already in ${s.label}` : `Move to the ${s.label} column`;
    b.onclick = () => moveStage(c.id, s.num);
    wrap.appendChild(b);
  });
  return wrap;
}

function outcomeRow(c) {
  const row = el("div", "orow");
  row.appendChild(el("span", "olab", "How did it end?"));
  (DATA.outcomes || []).forEach((o) => {
    const b = el("button", "ob" + (c.outcome === o ? ` on o-${o}` : ""), esc(o));
    b.onclick = () => saveFields(c, { outcome: o }, true);
    row.appendChild(b);
  });
  return row;
}

/* What the card shows (20 Oct 2026): who is responsible for it (the card
   carries their initials), its sector tag and a few words on where it stands.
   These belong to the card, not the company, so a second contact at the same
   company keeps its own. Each box saves when you click away; the board's copy
   is patched first, so a panel redrawn mid-save shows what was typed. */
function cardFields(c) {
  const wrap = el("div", "cardfields");
  const grid = el("div", "fieldgrid");
  const hint = saveHint();
  const field = (key, label, input, list, wide) => {
    const f = el("div", "field" + (wide ? " wide" : ""));
    const lab = el("label", null, esc(label));
    input.id = `cf-${key}`;
    lab.htmlFor = input.id;
    input.value = c[key] || "";
    let saved = input.value.trim();
    input.onblur = async () => {
      const now = input.value.trim();
      if (now === saved) return;
      const mine = DATA.contacts.find((x) => x.id === c.id);
      if (mine) mine[key] = now;
      const u = await saveFields(c, { [key]: now });
      if (!u) return;
      saved = u[key];
      hint.flash();
    };
    input.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); input.blur(); } };
    f.appendChild(lab);
    f.appendChild(input);
    if (list) f.appendChild(list);
    grid.appendChild(f);
  };

  const [resp, respList] = suggestInput("cf-people", responsibleOptions());
  resp.placeholder = "Name";
  resp.maxLength = 128;
  field("responsible", "Responsible", resp, respList);

  const [sector, sectorList] = suggestInput("cf-sectors", sectorOptions());
  sector.placeholder = "e.g. Healthcare";
  sector.maxLength = 60;
  field("sector", "Sector", sector, sectorList);

  const summary = el("input");
  summary.type = "text";
  summary.maxLength = 80;
  summary.placeholder = "A few words on where it stands";
  field("summary", "Summary", summary, null, true);

  wrap.appendChild(grid);
  wrap.appendChild(hint);
  return wrap;
}

/* A company write. The client's copy is patched before the request goes, so a
   panel redrawn mid-save (a stage click straight after typing) shows what was
   typed, not what was there before. */
async function saveCompany(co, fields) {
  Object.assign(co, fields);
  try {
    const res = await api("/company/update", { id: co.id, fields });
    Object.assign(co, res.company);
    return res.company;
  } catch (e) {
    setStatus(e.message, true);
    return null;
  }
}

/* "Saves when you click away", then "Saved" for a moment after each save. */
function saveHint() {
  const hint = el("div", "note-hint save-hint", "Saves when you click away.");
  hint.flash = () => {
    hint.textContent = "Saved.";
    hint.classList.add("ok");
    clearTimeout(hint.timer);
    hint.timer = setTimeout(() => {
      hint.textContent = "Saves when you click away.";
      hint.classList.remove("ok");
    }, 1600);
  };
  return hint;
}

/* The five people to contact: a name, a contact status (anything at all) and
   a red / amber / green. Names start from the people already on the record,
   or the directors ARGUS sent over, and every one can be typed over. The five
   rows save together; saves queue, so a quick click on a colour straight after
   typing cannot land before the typing does. */
function peopleBlock(co) {
  const wrap = el("div", "people");
  wrap.appendChild(el("div", "people-row people-head",
    "<span>Name</span><span>Contact status</span><span>RAG</span>"));
  const rows = (co.people || []).map((p) => ({ ...p }));
  const snap = () => JSON.stringify(rows.map((p) => ({
    name: p.name.trim(), status: p.status.trim(), rag: p.rag })));
  let saved = snap();
  let queue = Promise.resolve();
  const hint = saveHint();
  const commit = () => {
    queue = queue.then(async () => {
      const now = snap();
      if (now === saved) return;
      if (await saveCompany(co, { people: JSON.parse(now) })) {
        saved = now;
        hint.flash();
      }
    });
  };

  rows.forEach((p, i) => {
    const row = el("div", "people-row");
    const text = (key, cls, placeholder, label) => {
      const input = el("input", cls);
      input.type = "text";
      input.value = p[key];
      input.placeholder = placeholder;
      input.setAttribute("aria-label", `${label}, person ${i + 1}`);
      input.oninput = () => { p[key] = input.value; };
      input.onblur = commit;
      input.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); input.blur(); } };
      return input;
    };
    row.appendChild(text("name", "pp-name", "Name", "Name"));
    row.appendChild(text("status", "pp-status", "Contact status", "Contact status"));

    const rag = el("div", "rag");
    rag.setAttribute("role", "group");
    rag.setAttribute("aria-label", `Red, amber or green, person ${i + 1}`);
    const dots = RAG.map((r) => {
      const b = el("button", `rag-dot rag-${r.key}`);
      b.type = "button";
      b.onclick = () => {
        p.rag = p.rag === r.key ? "" : r.key;     // the lit colour again clears it
        paint();
        commit();
      };
      rag.appendChild(b);
      return b;
    });
    const paint = () => RAG.forEach((r, k) => {
      const on = p.rag === r.key;
      dots[k].classList.toggle("on", on);
      dots[k].setAttribute("aria-pressed", on ? "true" : "false");
      dots[k].title = on ? `${r.label}. Click again to clear.` : r.label;
      dots[k].setAttribute("aria-label", r.label);
    });
    paint();
    row.appendChild(rag);
    wrap.appendChild(row);
  });
  wrap.appendChild(hint);
  return wrap;
}

/* One Comments box per company. Enter is a new line here; it saves when you
   click away. */
function commentsBlock(co) {
  const wrap = el("div", "field comments-box");
  const ta = el("textarea");
  ta.value = co.notes || "";
  ta.rows = 5;
  ta.placeholder = "Anything worth knowing about this company.";
  ta.setAttribute("aria-label", "Comments");
  const hint = saveHint();
  let saved = co.notes || "";
  ta.onblur = async () => {
    if (ta.value.trim() === saved.trim()) return;
    const u = await saveCompany(co, { notes: ta.value });
    if (!u) return;
    saved = u.notes;
    hint.flash();
  };
  wrap.appendChild(ta);
  wrap.appendChild(hint);
  return wrap;
}

/* ---------- reference drawer: the library and the play ------------------- */
const HOOK_COLS = [
  { key: "tier", label: "Tier" },
  { key: "cost_area", label: "Cost area" },
  { key: "role_titles", label: "Role titles" },
  { key: "seniority", label: "Seniority" },
  { key: "benchmark", label: "Benchmark to lead with" },
  { key: "why_reachable", label: "Why reachable" },
  { key: "proof_point", label: "Proof point" },
  { key: "sharpness", label: "Sharpness" },
];

function renderDrawer() {
  const host = $("#drawer");
  const title = el("div", null,
    `<h2>Reference</h2><div class="sub">The library and the play. ` +
    `Double-click a library cell to edit it.</div>`);

  sheetShell(host, title, (body) => {
    /* Role & hook library */
    body.appendChild(el("div", "seclabel", `Role &amp; hook library (${(DATA.hooks || []).length} rows)`));
    const wrap = el("div", "tablewrap");
    const table = el("table", "grid");
    const thead = el("thead");
    const hr = el("tr");
    HOOK_COLS.forEach((col) => hr.appendChild(el("th", null, esc(col.label))));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    (DATA.hooks || []).forEach((h) => {
      const tr = el("tr");
      HOOK_COLS.forEach((col) => {
        const td = el("td", "ed wrap" + (col.key === "sharpness" ? ` sharp-${h.sharpness}` : ""),
          esc(h[col.key] || ""));
        td.title = "Double-click to edit";
        td.ondblclick = () => cellEdit(td, h, col.key);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    body.appendChild(wrap);

    /* The play */
    body.appendChild(el("div", "seclabel", "The play"));
    const flow = el("div", "stageflow");
    DATA.stages.forEach((s) =>
      flow.appendChild(el("div", "sf", `<i>${s.num}</i>${esc(s.label)}`)));
    body.appendChild(flow);
    const play = el("div", "playgrid");
    (DATA.playbook || []).forEach((p) => {
      const heading = p.heading || p.title || p[0] || "";
      const text = p.text || p.body || p[1] || "";
      play.appendChild(el("div", "playcard",
        `<h3>${esc(heading)}</h3><p>${esc(text)}</p>`));
    });
    if (!(DATA.playbook || []).length) {
      play.appendChild(el("div", "note-hint", "No playbook content in the database."));
    }
    body.appendChild(play);
    if (DATA.warm_note) {
      body.appendChild(el("div", "hookhint drawer-warm", `<b>Warm expansion</b>${esc(DATA.warm_note)}`));
    }
  });
}

/* ---------- moving companies with a CSV (20 Oct 2026) ----------
   Download this board, or add companies from a file: one downloaded from
   another copy of the tracker, or ARGUS's "Download for the tracker". The
   server only ever adds, so the same file can go in twice. A bulk import is
   the one write that reloads the whole board: it can add dozens of records. */
function renderCsv() {
  const host = $("#csvsheet");
  const title = el("div", null,
    `<h2>Move companies with a CSV</h2><div class="sub">Build a list on one tracker, ` +
    `then bring it into another.</div>`);

  sheetShell(host, title, (body) => {
    body.appendChild(el("div", "seclabel", "Download"));
    body.appendChild(el("p", "csv-text",
      "Every company and contact on this board, one row per contact, with its stage, " +
      "people and comments. It opens in Excel."));
    const dl = el("a", "btn btn-ghost", "Download the board (CSV)");
    dl.href = API + "/export.csv";
    body.appendChild(dl);

    body.appendChild(el("div", "seclabel csv-gap", "Import"));
    body.appendChild(el("p", "csv-text",
      "Adds the companies in a CSV as new cards: a file from another tracker, or ARGUS's " +
      "<b>Download for the tracker</b>. A company already on this board is left as it is, " +
      "so dropping the same file in twice changes nothing."));
    body.appendChild(el("p", "csv-text",
      "ARGUS's <b>all to contact</b> Excel goes in the same way. Only the rows with Yes in " +
      "its <b>Add to live site?</b> column are added."));
    const drop = el("label", "csv-drop",
      `<input type="file" accept=".csv,text/csv,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" hidden>` +
      `<b>Drop a CSV or Excel file here</b><span>or click to choose one</span>`);
    const input = drop.querySelector("input");
    const result = el("div", "csv-result");
    result.setAttribute("role", "status");
    input.onchange = () => { if (input.files[0]) importCsv(input.files[0], drop, result); };
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.classList.remove("over");
      const f = e.dataTransfer.files[0];
      if (f) importCsv(f, drop, result);
    });
    body.appendChild(drop);
    body.appendChild(result);
  });
}

async function importCsv(file, drop, result) {
  drop.classList.add("busy");
  result.className = "csv-result";
  result.textContent = `Reading ${file.name}...`;
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch(API + "/import", { method: "POST", body: form, cache: "no-store" });
    if (res.status === 401) { window.location.href = "/login"; return; }
    let json = {};
    try { json = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok || json.error) throw new Error(json.error || `Import failed (${res.status})`);
    const n = json.added.length;
    const parts = [n
      ? `Added ${n} compan${n === 1 ? "y" : "ies"} (${json.contacts} contact${json.contacts === 1 ? "" : "s"}) as new cards.`
      : (json.pick_column && !json.marked
        ? "No rows say Yes in the Add to live site? column. Put Yes beside the companies to add, save the file and drop it in again."
        : "Nothing new to add.")];
    if (json.already.length) {
      parts.push(`Already on the board, left as they are: ${json.already.map(esc).join(", ")}.`);
    }
    if (json.pick_column && json.marked && json.unmarked) {
      parts.push(`${json.unmarked} row${json.unmarked === 1 ? "" : "s"} without a Yes left out.`);
    }
    if (json.skipped) parts.push(`${json.skipped} row${json.skipped === 1 ? "" : "s"} had no company name and were skipped.`);
    // Fixed words and numbers, plus company names escaped above.
    result.innerHTML = parts.map((p) => `<p>${p}</p>`).join("");
    if (n) await load();
  } catch (e) {
    result.className = "csv-result error";
    result.textContent = e.message;
  } finally {
    drop.classList.remove("busy");
    drop.querySelector("input").value = "";
  }
}

/* The second and last inline editor: a library table cell. */
function cellEdit(td, row, key) {
  if (td.querySelector("input")) return;
  const before = row[key] || "";
  const input = el("input");
  input.type = "text";
  input.value = before;
  td.innerHTML = "";
  td.appendChild(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const value = input.value;
    td.textContent = commit ? value : before;
    if (!commit || value === before) return;
    try {
      const res = await api("/library/update", { id: row.id, fields: { [key]: value } });
      Object.assign(row, res.hook);
    } catch (e) {
      td.textContent = before;
      setStatus(e.message, true);
    }
  };
  input.onblur = () => finish(true);
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  };
}

/* ---------- add contact modal ---------- */
function openAddModal() {
  const overlay = el("div", "modal-overlay");
  const modal = el("div", "modal");
  modal.innerHTML = `<div class="modal-head"><h3>Add a contact</h3></div>`;
  const bodyBox = el("div", "modal-body");
  const grid = el("div", "fieldgrid");

  const mk = (key, label, type, opts, wide) => {
    const w = el("div", "field" + (wide ? " wide" : ""));
    w.appendChild(el("label", null, esc(label)));
    let i;
    if (type === "select") {
      i = el("select");
      if (!opts.length) i.add(new Option("", ""));
      opts.forEach((o) => i.add(new Option(o, o)));
    } else if (type === "textarea") {
      i = el("textarea");
    } else {
      i = el("input");
      i.type = type;
    }
    i.dataset.key = key;
    w.appendChild(i);
    grid.appendChild(w);
    return i;
  };

  // Company as a datalist so the existing accounts are one keystroke away
  const coField = el("div", "field");
  coField.appendChild(el("label", null, "Company"));
  const coInput = el("input");
  coInput.type = "text";
  coInput.dataset.key = "company";
  coInput.setAttribute("list", "co-list");
  const dl = el("datalist");
  dl.id = "co-list";
  (DATA.companies || []).forEach((c) => dl.appendChild(new Option(c.name, c.name)));
  coField.appendChild(coInput);
  coField.appendChild(dl);
  grid.appendChild(coField);

  const priority = mk("priority", "Priority", "select", DATA.priorities);
  priority.value = "Medium";
  // what the card shows: initials, sector tag, summary
  const suggest = (key, label, listId, options, max, placeholder, wide) => {
    const w = el("div", "field" + (wide ? " wide" : ""));
    w.appendChild(el("label", null, esc(label)));
    const [i, dl] = listId ? suggestInput(listId, options) : [el("input"), null];
    i.type = "text";
    i.maxLength = max;
    i.placeholder = placeholder;
    i.dataset.key = key;
    w.appendChild(i);
    if (dl) w.appendChild(dl);
    grid.appendChild(w);
    return i;
  };
  const resp = suggest("responsible", "Responsible", "add-people", responsibleOptions(), 128, "Name");
  const sector = suggest("sector", "Sector", "add-sectors", sectorOptions(), 60,
    "Blank: the company's sector");
  const summary = suggest("summary", "Summary", null, null, 80, "A few words on where it stands", true);
  const costAreas = [...new Set((DATA.hooks || []).map((h) => h.cost_area).filter(Boolean))].sort();
  const cost = mk("cost_area", "Cost area / function", "select", ["", ...costAreas]);
  const role = mk("target_role", "Target role", "text");
  const name = mk("contact_name", "Contact name (if known)", "text");
  const li = mk("linkedin_url", "LinkedIn URL", "text");
  const email = mk("email", "Email", "text");
  const hook = mk("hook", "Wedge / benchmarking hook", "textarea", null, true);
  const nextAct = mk("next_action", "Next action", "text", null, true);
  const nextDate = mk("next_action_date", "Next action date", "date");
  const stage = mk("stage", "Starting stage", "select",
    DATA.stages.filter((s) => s.num <= 3).map((s) => `${s.num}. ${s.label}`));

  // The library tie-in: picking a cost area suggests the roles and the benchmark
  const hint = el("div", "hookhint");
  hint.hidden = true;
  grid.appendChild(hint);
  cost.onchange = () => {
    const h = (DATA.hooks || []).find((x) => x.cost_area === cost.value);
    if (!h) { hint.hidden = true; return; }
    hint.hidden = false;
    hint.innerHTML =
      `<b>From the Role &amp; Hook library (${esc(h.sharpness)} sharpness)</b>` +
      `Roles: ${esc(h.role_titles)}<br>Lead with: ${esc(h.benchmark)}`;
    if (!role.value) role.value = (h.role_titles || "").split(";")[0].trim();
    if (!hook.value) hook.value = h.benchmark || "";
  };

  bodyBox.appendChild(grid);
  modal.appendChild(bodyBox);

  const foot = el("div", "modal-foot");
  const err = el("span", "note-hint");
  const cancel = el("button", "btn btn-ghost", "Cancel");
  const save = el("button", "btn btn-primary", "Add contact");
  foot.appendChild(err);
  foot.appendChild(cancel);
  foot.appendChild(save);
  modal.appendChild(foot);

  const close = () => overlay.remove();
  cancel.onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  modal.querySelector(".modal-head").appendChild(
    Object.assign(el("button", "close", "&times;"), { onclick: close }));

  save.onclick = async () => {
    const payload = { stage: parseInt((stage.value || "1").split(".")[0], 10) };
    [coInput, priority, resp, sector, summary, cost, role, name, li, email, hook, nextAct, nextDate]
      .forEach((i) => { payload[i.dataset.key] = i.value; });
    save.disabled = true;
    try {
      const res = await api("/contact/add", payload);
      close();
      // a new company may have been created by the typed name
      if (!(DATA.companies || []).some((x) => x.id === res.contact.company_id)) {
        DATA = await api("/data");
        render();
      } else {
        applyContact(res.contact);
      }
      openContact(res.contact.id);
      setStatus("Contact added.");
    } catch (e) {
      err.textContent = e.message;
      err.className = "note-hint error";
      save.disabled = false;
    }
  };

  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  coInput.focus();
}

/* The Pipeline row docks under the shared top bar, and the column heads under
   both. Those heights change with the window (the top bar wraps on a narrow
   one), so they are measured into CSS variables rather than hard-coded. */
function trackStickyOffsets() {
  const bar = document.querySelector(".ip-top");
  const head = $("#tk-head");
  const root = document.documentElement.style;
  const set = () => {
    if (bar) root.setProperty("--tk-bar", `${bar.offsetHeight}px`);
    if (head) root.setProperty("--tk-head", `${head.offsetHeight}px`);
  };
  set();
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(set);
    [bar, head].forEach((n) => n && ro.observe(n));
  }
}

/* ---------- wiring ---------- */
document.addEventListener("DOMContentLoaded", () => {
  trackStickyOffsets();
  $("#show-closed").onclick = () => { showClosed = !showClosed; render(); };
  $("#add-contact").onclick = openAddModal;
  $("#open-reference").onclick = () => renderDrawer();
  $("#open-csv").onclick = () => renderCsv();
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || document.querySelector(".modal-overlay")) return;
    const open = document.querySelector(".sheet:not([hidden])");
    if (open) closeSheet(open);
  });
  load();
});
