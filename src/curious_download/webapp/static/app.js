"use strict";

// -- app token (from the launch URL, kept for this browser tab only) ----------

const launchUrl = new URL(location.href);
if (launchUrl.searchParams.get("token")) {
  sessionStorage.setItem("curiousToken", launchUrl.searchParams.get("token"));
  history.replaceState(null, "", "/");
}
const TOKEN = sessionStorage.getItem("curiousToken");

// -- tiny helpers --------------------------------------------------------------

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

class ApiFailure extends Error {
  constructor(message, body, status) {
    super(message);
    this.body = body || {};
    this.status = status;
  }
}

async function api(path, options = {}) {
  const headers = { "X-Curious-Token": TOKEN || "" };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  let body = null;
  try {
    body = await response.json();
  } catch {
    /* non-JSON error page */
  }
  if (!response.ok) {
    if (body && body.badToken) showView("token-error");
    if (body && body.sessionExpired) showLogin("Your Curious login expired — log in again.");
    throw new ApiFailure((body && body.error) || `HTTP ${response.status}`, body, response.status);
  }
  return body;
}

const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });

function localDay(date) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function setError(id, message) {
  const box = $(id);
  box.textContent = message || "";
  box.hidden = !message;
}

// -- views ----------------------------------------------------------------------

function showView(name) {
  for (const view of ["login", "main", "token-error"]) {
    $(`view-${view}`).hidden = view !== name;
  }
  $("session-info").hidden = name !== "main";
}

function showLogin(message) {
  showView("login");
  setError("login-error", message || "");
  $("login-password").value = "";
  $("login-mfa").value = "";
  $("login-mfa-row").hidden = true;
}

// -- state ------------------------------------------------------------------------

const state = {
  workspaces: [],
  answers: { applets: [], applet: null, unlocked: false, participants: [], activities: [], flows: [] },
  dictionary: { applets: [] },
};

// -- checklists ---------------------------------------------------------------------

function fillChecklist(containerId, items, labelOf) {
  const container = $(containerId);
  const options = container.querySelector(".options");
  options.innerHTML = "";
  for (const item of items) {
    const checkbox = el("input", { type: "checkbox" });
    checkbox.dataset.item = JSON.stringify(item);
    options.append(el("label", {}, checkbox, " ", labelOf(item)));
  }
  const search = container.querySelector("input[type=search]");
  if (search) {
    search.value = "";
    search.oninput = () => {
      const query = search.value.toLowerCase();
      for (const row of options.querySelectorAll("label")) {
        row.hidden = Boolean(query) && !row.textContent.toLowerCase().includes(query);
      }
    };
  }
}

function checkedItems(containerId) {
  return [...$(containerId).querySelectorAll(".options input:checked")].map((box) => JSON.parse(box.dataset.item));
}

function bindModeToggle(selectId, listId) {
  $(selectId).onchange = () => {
    $(listId).hidden = $(selectId).value !== "some";
  };
}

// -- date ranges -----------------------------------------------------------------------

function bindDateRange(prefix) {
  $(`${prefix}-daterange`).onchange = () => {
    $(`${prefix}-dates`).hidden = $(`${prefix}-daterange`).value !== "custom";
  };
}

function dateRange(prefix) {
  const mode = $(`${prefix}-daterange`).value;
  if (mode === "all") return { fromDate: null, toDate: null };
  if (mode === "custom") {
    return { fromDate: $(`${prefix}-from`).value || null, toDate: $(`${prefix}-to`).value || null };
  }
  const today = new Date();
  const start = new Date();
  start.setDate(today.getDate() - Number(mode));
  return { fromDate: localDay(start), toDate: localDay(today) };
}

// -- login -----------------------------------------------------------------------------

$("login-form").onsubmit = async (event) => {
  event.preventDefault();
  setError("login-error", "");
  const button = $("login-btn");
  button.disabled = true;
  button.textContent = "Logging in…";
  try {
    const result = await post("/api/login", {
      server: $("login-server").value,
      email: $("login-email").value,
      password: $("login-password").value,
      mfaCode: $("login-mfa-row").hidden ? null : $("login-mfa").value || null,
    });
    if (result.mfaRequired) {
      $("login-mfa-row").hidden = false;
      $("login-mfa").focus();
      setError("login-error", "This account uses two-factor authentication. Enter a code to continue.");
      return;
    }
    await enterMain(result.email, result.server);
  } catch (error) {
    setError("login-error", error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Log in";
  }
};

$("logout-btn").onclick = async () => {
  try {
    await post("/api/logout");
    showLogin();
  } catch (error) {
    alert(error.message);
  }
};

// -- main view setup ---------------------------------------------------------------------

async function enterMain(email, server) {
  $("session-label").textContent = `${email} — ${server}`;
  showView("main");
  state.workspaces = await api("/api/workspaces");
  for (const selectId of ["ans-workspace", "dict-workspace", "counts-workspace"]) {
    const select = $(selectId);
    select.innerHTML = "";
    if (state.workspaces.length !== 1) select.append(el("option", { value: "" }, "Choose a workspace…"));
    for (const workspace of state.workspaces) {
      select.append(el("option", { value: workspace.ownerId }, workspace.name));
    }
  }
  if (state.workspaces.length === 1) {
    await Promise.all([loadAnswersApplets(), loadDictionaryApplets()]);
  }
  refreshJobs();
}

// tabs
$("tabs").onclick = (event) => {
  const button = event.target.closest(".tab");
  if (!button) return;
  for (const tab of document.querySelectorAll(".tab")) tab.classList.toggle("active", tab === button);
  for (const name of ["answers", "dictionary", "counts"]) {
    $(`tab-${name}`).hidden = name !== button.dataset.tab;
  }
};

// -- answers form ----------------------------------------------------------------------------

$("ans-workspace").onchange = loadAnswersApplets;

async function loadAnswersApplets() {
  const ownerId = $("ans-workspace").value;
  resetAnswersApplet();
  const select = $("ans-applet");
  select.innerHTML = "";
  if (!ownerId) {
    select.append(el("option", { value: "" }, "Choose a workspace first"));
    return;
  }
  select.append(el("option", { value: "" }, "Loading…"));
  state.answers.applets = await api(`/api/workspaces/${ownerId}/applets`);
  select.innerHTML = "";
  select.append(el("option", { value: "" }, "Choose an applet…"));
  for (const applet of state.answers.applets) {
    const option = el("option", { value: applet.id }, `${applet.name}  (${applet.role || "no role"})`);
    if (!applet.canExport) {
      option.disabled = true;
      option.textContent += " — no data access";
    }
    select.append(option);
  }
  if (!state.answers.applets.some((applet) => applet.canExport)) {
    select.innerHTML = "";
    select.append(el("option", { value: "" }, "No applets with data access in this workspace"));
  }
}

function resetAnswersApplet() {
  state.answers.applet = null;
  state.answers.unlocked = false;
  $("ans-unlock").hidden = true;
  $("ans-unlock-status").hidden = true;
  $("ans-filters").hidden = true;
  setError("ans-error", "");
}

$("ans-applet").onchange = async () => {
  resetAnswersApplet();
  const appletId = $("ans-applet").value;
  const ownerId = $("ans-workspace").value;
  if (!appletId) return;
  const [details, participants] = await Promise.all([
    api(`/api/applets/${appletId}`),
    api(`/api/workspaces/${ownerId}/applets/${appletId}/participants`).catch(() => []),
  ]);
  state.answers.applet = details;
  state.answers.participants = participants;
  state.answers.unlocked = details.unlocked;

  fillChecklist("ans-participants-list", participants, (p) => p.label);
  const activityItems = [
    ...details.activities.map((activity) => ({ kind: "activity", id: activity.id, name: activity.name })),
    ...details.flows.map((flow) => ({ kind: "flow", id: flow.id, name: `Flow: ${flow.name}` })),
  ];
  fillChecklist("ans-activities-list", activityItems, (item) => item.name);

  if (!details.hasEncryption) {
    showUnlockStatus("This applet has no encryption settings, so its answers cannot be decrypted.", false);
    return;
  }
  if (details.unlocked) {
    showUnlockStatus(`Unlocked: ${details.name}`, true);
    $("ans-filters").hidden = false;
  } else {
    $("ans-unlock").hidden = false;
    $("ans-applet-password").value = "";
    $("ans-applet-password").focus();
  }
};

function showUnlockStatus(message, ok) {
  const status = $("ans-unlock-status");
  status.textContent = message;
  status.className = `unlock-status ${ok ? "ok" : "error"}`;
  status.hidden = false;
}

async function unlockApplet() {
  const appletId = $("ans-applet").value;
  const button = $("ans-unlock-btn");
  button.disabled = true;
  button.textContent = "Checking…";
  try {
    await post(`/api/applets/${appletId}/unlock`, { password: $("ans-applet-password").value });
    state.answers.unlocked = true;
    $("ans-unlock").hidden = true;
    showUnlockStatus(`Unlocked: ${state.answers.applet.name}`, true);
    $("ans-filters").hidden = false;
  } catch (error) {
    showUnlockStatus(error.message, false);
  } finally {
    button.disabled = false;
    button.textContent = "Unlock";
  }
}

$("ans-unlock-btn").onclick = unlockApplet;
$("ans-applet-password").onkeydown = (event) => {
  if (event.key === "Enter") unlockApplet();
};

bindModeToggle("ans-participants-mode", "ans-participants-list");
bindModeToggle("ans-activities-mode", "ans-activities-list");
bindDateRange("ans");

$("ans-run").onclick = async () => {
  setError("ans-error", "");
  const appletId = $("ans-applet").value;
  if (!appletId) return setError("ans-error", "Choose an applet.");
  if (!state.answers.unlocked) return setError("ans-error", "Unlock the applet with its password first.");

  let participants = [];
  if ($("ans-participants-mode").value === "some") {
    participants = checkedItems("ans-participants-list");
    if (!participants.length) return setError("ans-error", "Select at least one participant, or choose “All participants”.");
  }
  let activityIds = [];
  let flowIds = [];
  if ($("ans-activities-mode").value === "some") {
    const chosen = checkedItems("ans-activities-list");
    if (!chosen.length) return setError("ans-error", "Select at least one activity, or choose “All activities”.");
    activityIds = chosen.filter((item) => item.kind === "activity").map((item) => item.id);
    flowIds = chosen.filter((item) => item.kind === "flow").map((item) => item.id);
  }
  const { fromDate, toDate } = dateRange("ans");

  await startJob("ans-run", "ans-error", "/api/jobs/answers", {
    appletId,
    participants: participants.map((p) => ({ subjectId: p.subjectId, secretId: p.secretId })),
    fromDate,
    toDate,
    activityIds,
    flowIds,
    media: $("ans-media").checked,
    journey: $("ans-journey").checked,
    outputDir: $("ans-output").value,
    pageSize: Number($("ans-page-size").value) || 500,
    saveDecrypted: $("ans-save-decrypted").checked,
    subscaleNullWhenSkipped: $("ans-null-when-skipped").checked,
  });
};

// -- dictionary form ---------------------------------------------------------------------------

$("dict-workspace").onchange = loadDictionaryApplets;
bindModeToggle("dict-mode", "dict-applets-list");

async function loadDictionaryApplets() {
  const ownerId = $("dict-workspace").value;
  state.dictionary.applets = ownerId ? await api(`/api/workspaces/${ownerId}/applets`) : [];
  fillChecklist("dict-applets-list", state.dictionary.applets, (applet) => `${applet.name}  (${applet.role || "no role"})`);
}

$("dict-run").onclick = async () => {
  setError("dict-error", "");
  const ownerId = $("dict-workspace").value;
  if (!ownerId) return setError("dict-error", "Choose a workspace.");
  const allApplets = $("dict-mode").value === "all";
  let appletIds = [];
  if (!allApplets) {
    appletIds = checkedItems("dict-applets-list").map((applet) => applet.id);
    if (!appletIds.length) return setError("dict-error", "Select at least one applet, or choose “All applets”.");
  }
  await startJob("dict-run", "dict-error", "/api/jobs/dictionary", {
    ownerId,
    allApplets,
    appletIds,
    currentOnly: $("dict-versions").value === "current",
    outputDir: $("dict-output").value,
  });
};

// -- counts form ---------------------------------------------------------------------------------

bindDateRange("counts");

$("counts-run").onclick = async () => {
  setError("counts-error", "");
  const ownerId = $("counts-workspace").value;
  if (!ownerId) return setError("counts-error", "Choose a workspace.");
  const { fromDate, toDate } = dateRange("counts");
  await startJob("counts-run", "counts-error", "/api/jobs/counts", {
    ownerId,
    fromDate,
    toDate,
    outputDir: $("counts-output").value,
  });
};

// -- jobs --------------------------------------------------------------------------------------------

async function startJob(buttonId, errorId, path, body) {
  const button = $(buttonId);
  button.disabled = true;
  try {
    await post(path, body);
    await refreshJobs();
    $("jobs-panel").scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    setError(errorId, error.message);
  } finally {
    button.disabled = false;
  }
}

let pollTimer = null;

async function refreshJobs() {
  let jobs;
  try {
    jobs = await api("/api/jobs");
  } catch {
    return; // e.g. session expired; the login view is already shown
  }
  renderJobs(jobs);
  const running = jobs.some((job) => job.status === "running");
  if (running && !pollTimer) pollTimer = setInterval(refreshJobs, 800);
  if (!running && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

const KIND_LABELS = { answers: "Answers", dictionary: "Data dictionary", counts: "Participant counts" };

function renderJobs(jobs) {
  const list = $("job-list");
  list.innerHTML = "";
  if (!jobs.length) {
    list.append(el("p", { class: "fineprint" }, "Nothing has run yet. Results will appear here."));
    return;
  }
  for (const job of jobs) list.append(renderJob(job));
}

function renderJob(job) {
  const card = el("div", { class: "job" });
  const time = new Date(job.created).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  card.append(
    el(
      "div",
      { class: "job-head" },
      el("span", { class: "job-title" }, job.title),
      job.status === "running" && job.cancelRequested
        ? el("span", { class: "badge cancelled" }, "cancelling…")
        : el("span", { class: `badge ${job.status}` }, job.status),
      el("span", { class: "job-time" }, `${KIND_LABELS[job.kind] || job.kind} · ${time}`),
    ),
  );

  if (job.status === "running") {
    const text = job.total ? `${job.stage} — ${job.done.toLocaleString()} of ${job.total.toLocaleString()}` : `${job.stage}…`;
    card.append(el("div", { class: "job-stage" }, text));
    if (job.detail) card.append(el("div", { class: "job-detail" }, job.detail));
    const bar = el("div", { class: "bar" });
    const progress = el("div", { class: `progress${job.total ? "" : " indeterminate"}` }, bar);
    if (job.total) bar.style.width = `${Math.min(100, (100 * job.done) / job.total)}%`;
    card.append(progress);
    if (job.cancelRequested) {
      card.append(el("div", { class: "job-detail" }, "Cancelling — waiting for the current request to finish…"));
    } else {
      card.append(
        el("div", { class: "job-buttons" }, el("button", { class: "ghost", onclick: () => cancelJob(job.id) }, "Cancel")),
      );
    }
  }

  if (job.status === "failed") card.append(el("div", { class: "job-error" }, job.error || "Failed."));
  if (job.status === "cancelled") card.append(el("div", { class: "job-stage" }, "Cancelled."));

  if (job.status === "done" && job.result) {
    appendResult(card, job);
    for (const warning of job.warnings) card.append(el("div", { class: "job-warning" }, warning));
    const buttons = el("div", { class: "job-buttons" });
    const revealPath = job.result.mainFile || job.result.folder;
    if (revealPath) {
      buttons.append(el("button", { class: "ghost", onclick: () => reveal(revealPath) }, "Show in Finder"));
    }
    card.append(buttons);
    if (job.result.command) {
      card.append(
        el(
          "details",
          {},
          el("summary", {}, "Command to repeat this in the terminal"),
          el("code", {}, job.result.command),
        ),
      );
    }
  }
  return card;
}

function appendResult(card, job) {
  const result = job.result;
  if (job.kind === "answers") {
    let text = `${result.submissions.toLocaleString()} submission(s) → ${result.rows.toLocaleString()} rows in responses.csv`;
    if (result.media) text += ` · media: ${result.media.saved} of ${result.media.files} file(s) saved`;
    if (result.journey) text += ` · journey: ${result.journey.rows.toLocaleString()} action(s)`;
    card.append(el("div", { class: "job-summary" }, text));
    card.append(el("div", { class: "job-folder" }, result.folder));
  } else if (job.kind === "dictionary") {
    const ok = result.applets - result.failed;
    card.append(
      el("div", { class: "job-summary" }, `${result.files} data dictionar${result.files === 1 ? "y" : "ies"} from ${ok} applet(s)`),
    );
    card.append(el("div", { class: "job-folder" }, result.folder));
  } else if (job.kind === "counts") {
    const range =
      result.fromUtc || result.toUtc
        ? ` · ${result.fromUtc || "beginning"} to ${result.toUtc || "now"} (UTC)`
        : "";
    card.append(el("div", { class: "job-summary" }, `${result.workspace}${range}`));
    card.append(renderCountsTable(result.rows));
    card.append(
      el(
        "p",
        { class: "fineprint" },
        "Submitted: people who completed at least one activity. With data: participants the answers are about — ",
        "they differ only when someone answers about another person (e.g. a parent about a child). ",
        "Reviewer assessments are not counted.",
      ),
    );
    card.append(el("div", { class: "job-folder" }, result.mainFile));
  }
}

function renderCountsTable(rows) {
  const table = el("table", { class: "counts" });
  table.append(
    el(
      "tr",
      {},
      ...["Applet", "Your role", "Submitted", "With data", "Submissions", "Last submission (UTC)", "Note"].map((head) =>
        el("th", {}, head),
      ),
    ),
  );
  for (const row of rows) {
    const number = (value) => el("td", { class: "num" }, value ?? "");
    table.append(
      el(
        "tr",
        {},
        el("td", {}, row.applet_name || ""),
        el("td", {}, row.your_role || ""),
        number(row.participants_submitted),
        number(row.participants_with_data),
        number(row.submissions),
        el("td", {}, (row.last_submission_utc || "").slice(0, 10)),
        el("td", { class: "note" }, row.note || ""),
      ),
    );
  }
  return table;
}

async function cancelJob(jobId) {
  try {
    await post(`/api/jobs/${jobId}/cancel`);
    await refreshJobs();
  } catch {
    /* the job may already be done */
  }
}

async function reveal(path) {
  try {
    await post("/api/reveal", { path });
  } catch (error) {
    alert(error.message);
  }
}

// -- folder picker --------------------------------------------------------------------------------------

let browseTarget = null;
let browsePath = null;

document.querySelectorAll("[data-browse]").forEach((button) => {
  button.onclick = () => openBrowse(button.dataset.browse);
});

async function openBrowse(inputId) {
  browseTarget = inputId;
  $("browse-modal").hidden = false;
  try {
    await loadBrowse($(inputId).value || null);
  } catch {
    await loadBrowse(null);
  }
}

async function loadBrowse(path) {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  const listing = await api(`/api/browse${query}`);
  browsePath = listing.path;
  $("browse-path").textContent = listing.path;
  const list = $("browse-list");
  list.innerHTML = "";
  if (listing.parent) {
    list.append(el("button", { type: "button", onclick: () => loadBrowse(listing.parent) }, "⬑ Up one level"));
  }
  if (!listing.dirs.length) {
    list.append(el("div", { class: "empty" }, "No subfolders."));
  }
  for (const name of listing.dirs) {
    list.append(
      el("button", { type: "button", onclick: () => loadBrowse(`${listing.path}/${name}`) }, `📁 ${name}`),
    );
  }
}

$("browse-cancel").onclick = () => {
  $("browse-modal").hidden = true;
};
$("browse-choose").onclick = () => {
  if (browseTarget && browsePath) $(browseTarget).value = browsePath;
  $("browse-modal").hidden = true;
};
$("browse-modal").onclick = (event) => {
  if (event.target === $("browse-modal")) $("browse-modal").hidden = true;
};

// -- boot ---------------------------------------------------------------------------------------------------

async function init() {
  if (!TOKEN) {
    showView("token-error");
    return;
  }
  let snapshot;
  try {
    snapshot = await api("/api/state");
  } catch {
    return; // the token-error view is already shown by api()
  }
  $("login-server").value = snapshot.settings.server || "";
  $("login-email").value = snapshot.settings.email || "";
  for (const input of document.querySelectorAll(".output-input")) input.value = snapshot.settings.outputDir;
  if (snapshot.loggedIn) {
    await enterMain(snapshot.email, snapshot.server);
  } else {
    showView("login");
  }
}

init();
