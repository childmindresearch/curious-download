"use strict";

// The Dashboard tab. Uses the helpers app.js defines ($, el, api, post, setError, refreshJobs).

(() => {
  const ROW_HEIGHT = 30;
  const STATUS_LABEL = {
    done: "Completed",
    late: "Completed outside the time window",
    partial: "Partly done",
    missed: "Missed",
    due: "Open now",
    upcoming: "Upcoming",
    extra: "Done, not scheduled",
    none: "Nothing scheduled",
    na: "Not in this applet",
  };
  const CROSS_SYMBOL = { done: "✓", partial: "◐", missed: "✕", due: "○", upcoming: "·", none: "–", extra: "✓" };

  const dash = {
    workspaces: [],
    studies: [],
    current: null, // the selected study payload from the server
    grid: null,
    rows: [], // rows after search and sort
    entityChoice: null, // null = all, else an array of entity keys
    focus: null,
    pollTimer: null,
    editing: null, // the config being edited (null when creating)
    editorApplets: [],
    windowStart: -1,
    windowEnd: -1,
    shown: false,
  };

  // -- small helpers ---------------------------------------------------------------

  const pct = (value) => (value == null ? "–" : `${Math.round(value * 100)}%`);
  const n = (value) => (value == null ? "–" : Number(value).toLocaleString());

  function when(text) {
    if (!text) return "";
    const [day, time] = text.split("T");
    const date = new Date(`${day}T${time || "00:00"}`);
    return `${date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })}${time ? ` ${time}` : ""}`;
  }

  function parseLines(text, separator = ",") {
    const result = {};
    for (const line of (text || "").split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const index = trimmed.indexOf(separator);
      if (index < 0) throw new Error(`Each line needs a comma: “${trimmed}”`);
      const key = trimmed.slice(0, index).trim();
      const value = trimmed.slice(index + 1).trim();
      if (key && value) result[key] = value;
    }
    return result;
  }

  function formatLines(map) {
    return Object.entries(map || {})
      .map(([key, value]) => `${key}, ${value}`)
      .join("\n");
  }

  function download(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = el("a", { href: url, download: filename });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // -- entry points --------------------------------------------------------------------

  async function onLogin(workspaces) {
    dash.workspaces = workspaces;
    dash.current = null;
    dash.grid = null;
    const select = $("dash-workspace");
    select.innerHTML = "";
    if (workspaces.length !== 1) select.append(el("option", { value: "" }, "Choose a workspace…"));
    for (const workspace of workspaces) select.append(el("option", { value: workspace.ownerId }, workspace.name));
    if (dash.shown) await loadStudies();
  }

  async function show() {
    dash.shown = true;
    if (!dash.studies.length) await loadStudies();
  }

  // -- studies ---------------------------------------------------------------------------

  async function loadStudies(selectId) {
    setError("dash-error", "");
    try {
      dash.studies = (await api("/api/dashboard/studies")).filter((study) => study.sameServer);
    } catch (error) {
      setError("dash-error", error.message);
      return;
    }
    const select = $("dash-study");
    const wanted = selectId || (dash.current && dash.current.id) || localStorage.getItem("dashStudy");
    select.innerHTML = "";
    if (!dash.studies.length) {
      select.append(el("option", { value: "" }, "No studies yet — create one"));
    } else {
      for (const study of dash.studies) {
        const label = `${study.name}${study.loaded ? " · loaded" : ""}`;
        select.append(el("option", { value: study.id }, label));
      }
      select.value = dash.studies.some((study) => study.id === wanted) ? wanted : dash.studies[0].id;
    }
    await selectStudy();
  }

  async function selectStudy() {
    const id = $("dash-study").value;
    dash.current = dash.studies.find((study) => study.id === id) || null;
    dash.focus = null;
    dash.entityChoice = dash.current && dash.current.entities ? dash.current.entities : null;
    if (dash.current) localStorage.setItem("dashStudy", dash.current.id);
    $("dash-edit").disabled = !dash.current;
    $("dash-delete").disabled = !dash.current;
    $("dash-load").disabled = !dash.current;
    $("dash-load").textContent = dash.current && dash.current.loaded ? "Reload data" : "Load data";
    $("dash-editor").hidden = true;
    if (!dash.current) {
      $("dash-board").hidden = true;
      if (!dash.studies.length) openEditor(null);
      return;
    }
    if (dash.current.loadingJobId) {
      pollLoad(dash.current.loadingJobId);
      return;
    }
    if (dash.current.loaded) {
      $("dash-board").hidden = false;
      applyConfigToControls();
      await loadView();
    } else {
      $("dash-board").hidden = true;
      setError("dash-error", "");
      $("dash-loading").hidden = true;
      // A gentle nudge instead of an empty page.
      $("dash-error").hidden = false;
      $("dash-error").className = "unlock-status ok";
      $("dash-error").textContent = `“${dash.current.name}” is set up. Click “Load data” to read its schedule and completions from Curious.`;
    }
  }

  $("dash-study").onchange = () => {
    $("dash-error").className = "error";
    selectStudy();
  };

  // -- loading ------------------------------------------------------------------------------

  $("dash-load").onclick = async () => {
    if (!dash.current) return;
    $("dash-error").className = "error";
    setError("dash-error", "");
    try {
      const { jobId } = await post(`/api/dashboard/studies/${dash.current.id}/load`);
      refreshJobs();
      pollLoad(jobId);
    } catch (error) {
      setError("dash-error", error.message);
    }
  };

  function pollLoad(jobId) {
    clearInterval(dash.pollTimer);
    $("dash-loading").hidden = false;
    $("dash-board").hidden = true;
    $("dash-load").disabled = true;
    const tick = async () => {
      let job;
      try {
        job = await api(`/api/jobs/${jobId}`);
      } catch {
        clearInterval(dash.pollTimer);
        return;
      }
      $("dash-loading-stage").textContent = job.total
        ? `${job.stage} — ${job.done.toLocaleString()} of ${job.total.toLocaleString()}`
        : `${job.stage}…`;
      $("dash-loading-detail").textContent = job.detail || "";
      const progress = $("dash-loading-progress");
      progress.classList.toggle("indeterminate", !job.total);
      progress.firstElementChild.style.width = job.total ? `${Math.min(100, (100 * job.done) / job.total)}%` : "";
      if (job.status === "running") return;
      clearInterval(dash.pollTimer);
      $("dash-loading").hidden = true;
      $("dash-load").disabled = false;
      if (job.status === "done") {
        await loadStudies(dash.current && dash.current.id);
      } else {
        $("dash-error").className = "error";
        const where = job.stage && job.stage !== "Starting" ? ` (while: ${job.stage}${job.detail ? ` — ${job.detail}` : ""})` : "";
        setError("dash-error", job.status === "cancelled" ? "Loading was cancelled." : `${job.error || "Loading failed."}${where}`);
      }
      refreshJobs();
    };
    tick();
    dash.pollTimer = setInterval(tick, 700);
  }

  // -- editor ---------------------------------------------------------------------------------

  $("dash-new").onclick = () => openEditor(null);
  $("dash-edit").onclick = () => dash.current && openEditor(dash.current);
  $("dash-cancel").onclick = () => {
    $("dash-editor").hidden = true;
    if (dash.current && dash.current.loaded) $("dash-board").hidden = false;
  };

  async function openEditor(config) {
    dash.editing = config;
    $("dash-editor").hidden = false;
    $("dash-board").hidden = true;
    $("dash-error").hidden = true;
    setError("dash-editor-error", "");
    $("dash-editor-title").textContent = config ? `Edit “${config.name}”` : "New study";
    $("dash-name").value = config ? config.name : "";
    const type = config ? config.view : "fixed";
    for (const radio of document.querySelectorAll("input[name=dash-type]")) radio.checked = radio.value === type;
    $("dash-start-rule").value = config ? config.start_rule : "first_completion";
    $("dash-study-days").value = config ? config.study_days : 28;
    $("dash-manual-starts").value = config ? formatLines(config.manual_starts) : "";
    $("dash-aliases").value = config ? formatLines(config.aliases) : "";
    $("dash-late-counts").checked = config ? config.late_counts_as_done : true;
    $("dash-count-from-joined").checked = config ? config.count_from_joined : true;
    $("dash-include-removed").checked = config ? config.include_removed : false;
    $("dash-data-from").value = (config && config.data_from) || "";
    const owner = config && config.applets.length ? config.applets[0].ownerId : $("dash-workspace").value;
    if (owner) $("dash-workspace").value = owner;
    await loadEditorApplets(config ? config.applets.map((applet) => applet.appletId) : []);
    toggleTypeOptions();
    $("dash-name").focus();
  }

  $("dash-workspace").onchange = () => loadEditorApplets([]);

  async function loadEditorApplets(checkedIds) {
    const ownerId = $("dash-workspace").value;
    const container = $("dash-applets-list");
    const options = container.querySelector(".options");
    options.innerHTML = "";
    dash.editorApplets = [];
    if (!ownerId) {
      options.append(el("div", { class: "fineprint" }, "Choose a workspace first."));
      return;
    }
    options.append(el("div", { class: "fineprint" }, "Loading…"));
    try {
      dash.editorApplets = await api(`/api/workspaces/${ownerId}/applets`);
    } catch (error) {
      options.innerHTML = "";
      options.append(el("div", { class: "fineprint" }, error.message));
      return;
    }
    options.innerHTML = "";
    for (const applet of dash.editorApplets) {
      const checkbox = el("input", { type: "checkbox", value: applet.id });
      checkbox.checked = checkedIds.includes(applet.id);
      checkbox.onchange = fillStartEntities;
      const label = el("label", {}, checkbox, " ", `${applet.name}  (${applet.role || "no role"})`);
      if (!applet.canExport) {
        checkbox.disabled = true;
        label.append(el("small", { class: "fineprint" }, " — no data access (needs owner, manager, or reviewer)"));
      }
      options.append(label);
    }
    const search = container.querySelector("input[type=search]");
    search.oninput = () => {
      const query = search.value.toLowerCase();
      for (const row of options.querySelectorAll("label")) {
        row.hidden = Boolean(query) && !row.textContent.toLowerCase().includes(query);
      }
    };
    await fillStartEntities();
  }

  function checkedApplets() {
    return [...$("dash-applets-list").querySelectorAll("input:checked")].map((box) => {
      const applet = dash.editorApplets.find((item) => item.id === box.value);
      return { ownerId: $("dash-workspace").value, appletId: box.value, name: applet ? applet.name : box.value };
    });
  }

  async function fillStartEntities() {
    const select = $("dash-start-entity");
    const previous = (dash.editing && dash.editing.start_entity) || select.value;
    select.innerHTML = "";
    select.append(el("option", { value: "" }, "any activity"));
    const applets = checkedApplets();
    const details = await Promise.all(applets.map((applet) => api(`/api/applets/${applet.appletId}`).catch(() => null)));
    details.forEach((detail, index) => {
      if (!detail) return;
      const prefix = applets.length > 1 ? `${applets[index].name}: ` : "";
      for (const activity of detail.activities) {
        select.append(el("option", { value: `${detail.id}:activity:${activity.id}` }, `${prefix}${activity.name}`));
      }
      for (const flow of detail.flows) {
        select.append(el("option", { value: `${detail.id}:flow:${flow.id}` }, `${prefix}Flow: ${flow.name}`));
      }
    });
    if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  }

  function toggleTypeOptions() {
    const type = document.querySelector("input[name=dash-type]:checked").value;
    $("dash-opts-rolling").hidden = type !== "rolling";
    const rule = $("dash-start-rule").value;
    $("dash-manual-row").hidden = rule !== "manual";
    $("dash-start-entity-row").hidden = rule !== "first_completion";
  }
  $("dash-type").onchange = toggleTypeOptions;
  $("dash-start-rule").onchange = toggleTypeOptions;

  $("dash-save").onclick = async () => {
    setError("dash-editor-error", "");
    let manualStarts;
    let aliases;
    try {
      manualStarts = parseLines($("dash-manual-starts").value);
      aliases = parseLines($("dash-aliases").value);
    } catch (error) {
      return setError("dash-editor-error", error.message);
    }
    const applets = checkedApplets();
    if (!applets.length) return setError("dash-editor-error", "Tick at least one applet.");
    const body = {
      id: dash.editing ? dash.editing.id : undefined,
      name: $("dash-name").value.trim(),
      view: document.querySelector("input[name=dash-type]:checked").value,
      applets,
      entities: dash.editing ? dash.editing.entities : null,
      start_rule: $("dash-start-rule").value,
      start_entity: $("dash-start-entity").value || null,
      study_days: Number($("dash-study-days").value) || 28,
      manual_starts: manualStarts,
      aliases,
      late_counts_as_done: $("dash-late-counts").checked,
      count_from_joined: $("dash-count-from-joined").checked,
      include_removed: $("dash-include-removed").checked,
      data_from: $("dash-data-from").value || null,
      granularity: dash.editing ? dash.editing.granularity : "day",
      created: dash.editing ? dash.editing.created : "",
    };
    if (body.id === undefined) delete body.id;
    const button = $("dash-save");
    button.disabled = true;
    try {
      const saved = await post("/api/dashboard/studies", body);
      $("dash-editor").hidden = true;
      await loadStudies(saved.id);
      if (!saved.loaded) $("dash-load").click();
    } catch (error) {
      setError("dash-editor-error", error.message);
    } finally {
      button.disabled = false;
    }
  };

  $("dash-delete").onclick = async () => {
    if (!dash.current) return;
    if (!confirm(`Delete the study “${dash.current.name}”? Only the dashboard set-up is removed; nothing in Curious changes.`)) return;
    try {
      await api(`/api/dashboard/studies/${dash.current.id}`, { method: "DELETE" });
      dash.current = null;
      await loadStudies();
    } catch (error) {
      setError("dash-error", error.message);
    }
  };

  // -- view controls -----------------------------------------------------------------------------

  function applyConfigToControls() {
    const config = dash.current;
    $("dash-view").value = config.view;
    $("dash-granularity").value = config.granularity || "day";
    $("dash-view-days").value = config.study_days;
    $("dash-view-late").checked = config.late_counts_as_done;
    $("dash-first").value = "";
    $("dash-last").value = "";
    $("dash-search").value = "";
    $("dash-sort").value = "label";
    $("dash-entities-mode").value = dash.entityChoice ? "some" : "all";
    fillEntityChecklist();
    syncControlVisibility();
  }

  function syncControlVisibility() {
    const view = $("dash-view").value;
    for (const node of document.querySelectorAll(".dash-range")) node.hidden = !(view === "fixed" || view === "individual");
    for (const node of document.querySelectorAll(".dash-gran")) node.hidden = view === "cross";
    for (const node of document.querySelectorAll(".dash-days")) node.hidden = view !== "rolling";
    document.querySelector(".dash-late-toggle").hidden = view === "cross";
  }

  function fillEntityChecklist() {
    const container = $("dash-entities-list");
    const options = container.querySelector(".options");
    options.innerHTML = "";
    const entities = (dash.current.data && dash.current.data.entities) || [];
    const multi = new Set(entities.map((entity) => entity.applet)).size > 1;
    for (const entity of entities) {
      const checkbox = el("input", { type: "checkbox", value: entity.key });
      checkbox.checked = !dash.entityChoice || dash.entityChoice.includes(entity.key);
      options.append(el("label", {}, checkbox, " ", multi ? `${entity.applet}: ${entity.label}` : entity.label));
    }
    container.hidden = $("dash-entities-mode").value !== "some";
    const search = container.querySelector("input[type=search]");
    search.oninput = () => {
      const query = search.value.toLowerCase();
      for (const row of options.querySelectorAll("label")) {
        row.hidden = Boolean(query) && !row.textContent.toLowerCase().includes(query);
      }
    };
  }

  $("dash-entities-mode").onchange = () => {
    const some = $("dash-entities-mode").value === "some";
    $("dash-entities-list").hidden = !some;
    if (!some) {
      dash.entityChoice = null;
      loadView();
    }
  };
  $("dash-entities-apply").onclick = () => {
    dash.entityChoice = [...$("dash-entities-list").querySelectorAll(".options input:checked")].map((box) => box.value);
    loadView();
  };

  $("dash-view").onchange = () => {
    dash.focus = null;
    syncControlVisibility();
    loadView();
  };
  for (const id of ["dash-first", "dash-last", "dash-granularity", "dash-view-days", "dash-view-late"]) {
    $(id).onchange = () => loadView();
  }
  $("dash-sort").onchange = () => renderRows();
  $("dash-search").oninput = () => renderRows();
  $("dash-focus-clear").onclick = () => {
    dash.focus = null;
    loadView();
  };

  function viewQuery() {
    const view = $("dash-view").value;
    const params = new URLSearchParams();
    params.set("view", view);
    if (view === "fixed" || view === "individual") {
      if ($("dash-first").value) params.set("first", $("dash-first").value);
      if ($("dash-last").value) params.set("last", $("dash-last").value);
    }
    if (view !== "cross") {
      params.set("granularity", $("dash-granularity").value);
      params.set("lateCountsAsDone", $("dash-view-late").checked ? "true" : "false");
    }
    if (view === "rolling") params.set("studyDays", $("dash-view-days").value || "28");
    if (dash.entityChoice) params.set("entities", dash.entityChoice.join(","));
    if (view === "individual" && dash.focus) params.set("focus", dash.focus);
    return params;
  }

  async function loadView() {
    if (!dash.current || !dash.current.loaded) return;
    $("dash-error").className = "error";
    setError("dash-error", "");
    const wrap = $("dash-grid-wrap");
    wrap.classList.add("loading");
    try {
      dash.grid = await api(`/api/dashboard/studies/${dash.current.id}/view?${viewQuery()}`);
    } catch (error) {
      setError("dash-error", error.message);
      return;
    } finally {
      wrap.classList.remove("loading");
    }
    const params = dash.grid.params;
    if (!$("dash-first").value) $("dash-first").value = params.first;
    if (!$("dash-last").value) $("dash-last").value = params.last;
    $("dash-focus").hidden = !params.focus;
    $("dash-focus-label").textContent = params.focus ? `Timeline of ${params.focusLabel || params.focus}` : "";
    render();
  }

  // -- rendering ------------------------------------------------------------------------------------

  function render() {
    renderStats();
    renderLegend();
    renderNotes();
    renderTableFrame();
    renderRows();
  }

  function renderStats() {
    const grid = dash.grid;
    const overall = grid.overall || {};
    const stats = $("dash-stats");
    stats.innerHTML = "";
    const tile = (value, label, sub) =>
      el("div", { class: "stat" }, el("div", { class: "stat-value" }, value), el("div", { class: "stat-label" }, label), sub ? el("div", { class: "stat-sub" }, sub) : "");
    stats.append(tile(n(overall.participants), grid.view === "individual" && grid.params.focus ? "activities" : "participants"));
    if (grid.view === "cross") {
      stats.append(tile(pct(overall.pct), "done", `${n(overall.d)} of ${n(overall.e)} activity × participant`));
    } else {
      const closed = (overall.d || 0) + (overall.l || 0) + (overall.p || 0) + (overall.m || 0);
      stats.append(tile(pct(overall.pct), "adherence so far", `${n(overall.d)} of ${n(closed)} closed occurrences done`));
      stats.append(tile(n(overall.m), "missed"));
      stats.append(tile(n(overall.u), "open or upcoming", "in the shown range"));
      if (overall.x) stats.append(tile(n(overall.x), "done without a schedule"));
    }
  }

  function renderLegend() {
    const legend = $("dash-legend");
    legend.innerHTML = "";
    for (const item of dash.grid.legend) {
      const symbol = dash.grid.view === "cross" ? CROSS_SYMBOL[item.status] || "" : "";
      legend.append(el("span", { class: "legend-item" }, el("span", { class: `swatch s-${item.status}` }, symbol), item.label));
    }
    legend.append(el("span", { class: "legend-item" }, el("span", { class: "swatch s-na" }), "Not in this applet"));
  }

  function renderNotes() {
    const notes = $("dash-notes");
    notes.innerHTML = "";
    for (const note of dash.grid.notes || []) notes.append(el("div", { class: "job-warning" }, note));
  }

  function renderTableFrame() {
    const grid = dash.grid;
    const table = $("dash-grid");
    table.innerHTML = "";
    table.className = `dash-grid view-${grid.view}${grid.groups.length ? " has-groups" : ""}`;
    const head = el("thead");
    const corner = el("th", { class: "row-head" }, grid.view === "individual" && grid.params.focus ? "Activity" : "Participant");
    if (grid.groups.length) {
      corner.rowSpan = 2;
      const groupRow = el("tr", { class: "hdr-group" }, corner);
      for (const group of grid.groups) {
        const th = el("th", { class: `group${group.today ? " today" : ""}`, title: `${group.label} ${group.sub || ""}` }, group.label);
        th.colSpan = group.span;
        if (group.sub) th.append(el("small", {}, group.sub));
        groupRow.append(th);
      }
      head.append(groupRow);
    }
    const colRow = el("tr", { class: "hdr-col" });
    if (!grid.groups.length) colRow.append(corner);
    for (const column of grid.columns) {
      const title = [column.label, column.sub, column.applet].filter(Boolean).join(" · ");
      const prefix = column.applet ? `${column.applet}: ` : "";
      const text = prefix && column.label.startsWith(prefix) ? column.label.slice(prefix.length) : column.label;
      const th = el("th", { class: `col${column.today ? " today" : ""}`, title }, el("span", { class: "rot" }, text));
      if (prefix && text !== column.label) th.append(el("span", { class: "rot rot-sub" }, column.applet));
      colRow.append(th);
    }
    head.append(colRow);
    table.append(head, el("tbody"));

    const foot = el("tfoot");
    const totalLabel = grid.view === "individual" && grid.params.focus ? "All activities" : "All participants";
    const footRow = el("tr", {}, el("th", { class: "row-head" }, el("div", { class: "rh-label" }, totalLabel, el("span", { class: "rh-pct" }, pct(grid.overall.pct)))));
    grid.totals.forEach((cell, index) => footRow.append(renderCell(cell, grid.columns[index], null, true)));
    foot.append(footRow);
    table.append(foot);
    $("dash-grid-wrap").onscroll = () => renderBody(false);
  }

  function compareRows(a, b) {
    const mode = $("dash-sort").value;
    if (mode === "pct-asc" || mode === "pct-desc") {
      const pa = a.summary.pct;
      const pb = b.summary.pct;
      if (pa == null && pb == null) return a.label.localeCompare(b.label);
      if (pa == null) return 1;
      if (pb == null) return -1;
      return mode === "pct-asc" ? pa - pb : pb - pa;
    }
    if (mode === "last") {
      const la = a.summary.last || "";
      const lb = b.summary.last || "";
      if (la === lb) return a.label.localeCompare(b.label);
      if (!la) return -1;
      if (!lb) return 1;
      return la.localeCompare(lb);
    }
    return a.label.localeCompare(b.label, undefined, { numeric: true });
  }

  function renderRows() {
    const query = $("dash-search").value.trim().toLowerCase();
    dash.rows = dash.grid.rows
      .filter((row) => !query || `${row.label} ${row.sub || ""}`.toLowerCase().includes(query))
      .sort(compareRows);
    dash.windowStart = -1;
    $("dash-count").textContent = `${dash.rows.length} of ${dash.grid.rows.length} row(s) · ${dash.grid.columns.length} column(s)`;
    renderBody(true);
  }

  function renderBody(force) {
    const wrap = $("dash-grid-wrap");
    const body = $("dash-grid").querySelector("tbody");
    if (!body) return;
    const total = dash.rows.length;
    const start = Math.max(0, Math.floor(wrap.scrollTop / ROW_HEIGHT) - 6);
    const end = Math.min(total, Math.ceil((wrap.scrollTop + wrap.clientHeight) / ROW_HEIGHT) + 6);
    if (!force && start === dash.windowStart && end === dash.windowEnd) return;
    dash.windowStart = start;
    dash.windowEnd = end;
    body.innerHTML = "";
    const span = dash.grid.columns.length + 1;
    if (!total) {
      const empty = el("td", { class: "dash-empty" }, dash.grid.rows.length ? "No participant matches the search." : "No participants.");
      empty.colSpan = span;
      body.append(el("tr", {}, empty));
      return;
    }
    if (start > 0) body.append(spacer(start * ROW_HEIGHT, span));
    for (let index = start; index < end; index++) body.append(renderRow(dash.rows[index]));
    if (end < total) body.append(spacer((total - end) * ROW_HEIGHT, span));
  }

  function spacer(height, span) {
    const cell = el("td", { class: "spacer" });
    cell.colSpan = span;
    cell.style.height = `${height}px`;
    return el("tr", {}, cell);
  }

  function renderRow(row) {
    const grid = dash.grid;
    const tr = el("tr");
    const canFocus = grid.view === "individual" && !grid.params.focus;
    const head = el(
      "th",
      { class: `row-head${canFocus ? " rh-link" : ""}`, title: canFocus ? "Show this participant's own timeline" : row.label },
      el("div", { class: "rh-label" }, row.label, el("span", { class: "rh-pct" }, pct(row.summary.pct))),
      el("div", { class: "rh-sub" }, rowSub(row)),
    );
    if (canFocus) {
      head.onclick = () => {
        dash.focus = row.key;
        loadView();
      };
    }
    tr.append(head);
    row.cells.forEach((cell, index) => tr.append(renderCell(cell, grid.columns[index], row, false)));
    return tr;
  }

  function rowSub(row) {
    const parts = [];
    if (row.sub) parts.push(row.sub);
    if (row.summary.last) parts.push(`last ${when(row.summary.last)}`);
    return parts.join(" · ");
  }

  function cellText(cell, view) {
    if (view === "cross") {
      if (cell.d) return `✓ ${cell.t ? when(cell.t).replace(/^\w+ /, "") : ""}`.trim();
      return CROSS_SYMBOL[cell.s] || "";
    }
    if (cell.e === 0) return cell.x ? `+${cell.x}` : "";
    if (cell.s === "upcoming") return "·";
    let text = `${cell.d}/${cell.e}`;
    if (cell.x) text += ` +${cell.x}`;
    return text;
  }

  function cellTitle(cell, column, row) {
    const where = [row ? row.label : "All participants", column.label, column.sub].filter(Boolean).join(" · ");
    if (!cell) return `${where}: not in this applet`;
    const parts = [`${where}: ${STATUS_LABEL[cell.s] || cell.s}`];
    if (dash.grid.view === "cross") {
      if (cell.x) parts.push(`${cell.x} completion(s)${cell.t ? `, first on ${cell.t}` : ""}`);
      return parts.join("\n");
    }
    if (cell.e) parts.push(`${cell.d} of ${cell.e} expected done`);
    if (cell.l) parts.push(`${cell.l} outside the time window`);
    if (cell.p) parts.push(`${cell.p} flow(s) partly done`);
    if (cell.m) parts.push(`${cell.m} missed`);
    if (cell.u) parts.push(`${cell.u} open or upcoming`);
    if (cell.x) parts.push(`${cell.x} done without being scheduled`);
    return parts.join("\n");
  }

  function renderCell(cell, column, row, isTotal) {
    if (!cell) return el("td", { class: "cell s-na", title: cellTitle(null, column, row) });
    const td = el("td", { class: `cell s-${cell.s}${column.today ? " today" : ""}`, title: cellTitle(cell, column, row) }, cellText(cell, dash.grid.view));
    if (cell.l && dash.grid.view !== "cross" && cell.s === "done") td.append(el("span", { class: "late-mark", title: "includes a completion outside its time window" }, "*"));
    if (!isTotal && row && (cell.e || cell.x)) td.onclick = () => openCell(row, column);
    return td;
  }

  // -- cell details ---------------------------------------------------------------------------------

  async function openCell(row, column) {
    const params = viewQuery();
    params.set("row", row.key);
    params.set("col", column.key);
    $("dash-cell-title").textContent = `${row.label} · ${[column.label, column.sub].filter(Boolean).join(" ")}`;
    const body = $("dash-cell-body");
    body.innerHTML = "";
    body.append(el("p", { class: "fineprint" }, "Loading…"));
    $("dash-cell-modal").hidden = false;
    let items;
    try {
      ({ items } = await api(`/api/dashboard/studies/${dash.current.id}/cell?${params}`));
    } catch (error) {
      body.innerHTML = "";
      body.append(el("div", { class: "error" }, error.message));
      return;
    }
    body.innerHTML = "";
    if (!items.length) body.append(el("p", { class: "fineprint" }, "Nothing was scheduled or completed here."));
    for (const item of items) body.append(renderOccurrence(item));
  }

  function renderOccurrence(item) {
    const block = el("div", { class: "occ" });
    const whenText =
      item.kind === "occurrence"
        ? `${when(item.day)} · window ${item.windowStart.slice(11)}–${item.windowEnd.slice(11)}${item.windowEnd.slice(0, 10) !== item.day ? " next day" : ""}`
        : when(item.day);
    block.append(
      el(
        "div",
        { class: "occ-head" },
        el("strong", {}, item.entity),
        el("span", { class: `badge s-${item.status}` }, STATUS_LABEL[item.status] || item.status),
        el("span", { class: "occ-when" }, whenText),
        item.schedule ? el("span", { class: "occ-when" }, `${item.schedule} schedule, ${item.periodicity.toLowerCase()}`) : "",
      ),
    );
    if (item.completions.length) {
      const list = el("ul", { class: "occ-items" });
      for (const completion of item.completions) {
        const line = el("li", {}, `Completed ${when(completion.end)}`);
        if (completion.start && completion.start !== completion.end) line.append(el("span", { class: "muted" }, ` (started ${completion.start.slice(11)})`));
        if (item.kind === "occurrence" && !completion.inWindow) line.append(el("span", { class: "muted" }, " — outside the time window"));
        if (completion.activities) {
          const done = completion.activities.filter((activity) => activity.done).length;
          line.append(el("span", { class: "muted" }, ` — ${done} of ${completion.activities.length} activities: `));
          line.append(completion.activities.map((activity) => `${activity.done ? "✓" : "✕"} ${activity.name}`).join(", "));
        }
        list.append(line);
      }
      block.append(list);
    } else if (item.kind === "occurrence") {
      block.append(el("ul", { class: "occ-items" }, el("li", { class: "muted" }, item.status === "missed" ? "Not completed." : "Not completed yet.")));
    }
    return block;
  }

  $("dash-cell-close").onclick = () => {
    $("dash-cell-modal").hidden = true;
  };
  $("dash-cell-modal").onclick = (event) => {
    if (event.target === $("dash-cell-modal")) $("dash-cell-modal").hidden = true;
  };

  // -- export -----------------------------------------------------------------------------------------

  async function exportCsv(format) {
    if (!dash.current) return;
    const params = viewQuery();
    params.set("format", format);
    const response = await fetch(`/api/dashboard/studies/${dash.current.id}/export?${params}`, {
      headers: { "X-Curious-Token": TOKEN || "" },
    });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        message = (await response.json()).error || message;
      } catch {
        /* keep the status */
      }
      return setError("dash-error", message);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/);
    download(await response.blob(), match ? match[1] : `dashboard_${format}.csv`);
  }
  $("dash-export-grid").onclick = () => exportCsv("grid");

  // The full export is a job like the downloads: a folder with export_info.json and the CSVs.
  $("dash-export").onclick = () => {
    if (!dash.current || !dash.current.loaded) return;
    setError("dash-error", "");
    startJob("dash-export", "dash-error", `/api/dashboard/studies/${dash.current.id}/export-job`, {
      outputDir: $("dash-output").value,
      params: Object.fromEntries(viewQuery().entries()),
      workbook: $("dash-export-xlsx").checked,
      gridJson: $("dash-export-json").checked,
    });
  };

  window.dashboard = { show, onLogin };
})();
