// Customer portal - Jamie-style meeting notes (list + overview + action items).
const $ = (s) => document.querySelector(s);

const state = { meetings: [], filtered: [], currentId: null, currentDetail: null, overviewView: "summary" };

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function initials(name) {
  const parts = String(name || "").trim().split(/\s+/);
  return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || "?";
}

function fmtDate(iso, long) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleDateString(undefined,
    long ? { weekday: "short", day: "numeric", month: "long", year: "numeric" }
         : { day: "numeric", month: "short", year: "numeric" });
}

/* ---------- Meetings list ---------- */
async function loadMeetings() {
  try {
    const res = await fetch("/api/portal/meetings");
    const json = await res.json();
    if (json.error) throw new Error(json.error);
    state.meetings = json.meetings || [];
    state.filtered = state.meetings;
    renderMeetingList();
    if (state.meetings.length) {
      selectMeeting(state.meetings[0].id, "Latest meeting");
    } else {
      $("#meetingList").innerHTML = `<li class="meeting-empty">${escapeHtml(json.note || "No meetings to show yet.")}</li>`;
    }
  } catch (e) {
    const st = $("#status"); st.className = "status error"; st.textContent = "Could not load meetings: " + e.message;
    $("#meetingList").innerHTML = "";
  }
}

function renderMeetingList() {
  const list = $("#meetingList");
  if (!state.filtered.length) {
    list.innerHTML = `<li class="meeting-empty">No meetings match your search.</li>`;
    return;
  }
  list.innerHTML = "";
  state.filtered.forEach((m) => {
    const li = document.createElement("li");
    li.className = "meeting-item" + (m.id === state.currentId ? " active" : "");
    li.dataset.id = m.id;
    li.innerHTML = `<div class="m-title">${escapeHtml(m.title)}</div>
                    <div class="m-date">${escapeHtml(fmtDate(m.startTime))}</div>`;
    li.addEventListener("click", () => selectMeeting(m.id));
    list.appendChild(li);
  });
}

async function selectMeeting(id, badge) {
  state.currentId = id;
  document.querySelectorAll(".meeting-item").forEach((el) =>
    el.classList.toggle("active", el.dataset.id === id));
  const isFirst = state.meetings.length && state.meetings[0].id === id;
  const theBadge = badge || (isFirst ? "Latest meeting" : "Meeting");
  $("#meetingDetail").classList.add("hidden");
  $("#detailLoading").classList.remove("hidden");
  try {
    const res = await fetch("/api/portal/meeting/" + encodeURIComponent(id));
    const json = await res.json();
    if (json.error) throw new Error(json.error);
    renderDetail(json, theBadge);
  } catch (e) {
    $("#detailLoading").classList.add("hidden");
    const st = $("#status"); st.classList.remove("hidden");
    st.className = "status error"; st.textContent = "Could not load this meeting: " + e.message;
  }
}

/* ---------- Detail ---------- */
function renderDetail(detail, badge) {
  state.currentDetail = detail;
  $("#detailLoading").classList.add("hidden");
  $("#status").classList.add("hidden");
  $("#meetingDetail").classList.remove("hidden");

  $("#detailBadge").textContent = badge || "Meeting";
  $("#detailTitle").textContent = detail.title || "Untitled meeting";
  $("#detailMeta").innerHTML = `<span>📅 ${escapeHtml(fmtDate(detail.startTime, true))}</span>`;

  const tasks = detail.tasks || [];
  $("#actionCount").textContent = tasks.filter((t) => !t.completed).length + " open / " + tasks.length + " total";
  const ai = $("#actionItems");
  ai.innerHTML = tasks.length
    ? tasks.map(actionItemHtml).join("")
    : '<li class="no-actions">No action items were captured for this meeting.</li>';

  renderOverview();
}

function actionItemHtml(t) {
  const who = t.assignee || "";
  const assignee = who
    ? `<span class="action-assignee"><span class="avatar">${escapeHtml(initials(who))}</span>${escapeHtml(who)}</span>`
    : "";
  return `<li class="action-item ${t.completed ? "done" : ""}">
      <div class="action-body">
        <div class="action-text">${escapeHtml(t.text || "")}</div>
        ${assignee}
      </div>
    </li>`;
}

function renderOverview() {
  const d = state.currentDetail;
  if (!d) return;
  const box = $("#overview");
  const html = d.summaryHtml || "";
  if (!html) {
    box.innerHTML = '<p class="no-actions">No overview is available for this meeting yet.</p>';
    return;
  }
  const clean = stripActionSections(html);
  box.innerHTML = state.overviewView === "summary" ? extractSummary(clean) : clean;
}

// Compact view: keep just the Executive summary / summary section.
function extractSummary(html) {
  const tmp = document.createElement("div");
  tmp.innerHTML = html;
  const out = [];
  let capturing = false, sawHeading = false;
  for (const node of Array.from(tmp.childNodes)) {
    if (node.nodeName === "H1" || node.nodeName === "H2") {
      sawHeading = true;
      const text = (node.textContent || "").toLowerCase();
      capturing = text.includes("executive") || (text.includes("summary") && !text.includes("full"));
      if (text.includes("full summary") || text.includes("attendees") || text.includes("transcript")) capturing = false;
    }
    if (capturing) out.push(node.outerHTML || node.textContent);
  }
  return (sawHeading && out.length) ? out.join("") : html;
}

// Remove "Action items" / "Next steps" / "Follow ups" sections from the overview
// so it doesn't duplicate the Key action items panel below.
const ACTION_HEADING_RE = /(action items?|next steps?|follow[\s-]?ups?)/i;
function stripActionSections(html) {
  if (!html) return html;
  const tmp = document.createElement("div");
  tmp.innerHTML = html;
  Array.from(tmp.querySelectorAll("h1,h2,h3,h4,h5")).forEach((h) => {
    if (!ACTION_HEADING_RE.test(h.textContent || "")) return;
    const remove = [h];
    let node = h.nextElementSibling;
    while (node && !/^H[1-5]$/.test(node.nodeName)) { remove.push(node); node = node.nextElementSibling; }
    remove.forEach((n) => n.remove());
  });
  Array.from(tmp.querySelectorAll("li")).forEach((li) => {
    if (!li.querySelector("ul, ol")) return;
    const label = Array.from(li.childNodes)
      .filter((n) => n.nodeName !== "UL" && n.nodeName !== "OL")
      .map((n) => n.textContent).join(" ");
    if (ACTION_HEADING_RE.test(label)) li.remove();
  });
  return tmp.innerHTML;
}

/* ---------- Controls ---------- */
document.querySelectorAll(".vt").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll(".vt").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state.overviewView = btn.dataset.view;
  renderOverview();
}));

$("#searchBox").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  state.filtered = q ? state.meetings.filter((m) => (m.title || "").toLowerCase().includes(q)) : state.meetings;
  renderMeetingList();
});

loadMeetings();
