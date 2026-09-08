(function () {
  "use strict";

  const Core = window.UptickTraceViewerCore;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const COLORS = [
    "var(--accent)",
    "var(--ok)",
    "var(--warn)",
    "var(--bad)",
    "var(--note)",
    "var(--accent-ink)",
  ];
  const MAP_NODE_WIDTH = 286;
  const MAP_NODE_HEIGHT = 430;
  const MAP_NODE_GAP = 48;
  const MAP_PADDING = 64;
  const MAP_MIN_SCALE = 0.018;
  const MAP_MAX_SCALE = 2.4;
  const TAB_ORDER = ["run", "map", "charts", "experiment"];
  const FILTER_DEFINITIONS = [
    { key: "failedObservation", label: "Failed observations" },
    { key: "policyRejected", label: "Policy rejection" },
    { key: "providerFailure", label: "Provider/schema failure" },
    { key: "terminal", label: "Terminal decision" },
    { key: "errors", label: "Errors / DDoS" },
    { key: "advances", label: "Advances", group: true },
    { key: "fixes", label: "Fixes", group: true },
    { key: "scaling", label: "Scaling", group: true },
    { key: "deployments", label: "Deployments", group: true },
    { key: "operations", label: "Operations", group: true },
  ];

  const elements = {
    picker: document.getElementById("artifact-picker"),
    reset: document.getElementById("reset-button"),
    dropZone: document.getElementById("drop-zone"),
    messages: document.getElementById("messages"),
    workspace: document.getElementById("workspace"),
    runSelect: document.getElementById("run-select"),
    fileSummary: document.getElementById("file-summary"),
    overview: document.getElementById("overview"),
    tabs: Array.from(document.querySelectorAll(".tab[role='tab']")),
    runPanel: document.getElementById("run-panel"),
    mapPanel: document.getElementById("map-panel"),
    strategyEpisodes: document.getElementById("strategy-episodes"),
    mapEpisodeLabel: document.getElementById("map-episode-label"),
    mapEpisodeTitle: document.getElementById("map-episode-title"),
    mapEpisodeSummary: document.getElementById("map-episode-summary"),
    mapCanvas: document.getElementById("decision-map-canvas"),
    mapSvg: document.getElementById("decision-map-svg"),
    mapMinimap: document.getElementById("decision-map-minimap"),
    mapDetail: document.getElementById("decision-map-detail"),
    mapZoomOut: document.getElementById("map-zoom-out"),
    mapZoomIn: document.getElementById("map-zoom-in"),
    mapZoomLabel: document.getElementById("map-zoom-label"),
    mapFit: document.getElementById("map-fit"),
    mapCenter: document.getElementById("map-center"),
    mapExpandCadence: document.getElementById("map-expand-cadence"),
    mapMatchLabel: document.getElementById("map-match-label"),
    chartsPanel: document.getElementById("charts-panel"),
    experimentPanel: document.getElementById("experiment-panel"),
    filters: document.getElementById("filters"),
    search: document.getElementById("search-input"),
    actionFilter: document.getElementById("action-filter"),
    filterToggle: document.getElementById("filter-toggle"),
    filterDrawer: document.getElementById("filter-drawer"),
    iterationMin: document.getElementById("iteration-min"),
    iterationMax: document.getElementById("iteration-max"),
    filterToggles: document.getElementById("filter-toggles"),
    clearFilters: document.getElementById("clear-filters"),
    resultCount: document.getElementById("result-count"),
    timelineRows: document.getElementById("timeline-rows"),
    decisionDetail: document.getElementById("decision-detail"),
    stateDetail: document.getElementById("state-detail"),
    chartsGrid: document.getElementById("charts-grid"),
    experimentContent: document.getElementById("experiment-content"),
    timingSummary: document.getElementById("timing-summary"),
    keyboardHelp: document.getElementById("keyboard-help"),
  };

  const state = {
    trace: null,
    companions: [],
    session: null,
    runId: null,
    selectedIteration: null,
    visibleRows: [],
    tab: "run",
    selectedEpisodeId: null,
    cadenceExpanded: false,
    notices: [],
    filters: emptyFilters(),
    corpusQuery: "",
    corpusSelection: null,
    mapView: {
      runId: null,
      x: 0,
      y: 0,
      scale: 1,
      minScale: MAP_MIN_SCALE,
      contentWidth: 0,
      contentHeight: 0,
      group: null,
      dragging: false,
      moved: false,
      pointerId: null,
      pointerX: 0,
      pointerY: 0,
    },
  };

  function emptyFilters() {
    return {
      text: "",
      action: "",
      minIteration: null,
      maxIteration: null,
      failedObservation: false,
      policyRejected: false,
      providerFailure: false,
      terminal: false,
      errors: false,
      groups: new Set(),
    };
  }

  /* ---------------------------------------------------------------------- */
  /* DOM helpers                                                            */
  /* ---------------------------------------------------------------------- */

  function createNode(tagName, className, textValue) {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    if (textValue !== undefined && textValue !== null) element.textContent = String(textValue);
    return element;
  }

  function createSvgNode(tagName, className) {
    const element = document.createElementNS(SVG_NS, tagName);
    if (className) element.setAttribute("class", className);
    return element;
  }

  function clearNode(element) {
    while (element.firstChild) element.removeChild(element.firstChild);
  }

  function appendChildren(parent) {
    for (let index = 1; index < arguments.length; index += 1) {
      const child = arguments[index];
      if (child !== null && child !== undefined) parent.appendChild(child);
    }
    return parent;
  }

  function isKnown(value) {
    return value !== null && value !== undefined;
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function button(label, className, onClick) {
    const node = createNode("button", className, label);
    node.type = "button";
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }

  function badge(text, variant, title) {
    const node = createNode("span", "badge" + (variant ? " badge-" + variant : ""), text);
    if (title) node.title = title;
    return node;
  }

  function jsonBlock(value) {
    return createNode("pre", "json-block", Core.stringifyForDisplay(value));
  }

  function missingText(text) {
    return createNode("p", "story-text missing", text || "missing");
  }

  function countNote(value) {
    if (Array.isArray(value)) return value.length + (value.length === 1 ? " item" : " items");
    if (isObject(value)) {
      const size = Object.keys(value).length;
      return size + (size === 1 ? " key" : " keys");
    }
    return null;
  }

  // Collapsible section. The body is built on first open so large raw
  // payloads are not rendered until a person asks for them.
  function disclosure(title, note, buildBody, open) {
    const details = createNode("details", "disclosure");
    const summary = createNode("summary");
    summary.appendChild(createNode("span", "", title));
    if (note) summary.appendChild(createNode("span", "summary-note", note));
    details.appendChild(summary);
    const body = createNode("div", "disclosure-body");
    details.appendChild(body);
    let built = false;
    function build() {
      if (built) return;
      built = true;
      buildBody(body);
    }
    if (open) {
      details.open = true;
      build();
    } else {
      details.addEventListener("toggle", function () { if (details.open) build(); }, { once: true });
    }
    return details;
  }

  function jsonDisclosure(title, value, note, open) {
    const label = note || (isKnown(value) ? countNote(value) : "missing");
    return disclosure(title, label, function (body) {
      if (!isKnown(value)) body.appendChild(missingText());
      else body.appendChild(jsonBlock(value));
    }, open);
  }

  function metricTable(rows) {
    const list = createNode("dl", "metric-table");
    for (const row of rows) {
      const line = createNode("div", "metric-line");
      const term = createNode("dt", "", row[0]);
      const missing = !isKnown(row[1]);
      const description = createNode("dd", missing ? "missing" : "", missing ? "missing" : displayValue(row[1]));
      description.title = description.textContent;
      appendChildren(line, term, description);
      list.appendChild(line);
    }
    return list;
  }

  function kvTable(object) {
    const list = createNode("dl", "args-table");
    for (const entry of Object.entries(object)) {
      const line = createNode("div");
      const value = entry[1];
      // Strings are shown as saved; every other value keeps its exact JSON form.
      const rendered = typeof value === "string" ? value : Core.stringifyForDisplay(value);
      appendChildren(line, createNode("dt", "", entry[0]), createNode("dd", "", rendered));
      list.appendChild(line);
    }
    return list;
  }

  function storySection(title, note) {
    const section = createNode("section", "story-section");
    const heading = createNode("h3", "", title);
    if (note) heading.appendChild(createNode("span", "h-note", note));
    section.appendChild(heading);
    return section;
  }

  function storyBlock(title, child, note) {
    const block = createNode("div", "story-block");
    if (title) block.appendChild(createNode("h4", "", title));
    if (child) block.appendChild(child);
    if (note) block.appendChild(createNode("p", "source-note", note));
    return block;
  }

  function storyText(value, tone) {
    if (!isKnown(value) || value === "") return missingText();
    return createNode("p", "story-text" + (tone ? " " + tone : ""), value);
  }

  function savedSummary(title, value, tone) {
    if (typeof value !== "string" || value.length <= 320) {
      return storyBlock(title, storyText(value, tone));
    }
    return disclosure(title, value.length.toLocaleString("en-US") + " characters · full saved text", function (body) {
      body.appendChild(storyText(value, tone));
    });
  }

  function storyList(values, emptyText) {
    if (!Array.isArray(values) || !values.length) return missingText(emptyText || "none recorded");
    const list = createNode("ol", "story-list");
    for (const value of values) {
      const item = createNode("li");
      if (isObject(value)) item.appendChild(kvTable(value));
      else if (Array.isArray(value)) item.appendChild(jsonBlock(value));
      else item.textContent = Core.formatScalar(value);
      list.appendChild(item);
    }
    return list;
  }

  function factsGrid(entries) {
    const list = createNode("dl", "story-facts");
    for (const entry of entries) {
      const wrapper = createNode("div");
      const missing = !isKnown(entry[1]) || entry[1] === "";
      const value = createNode("dd", (entry[2] ? entry[2] : "") + (missing ? " missing" : ""), missing ? "missing" : displayValue(entry[1]));
      appendChildren(wrapper, createNode("dt", "", entry[0]), value);
      list.appendChild(wrapper);
    }
    return list;
  }

  /* ---------------------------------------------------------------------- */
  /* Session                                                                */
  /* ---------------------------------------------------------------------- */

  function selectedRun() {
    if (!state.session) return null;
    return state.session.runs.find(function (run) { return run.runId === state.runId; }) || null;
  }

  function selectedRow() {
    const run = selectedRun();
    if (!run) return null;
    return run.rows.find(function (row) { return row.iteration === state.selectedIteration; }) || null;
  }

  function episodeForIteration(run, iteration) {
    if (!run) return null;
    return run.strategyEpisodes.find(function (episode) {
      return episode.rows.some(function (row) { return row.iteration === iteration; });
    }) || null;
  }

  function defaultStoryIteration(run) {
    if (!run || !run.rows.length) return null;
    const highSignal = run.strategyEpisodes.find(function (episode) { return episode.hasScale; }) ||
      run.strategyEpisodes.find(function (episode) { return episode.hasFix; }) ||
      run.strategyEpisodes.find(function (episode) { return episode.hasContradiction; });
    return highSignal ? highSignal.startedIteration : run.rows[0].iteration;
  }

  async function loadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const parsedTraces = [];
    const parsedCompanions = [];
    const notices = [];
    for (const file of files) {
      try {
        const text = await file.text();
        if (file.name.toLocaleLowerCase().endsWith(".jsonl")) {
          parsedTraces.push(Core.parseTraceJsonl(text, file.name));
        } else {
          parsedCompanions.push(Core.parseCompanionJson(text, file.name));
        }
      } catch (error) {
        notices.push({ level: "error", text: error.message || String(error) });
      }
    }
    if (parsedTraces.length > 1) {
      notices.push({ level: "error", text: "Select one trace JSONL per viewer session." });
    } else if (parsedTraces.length === 1) {
      state.trace = parsedTraces[0];
      state.companions = parsedCompanions;
      state.runId = null;
      state.selectedIteration = null;
      resetFilters(false);
    } else {
      state.companions = state.companions.concat(parsedCompanions);
    }
    state.notices = notices;
    rebuildSession();
    elements.picker.value = "";
  }

  function rebuildSession() {
    if (!state.trace) {
      renderMessages();
      return;
    }
    try {
      state.session = Core.buildSession(state.trace, state.companions);
      if (!state.runId || !state.session.runs.some(function (run) { return run.runId === state.runId; })) {
        state.runId = state.session.runs[0].runId;
      }
      const run = selectedRun();
      if (!run.rows.some(function (row) { return row.iteration === state.selectedIteration; })) {
        state.selectedIteration = defaultStoryIteration(run);
      }
      const episode = episodeForIteration(run, state.selectedIteration);
      state.selectedEpisodeId = episode ? episode.id : null;
      renderAll();
    } catch (error) {
      state.notices.push({ level: "error", text: error.message || String(error) });
      renderMessages();
    }
  }

  function renderAll() {
    const run = selectedRun();
    elements.dropZone.hidden = true;
    elements.workspace.hidden = false;
    elements.reset.hidden = false;
    renderMessages();
    renderRunSelector();
    renderFileSummary();
    renderOutcomeOverview(run);
    renderActionFilter(run);
    renderFilterToggles();
    renderTimeline(run);
    renderDecisionDetail(selectedRow());
    renderStateDetail(selectedRow());
    renderStrategyEpisodes(run);
    renderDecisionMap(run);
    renderCharts(run);
    renderExperiment();
    renderTimingSummary(run);
    switchTab(state.tab);
  }

  function renderSelectionDependents() {
    const run = selectedRun();
    renderTimeline(run);
    renderDecisionDetail(selectedRow());
    renderStateDetail(selectedRow());
    updateDecisionMapState(run);
  }

  function renderMessages() {
    clearNode(elements.messages);
    const all = state.notices.slice();
    if (state.session) {
      for (const warning of state.session.warnings) all.push({ level: "warning", text: warning });
    }
    elements.messages.hidden = all.length === 0;
    for (const notice of all) {
      elements.messages.appendChild(
        createNode("div", "message message-" + (notice.level || "info"), notice.text),
      );
    }
  }

  function renderRunSelector() {
    clearNode(elements.runSelect);
    for (const run of state.session.runs) {
      const option = createNode("option", "", run.runId);
      option.value = run.runId;
      option.selected = run.runId === state.runId;
      elements.runSelect.appendChild(option);
    }
  }

  function renderFileSummary() {
    const companionNames = state.companions.map(function (item) { return item.sourceName; });
    elements.fileSummary.textContent = [state.trace.sourceName].concat(companionNames).join(" · ");
    elements.fileSummary.title = elements.fileSummary.textContent;
  }

  /* ---------------------------------------------------------------------- */
  /* Overview                                                               */
  /* ---------------------------------------------------------------------- */

  function statusCard(label, value, detail, variant) {
    const card = createNode("article", "status-card" + (variant ? " is-" + variant : ""));
    card.appendChild(createNode("span", "label", label));
    const valueNode = createNode("span", "value", isKnown(value) ? value : "missing");
    if (!isKnown(value)) valueNode.classList.add("missing");
    valueNode.title = valueNode.textContent;
    card.appendChild(valueNode);
    if (detail !== undefined) {
      const detailNode = createNode("span", "detail", isKnown(detail) ? detail : "missing");
      detailNode.title = detailNode.textContent;
      card.appendChild(detailNode);
    }
    return card;
  }

  function moneyCard(label, value, currency) {
    const formatted = Core.formatMinor(value);
    return statusCard(
      label,
      isKnown(value) ? formatted.compact : null,
      joinKnown("raw " + formatted.raw, currency, " · "),
    );
  }

  function renderOutcomeOverview(run) {
    clearNode(elements.overview);
    if (!run) return;
    const metrics = run.outcomeMetrics;
    const score = statusCard("Score", Core.formatScalar(metrics.score),
      isKnown(metrics.max_score) ? "published maximum " + Core.formatScalar(metrics.max_score) : "published maximum missing");
    const uptime = statusCard("Uptime", formatPercent(metrics.uptime_ratio),
      joinKnown("target " + formatPercent(metrics.uptime_target),
        metrics.slo_passed === true ? "SLO passed" : metrics.slo_passed === false ? "SLO failed" : null, " · "));
    const cost = moneyCard("Total cost", metrics.total_cost_minor, metrics.currency);
    const lifecycle = run.lifecycle;
    const outcome = statusCard(
      "Run outcome",
      lifecycle.runner,
      [
        "simulator " + lifecycle.simulator,
        Core.formatScalar(run.steps) + " decisions",
        run.stopReason || "stop reason missing",
      ].join(" · "),
      lifecycle.runner === "failed" ? "danger" : lifecycle.simulatorCompleted ? "success" : "warning",
    );
    appendChildren(elements.overview, score, uptime, cost, outcome);

    const details = disclosure(
      "Run details",
      "provenance, telemetry, published outcome",
      function (body) {
        const grid = createNode("div", "overview-details-grid");
        const manifest = run.manifest || {};
        const reasoner = manifest.reasoner || {};
        const firstTelemetry = run.rows.find(function (row) { return row.telemetry; });
        const reported = firstTelemetry ? firstTelemetry.telemetry : {};
        grid.appendChild(overviewTableCard("Published outcome", Object.entries(metrics)));
        grid.appendChild(
          overviewTableCard("Identity & lifecycle", [
            ["run_id", run.runId],
            ["seed", manifest.seed],
            ["repeat_index", manifest.repeat_index],
            ["agent", joinKnown(manifest.agent_id, manifest.agent_version, " / ")],
            ["memory_mode", manifest.memory_mode],
            ["runner", lifecycle.runner],
            ["simulator", lifecycle.simulator],
            ["budget reached", lifecycle.budgetReached],
            ["steps / max", Core.formatScalar(run.steps) + " / " + Core.formatScalar(run.maxIterations)],
            ["start", joinKnown(run.startTime, run.startTimeSource, " · ")],
            ["end", joinKnown(run.endTime, run.endTimeSource, " · ")],
            ["stop_reason", run.stopReason],
          ]),
        );
        grid.appendChild(
          overviewTableCard("Reasoner", [
            ["provider", reasoner.provider || reported.provider],
            ["requested model", reasoner.model || reported.requested_model],
            ["reported model", reported.reported_model],
            ["requested effort", reasoner.effort || reported.requested_effort],
            ["reported effort", reported.reported_effort],
            ["thread mode", reasoner.thread_mode || reported.thread_mode],
            ["SDK", joinKnown(reported.sdk_name, reported.sdk_version, " ")],
          ]),
        );
        grid.appendChild(
          overviewTableCard("Provenance", [
            ["git revision", manifest.git_revision],
            ["git dirty", manifest.git_dirty],
            ["system prompt SHA-256", manifest.system_prompt_sha256],
            ["schema SHA-256", manifest.schema_sha256],
            ["provider instructions SHA-256", manifest.provider_instructions_sha256],
            ["resolved spec SHA-256", manifest.resolved_spec_sha256],
            ["uv.lock SHA-256", manifest.uv_lock_sha256],
            ["Python", manifest.python_version],
            ["simulator endpoint SHA-256", manifest.simulator_endpoint_sha256],
            ["reproducible", manifest.reproducible],
          ]),
        );
        grid.appendChild(overviewTableCard("Run telemetry", telemetryRows(run.metrics)));
        grid.appendChild(overviewTableCard("Action counts", actionCountRows(run.metrics)));
        body.appendChild(grid);
      },
    );
    details.classList.add("overview-disclosure");
    elements.overview.appendChild(details);
  }

  function overviewTableCard(label, rows) {
    const card = createNode("article", "status-card");
    card.appendChild(createNode("span", "label", label));
    card.appendChild(metricTable(rows));
    return card;
  }

  function telemetryRows(metrics) {
    const value = metrics || {};
    const usage = value.token_usage || {};
    return [
      ["model turns", value.model_turns],
      ["capability executions", value.capability_executions],
      ["policy rejections", value.policy_rejections],
      ["simulator calls", value.simulator_calls],
      ["program executions", value.program_executions],
      ["program subcalls", value.program_subcalls],
      ["model duration", formatDuration(value.model_duration_seconds)],
      ["environment duration", formatDuration(value.environment_duration_seconds)],
      ["transport duration", formatDuration(value.transport_duration_seconds)],
      ["input tokens", usage.input_tokens],
      ["output tokens", usage.output_tokens],
      ["reasoning tokens", usage.reasoning_output_tokens],
      ["total tokens", usage.total_tokens],
      ["cached input tokens", usage.cached_input_tokens],
    ];
  }

  function actionCountRows(metrics) {
    const counts = metrics && metrics.action_counts ? metrics.action_counts : null;
    if (!counts || !Object.keys(counts).length) return [["action counts", null]];
    return Object.entries(counts)
      .sort(function (left, right) { return right[1] - left[1] || left[0].localeCompare(right[0]); });
  }

  /* ---------------------------------------------------------------------- */
  /* Filters and decision list                                              */
  /* ---------------------------------------------------------------------- */

  function renderActionFilter(run) {
    const previous = state.filters.action;
    clearNode(elements.actionFilter);
    const allOption = createNode("option", "", "All actions");
    allOption.value = "";
    elements.actionFilter.appendChild(allOption);
    const actions = Array.from(
      new Set(run.rows.map(function (row) { return row.actionName; }).filter(Boolean)),
    ).sort();
    for (const action of actions) {
      const option = createNode("option", "", action);
      option.value = action;
      elements.actionFilter.appendChild(option);
    }
    elements.actionFilter.value = actions.includes(previous) ? previous : "";
    state.filters.action = elements.actionFilter.value;
  }

  function renderFilterToggles() {
    clearNode(elements.filterToggles);
    for (const definition of FILTER_DEFINITIONS) {
      const label = createNode("label", "toggle");
      const input = createNode("input");
      input.type = "checkbox";
      input.dataset.filterKey = definition.key;
      input.dataset.filterGroup = definition.group ? "true" : "false";
      input.checked = definition.group
        ? state.filters.groups.has(definition.key)
        : Boolean(state.filters[definition.key]);
      input.addEventListener("change", onFilterToggle);
      appendChildren(label, input, createNode("span", "", definition.label));
      elements.filterToggles.appendChild(label);
    }
  }

  function onFilterToggle(event) {
    const input = event.currentTarget;
    const key = input.dataset.filterKey;
    if (input.dataset.filterGroup === "true") {
      if (input.checked) state.filters.groups.add(key);
      else state.filters.groups.delete(key);
    } else {
      state.filters[key] = input.checked;
    }
    renderSelectionDependents();
  }

  function hasActiveFilters() {
    const filters = state.filters;
    return Boolean(
      filters.text ||
      filters.action ||
      isKnown(filters.minIteration) ||
      isKnown(filters.maxIteration) ||
      filters.failedObservation ||
      filters.policyRejected ||
      filters.providerFailure ||
      filters.terminal ||
      filters.errors ||
      filters.groups.size,
    );
  }

  function setFilterDrawer(open) {
    elements.filterDrawer.hidden = !open;
    elements.filterToggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function renderTimeline(run) {
    if (!run) return;
    state.visibleRows = Core.filterDecisionRows(run.rows, state.filters);
    if (!state.visibleRows.some(function (row) { return row.iteration === state.selectedIteration; })) {
      state.selectedIteration = state.visibleRows.length ? state.visibleRows[0].iteration : null;
    }
    const active = hasActiveFilters();
    elements.resultCount.textContent = active
      ? state.visibleRows.length + " of " + run.rows.length + " decisions"
      : run.rows.length + (run.rows.length === 1 ? " decision" : " decisions");
    elements.clearFilters.hidden = !active;
    clearNode(elements.timelineRows);
    const fragment = document.createDocumentFragment();
    for (const row of state.visibleRows) fragment.appendChild(timelineRow(row));
    if (!state.visibleRows.length) {
      fragment.appendChild(createNode("div", "empty-panel", "No decisions match the active filters."));
    }
    elements.timelineRows.appendChild(fragment);
  }

  function timelineRow(row) {
    const item = createNode("div", "timeline-item");
    item.setAttribute("role", "listitem");
    const node = createNode("button", "timeline-row");
    node.type = "button";
    node.dataset.iteration = String(row.iteration);
    node.setAttribute("aria-label", "Iteration " + row.iteration + ", " + (row.actionName || "no action"));
    if (row.iteration === state.selectedIteration) node.setAttribute("aria-current", "true");
    node.addEventListener("click", function () { selectIteration(row.iteration); });

    node.appendChild(createNode("span", "row-index", "#" + row.iteration));

    const head = createNode("span", "row-head");
    const dot = createNode("span", "row-dot " + rowDotClass(row));
    dot.title = rowDotTitle(row);
    head.appendChild(dot);
    head.appendChild(createNode("span", "row-action" + (row.actionName ? "" : " missing"), row.actionName || "no action recorded"));
    node.appendChild(head);

    node.appendChild(createNode("span", "row-summary", rowSummary(row)));

    const meta = createNode("span", "row-meta");
    const clock = createNode("span", "mono", formatSimulationTime(row.preTime));
    clock.title = "Simulation time before the action · recorded " + formatTimestamp(row.recordedAt);
    meta.appendChild(clock);
    for (const marker of rowMarkers(row).slice(0, 3)) meta.appendChild(marker);
    node.appendChild(meta);
    item.appendChild(node);
    return item;
  }

  function rowSummary(row) {
    if (row.failure && !row.observation) return row.failure.message || "failure without message";
    if (row.observation) return row.observation.summary || "observation without summary";
    return "no observation recorded";
  }

  function rowDotClass(row) {
    if (row.failure || row.failedObservation || row.policyRejected || row.incidentCodes.length) return "is-bad";
    if (row.terminalDecision || row.terminalObservation) return "is-terminal";
    if (row.freeTextIncidentCodes.length || row.activeOperationDuringAdvance ||
      (row.followingAssessment && row.followingAssessment.status === "contradicted")) return "is-warn";
    if (row.observation && row.observation.ok === true) return "is-ok";
    return "";
  }

  function rowDotTitle(row) {
    if (row.failure) return "Failure recorded";
    if (row.failedObservation) return "Observation reported ok = false";
    if (row.policyRejected) return "Policy rejected the action";
    if (row.incidentCodes.length) return "Structured incident code: " + row.incidentCodes.join(", ");
    if (row.terminalDecision || row.terminalObservation) return "Terminal";
    if (row.followingAssessment && row.followingAssessment.status === "contradicted") return "Following review was contradicted";
    if (row.freeTextIncidentCodes.length) return "Incident code in free text only";
    if (row.activeOperationDuringAdvance) return "Advanced time while an operation was pending";
    if (row.observation && row.observation.ok === true) return "Observation ok";
    return "No observation";
  }

  function rowMarkers(row) {
    const markers = [];
    if (row.failure) markers.push(badge("failure", "danger"));
    if (row.policyRejected) markers.push(badge("rejected", "danger"));
    if (row.failedObservation) markers.push(badge("failed", "danger"));
    for (const code of row.incidentCodes.slice(0, 2)) markers.push(badge(code, "danger", "Structured saved evidence"));
    if (row.terminalDecision) markers.push(badge("completed", "purple"));
    if (row.followingAssessment) {
      markers.push(badge("review " + row.followingAssessment.status, assessmentVariant(row.followingAssessment.status)));
    }
    if (row.groups.has("fixes")) markers.push(badge("fix", "success"));
    if (row.scaleDirection !== "unknown") markers.push(badge("scale " + row.scaleDirection, "purple"));
    if (row.groups.has("deployments")) markers.push(badge("deploy", "info"));
    for (const code of row.freeTextIncidentCodes.slice(0, 1)) {
      markers.push(badge(code + " · text", "warning", "Heuristic: exact incident code appears only in saved free text"));
    }
    if (row.activeOperationDuringAdvance) markers.push(badge("active op", "warning", "Heuristic: pre-action pending status is accepted, pending, running, or in_progress"));
    if (row.unchangedPoll) markers.push(badge("same payload", "warning", "Heuristic: same read-only call returned the same payload after removing clock"));
    return markers;
  }

  function assessmentVariant(status) {
    if (status === "confirmed") return "success";
    if (status === "contradicted") return "danger";
    if (status === "not_applicable") return null;
    return "warning";
  }

  function selectIteration(iteration, scroll, preserveOutsideFilters) {
    const run = selectedRun();
    const previousEpisodeId = state.selectedEpisodeId;
    state.selectedIteration = iteration;
    const episode = episodeForIteration(run, iteration);
    if (episode) state.selectedEpisodeId = episode.id;
    if (!preserveOutsideFilters) renderTimeline(run);
    else syncTimelineSelection();
    renderDecisionDetail(selectedRow());
    renderStateDetail(selectedRow());
    if (previousEpisodeId !== state.selectedEpisodeId) {
      state.cadenceExpanded = false;
      state.mapView.runId = null;
      renderStrategyEpisodes(run);
      renderDecisionMap(run);
    } else {
      updateDecisionMapState(run);
    }
    if (scroll) scrollTimelineToSelection();
  }

  function syncTimelineSelection() {
    for (const node of elements.timelineRows.querySelectorAll(".timeline-row")) {
      if (Number(node.dataset.iteration) === Number(state.selectedIteration)) node.setAttribute("aria-current", "true");
      else node.removeAttribute("aria-current");
    }
  }

  function scrollTimelineToSelection() {
    const target = Array.from(elements.timelineRows.querySelectorAll(".timeline-row")).find(function (node) {
      return Number(node.dataset.iteration) === Number(state.selectedIteration);
    });
    if (target) target.scrollIntoView({ block: "nearest" });
  }

  /* ---------------------------------------------------------------------- */
  /* Decision story                                                         */
  /* ---------------------------------------------------------------------- */

  function renderDecisionDetail(row) {
    clearNode(elements.decisionDetail);
    if (!row) {
      elements.decisionDetail.appendChild(createNode("div", "empty-panel", "Select a decision on the left."));
      return;
    }
    renderDecisionStory(elements.decisionDetail, row, { compact: false });
  }

  function renderDecisionStory(container, row, options) {
    const compact = Boolean(options && options.compact);
    const run = selectedRun();
    container.appendChild(storyHeader(row, compact));
    container.appendChild(sawSection(row, compact));
    container.appendChild(whySection(row, run, compact));
    container.appendChild(actionSection(row, compact));
    container.appendChild(resultSection(row, compact));
    container.appendChild(reviewSection(row));
    if (row.closedEpisode || row.episodeCommit) container.appendChild(episodeSection(row));
    if (compact) {
      const actions = createNode("div", "story-actions");
      const outsideCurrentFilters = !state.visibleRows.some(function (visible) {
        return visible.iteration === row.iteration;
      });
      const openLabel = outsideCurrentFilters
        ? "Clear filters and open in Decisions"
        : "Open in Decisions";
      actions.appendChild(button(openLabel, "button button-small", function () {
        if (outsideCurrentFilters) {
          resetFilters(false);
          renderFilterToggles();
        }
        switchTab("run");
        selectIteration(row.iteration, true, false);
      }));
      container.appendChild(actions);
    }
  }

  function storyHeader(row, compact) {
    const header = createNode("header", "story-header");
    const top = createNode("div", "story-header-top");
    const copy = createNode("div");
    copy.appendChild(createNode("p", "eyebrow", joinKnown(
      "Iteration " + row.iteration + " · sequence " + row.sequence,
      row.phase ? "phase " + row.phase : null,
      " · ",
    )));
    const title = createNode("h2", row.actionName ? "" : "missing", row.actionName || "No action recorded");
    copy.appendChild(title);
    top.appendChild(copy);
    if (!compact) {
      const nav = createNode("div", "story-nav");
      const previous = button("Previous", "button button-quiet button-small", function () { moveSelection(-1); });
      const next = button("Next", "button button-quiet button-small", function () { moveSelection(1); });
      previous.setAttribute("aria-label", "Previous visible decision");
      next.setAttribute("aria-label", "Next visible decision");
      const index = state.visibleRows.findIndex(function (item) { return item.iteration === row.iteration; });
      previous.disabled = index <= 0;
      next.disabled = index < 0 || index >= state.visibleRows.length - 1;
      appendChildren(nav, previous, next);
      top.appendChild(nav);
    }
    header.appendChild(top);

    const badges = createNode("div", "badge-row");
    if (row.observation) {
      badges.appendChild(
        row.observation.ok === false ? badge("observation failed", "danger")
          : row.observation.ok === true ? badge("observation ok", "success")
            : badge("observation ok missing", null),
      );
    } else {
      badges.appendChild(badge("no observation", null));
    }
    if (row.terminalObservation) badges.appendChild(badge("terminal observation", "purple"));
    if (row.terminalDecision) badges.appendChild(badge("task completed", "purple"));
    if (row.policyResult) badges.appendChild(row.policyRejected ? badge("policy rejected", "danger") : badge("policy accepted", "success"));
    if (row.failure) badges.appendChild(badge(joinKnown("failure", joinKnown(row.failure.stage, row.failure.category, " / "), " · "), "danger"));
    for (const code of row.incidentCodes) badges.appendChild(badge(code, "danger", "Structured saved evidence"));
    for (const code of row.freeTextIncidentCodes) {
      badges.appendChild(badge(code + " · text only", "warning", "Heuristic: exact incident code appears only in saved free text, not a structured error field"));
    }
    if (row.advanceMismatch) badges.appendChild(badge("advance mismatch", "warning", "Requested and applied advance seconds differ"));
    if (row.activeOperationDuringAdvance) badges.appendChild(badge("advanced during active operation", "warning", "Heuristic: pre-action pending status is accepted, pending, running, or in_progress"));
    if (row.unchangedPoll) badges.appendChild(badge("same payload as previous poll", "warning", "Heuristic: same read-only call returned the same payload after removing clock"));
    header.appendChild(badges);

    const meta = createNode("p", "story-meta");
    meta.appendChild(metaItem("Recorded", formatTimestamp(row.recordedAt), true));
    meta.appendChild(metaItem("Sim time", formatSimulationTime(row.preTime) + " → " + formatSimulationTime(row.postTime), true));
    meta.appendChild(metaItem("Δ sim", formatDuration(row.simulationDeltaSeconds), true));
    if (!compact) {
      meta.appendChild(metaItem("Model", formatDuration(row.payload.model_duration_seconds), true));
      meta.appendChild(metaItem("Env", formatDuration(row.payload.environment_duration_seconds), true));
      meta.appendChild(metaItem("Tokens", tokenSummary(row.tokenUsage), true));
    }
    header.appendChild(meta);
    return header;
  }

  function metaItem(label, value, mono) {
    const span = createNode("span");
    span.appendChild(createNode("b", "", label + " "));
    span.appendChild(createNode("span", mono ? "mono" : "", value));
    return span;
  }

  function sawSection(row, compact) {
    const section = storySection("What the agent saw", "pre-action state");
    const pending = Array.isArray(row.decisionView.pending_operations) ? row.decisionView.pending_operations : null;
    const brief = row.memoryBrief || {};
    const lessons = Array.isArray(brief.lessons) ? brief.lessons : [];
    const similar = Array.isArray(brief.similar_episodes) ? brief.similar_episodes : [];
    const contradictions = Array.isArray(brief.contradictions) ? brief.contradictions : [];
    section.appendChild(factsGrid([
      ["Environment status", row.preState.status],
      ["Pending operations", pending ? pendingSummary(pending) : null],
      ["Step", row.progress ? joinKnown(row.progress.step, row.progress.step_limit, " of ") : null],
      ["Memory records", lessons.length + " lessons · " + similar.length + " episodes · " + contradictions.length + " contradictions"],
    ].filter(function (entry) { return isKnown(entry[1]); })));
    if (row.preObservation) {
      section.appendChild(savedSummary(
        "Latest observation" + (row.preObservation.action_kind ? " · " + row.preObservation.action_kind : ""),
        row.preObservation.summary,
        row.preObservation.ok === false ? "is-bad" : null,
      ));
    } else {
      section.appendChild(storyBlock("Latest observation", missingText("No pre-action observation was recorded.")));
    }
    if (compact) return section;

    const memory = disclosure("Memory used for this decision", lessons.length + " lessons · " + similar.length + " episodes · " + contradictions.length + " contradictions", function (memory) {
      if (lessons.length) memory.appendChild(storyBlock("Lessons", storyList(lessons)));
      if (similar.length) memory.appendChild(storyBlock("Similar episodes", storyList(similar)));
      if (contradictions.length) memory.appendChild(storyBlock("Contradictions", storyList(contradictions)));
      if (!lessons.length && !similar.length && !contradictions.length) {
        memory.appendChild(missingText("MemoryBrief contained no lessons, similar episodes, or contradictions."));
      }
      const diagnostics = row.retrievalDiagnostics;
      memory.appendChild(disclosure("Retrieval diagnostics", diagnostics ? formatDuration(diagnostics.duration_seconds) : "missing", function (body) {
        if (!diagnostics) {
          body.appendChild(missingText("No retrieval diagnostics were recorded."));
          return;
        }
        body.appendChild(metricTable([
          ["retrieval duration", formatDuration(diagnostics.duration_seconds)],
          ["candidates", Array.isArray(diagnostics.candidate_record_ids) ? diagnostics.candidate_record_ids.length : null],
          ["selected record IDs", joinList(diagnostics.selected_record_ids)],
          ["excluded record IDs", joinList(diagnostics.excluded_record_ids)],
        ]));
        body.appendChild(createNode("p", "source-note",
          "The trace records selected/candidate IDs and the compact brief. It does not persist per-record scores or the full query."));
      }));
    });
    section.appendChild(memory);
    return section;
  }

  function pendingSummary(operations) {
    if (!operations.length) return "none";
    const parts = operations.slice(0, 3).map(function (operation) {
      if (!isObject(operation)) return Core.formatScalar(operation);
      return joinKnown(operation.kind, operation.status, " · ") || Core.stringifyForDisplay(operation);
    });
    if (operations.length > 3) parts.push("+" + (operations.length - 3) + " more");
    return operations.length + ": " + parts.join("; ");
  }

  function whySection(row, run, compact) {
    const section = storySection("Why it chose this", "recorded SGR v2 envelope");
    if (!row.envelope) {
      section.appendChild(missingText("No decision envelope was recorded for this iteration."));
      if (row.failure) {
        section.appendChild(storyBlock("Failure", storyText(row.failure.message, "is-lead is-bad")));
      }
      return section;
    }
    const strategyNote = row.strategyContinues
      ? "Continues the strategy started at iteration " + row.strategyStartedIteration + "."
      : null;
    section.appendChild(storyBlock("Strategy", storyText(row.strategy, "is-lead"), compact ? null : strategyNote));
    section.appendChild(storyBlock("Expected result", storyList(row.expectedResult, "none recorded")));
    section.appendChild(disclosure("Evidence and alternatives", row.facts.length + " facts · " + row.competingHypotheses.length + " hypotheses", function (section) {
      if (!compact) {
        const verification = row.previousVerification;
        const reviewBlock = storyBlock("Review of the previous decision");
        if (verification) {
          const line = createNode("div", "review-line");
          line.appendChild(badge(String(verification.status), assessmentVariant(String(verification.status))));
          line.appendChild(createNode("span", "h-note", previousReviewTarget(row, run)));
          reviewBlock.appendChild(line);
          reviewBlock.appendChild(storyList(verification.evidence, "no evidence recorded"));
        } else {
          reviewBlock.appendChild(missingText("No previous_verification was recorded."));
        }
        section.appendChild(reviewBlock);
      }
      section.appendChild(storyBlock("Facts", storyList(row.facts, "no facts recorded")));
      section.appendChild(storyBlock(
        "Competing hypotheses",
        storyList(row.competingHypotheses, "none recorded"),
        compact ? null : "Hypotheses are considered alternatives. The trace does not mark any of them as selected.",
      ));
      if (row.contradictingEvidence.length || !compact) {
        section.appendChild(storyBlock("Contradicting evidence", storyList(row.contradictingEvidence, "none recorded")));
      }
      if (!compact) section.appendChild(storyBlock("Verification plan", storyList(row.verification, "none recorded")));
    }));
    return section;
  }

  function previousReviewTarget(row, run) {
    if (!run) return "";
    const index = run.rows.findIndex(function (item) { return item.iteration === row.iteration; });
    const previous = index > 0 ? run.rows[index - 1] : null;
    if (previous && previous.assessmentIteration === row.iteration) {
      return "assesses iteration " + previous.iteration + " (" + (previous.actionName || "no action") + ")";
    }
    if (previous) return "not linked to iteration " + previous.iteration + " by open_decision.step";
    return "first decision of the run";
  }

  function actionSection(row, compact) {
    const section = storySection("Action", "capability and arguments");
    if (!row.action) {
      section.appendChild(missingText("No action was recorded."));
      return section;
    }
    const args = row.arguments || {};
    if (Object.keys(args).length && Core.stringifyForDisplay(args).length > 600) {
      section.appendChild(jsonDisclosure("Arguments", args));
    } else if (Object.keys(args).length) {
      section.appendChild(storyBlock(row.actionName || "action", kvTable(args)));
    } else {
      section.appendChild(storyBlock(row.actionName || "action", missingText("no arguments")));
    }
    if (row.groups.has("advances")) {
      section.appendChild(factsGrid([
        ["Requested advance", isKnown(row.requestedAdvance) ? Core.formatScalar(row.requestedAdvance) + "s" : null, "mono"],
        ["Applied advance", isKnown(row.appliedAdvance) ? Core.formatScalar(row.appliedAdvance) + "s" : null, "mono"],
      ]));
    }
    if (compact) return section;
    const policy = row.policyResult;
    const policyBlock = storyBlock("Policy result");
    if (!policy) {
      policyBlock.appendChild(missingText("No policy result was recorded."));
    } else {
      const line = createNode("div", "review-line");
      line.appendChild(policy.accepted === false ? badge("rejected", "danger") : policy.accepted === true ? badge("accepted", "success") : badge("accepted missing", null));
      policyBlock.appendChild(line);
      const violations = Array.isArray(policy.violations) ? policy.violations : [];
      if (violations.length) policyBlock.appendChild(storyList(violations));
      const extra = Object.keys(policy).filter(function (key) { return key !== "accepted" && key !== "violations"; });
      if (extra.length) policyBlock.appendChild(jsonDisclosure("Full policy result", policy));
    }
    section.appendChild(policyBlock);
    section.appendChild(jsonDisclosure("Raw action", row.action));
    return section;
  }

  function resultSection(row, compact) {
    const section = storySection("What happened", "observation from the same DecisionTrace");
    const observation = row.observation;
    if (observation) {
      const tone = observation.ok === false ? "is-lead is-bad"
        : observation.terminal === true ? "is-lead is-note"
          : observation.ok === true ? "is-lead is-ok"
            : "is-lead";
      section.appendChild(savedSummary("Saved observation summary", observation.summary, tone));
      const composite = compositeItems(observation);
      if (composite) section.appendChild(storyBlock(composite.title, composite.node));
      if (!compact) {
        section.appendChild(jsonDisclosure("Observation data", observation.data));
      }
    } else if (!row.failure) {
      section.appendChild(missingText("No observation was recorded."));
    }
    if (row.failure) {
      const failureBlock = storyBlock("Failure");
      failureBlock.appendChild(factsGrid([
        ["Stage", row.failure.stage],
        ["Category", row.failure.category],
      ]));
      failureBlock.appendChild(storyText(row.failure.message, "is-lead is-bad"));
      if (!compact) failureBlock.appendChild(jsonDisclosure("Full failure record", row.failure));
      section.appendChild(failureBlock);
    }
    return section;
  }

  // Batched and program observations keep every saved sub-result. The viewer
  // lists them exactly as saved and never derives completion or intent.
  function compositeItems(observation) {
    const data = observation.data;
    if (!isObject(data)) return null;
    if (Array.isArray(data.items) && data.items.some(function (item) { return isObject(item) && "call" in item; })) {
      const list = createNode("ol", "subcall-list");
      data.items.forEach(function (item, index) {
        list.appendChild(batchItem(item, index));
      });
      const block = createNode("div");
      block.appendChild(factsGrid([
        ["Batch", data.batch_id],
        ["Code", data.code],
        ["Violations", Array.isArray(data.violations) && data.violations.length ? data.violations.join("; ") : Array.isArray(data.violations) ? "none" : null],
      ]));
      block.appendChild(list);
      return { title: "Batched calls (" + data.items.length + ")", node: block };
    }
    if (Array.isArray(data.results) && data.results.some(function (item) { return isObject(item) && "step" in item; })) {
      const list = createNode("ol", "subcall-list");
      data.results.forEach(function (item, index) {
        list.appendChild(programResult(item, index));
      });
      const block = createNode("div");
      block.appendChild(factsGrid([
        ["Program", data.program_id],
        ["Created", data.created],
        ["Executed calls", data.executed_calls],
        ["Skipped follow-up", data.skipped_followup],
        ["Output", data.code || null],
      ]));
      block.appendChild(list);
      return { title: "Program results (" + data.results.length + ")", node: block };
    }
    return null;
  }

  function batchItem(item, index) {
    const node = createNode("li", "subcall");
    if (!isObject(item)) {
      node.appendChild(createNode("span", "subcall-index", String(index)));
      node.appendChild(createNode("span", "subcall-body", Core.stringifyForDisplay(item)));
      return node;
    }
    node.appendChild(createNode("span", "subcall-index", isKnown(item.index) ? String(item.index) : String(index)));
    const head = createNode("span", "subcall-head");
    const call = isObject(item.call) ? item.call : {};
    head.appendChild(createNode("span", "", call.name || "call name missing"));
    if (isKnown(item.status)) head.appendChild(badge(String(item.status), item.status === "executed" ? "success" : item.status === "not_executed" ? null : "warning"));
    const response = isObject(item.observation) ? item.observation : null;
    if (response && typeof response.ok === "boolean") {
      head.appendChild(response.ok ? badge("ok", "success") : badge("failed", "danger"));
    }
    if (response && response.terminal === true) head.appendChild(badge("terminal", "purple"));
    node.appendChild(head);
    const body = createNode("div", "subcall-body");
    const lines = [];
    if (isObject(call.arguments) && Object.keys(call.arguments).length) lines.push(summarizeArguments(call.arguments, 4));
    if (response) lines.push(response.summary || "response without summary");
    else lines.push("no response saved");
    if (isKnown(item.reason)) lines.push("reason: " + Core.formatScalar(item.reason));
    appendSubcallSummary(body, lines.join("\n"));
    node.appendChild(body);
    return node;
  }

  function programResult(item, index) {
    const node = createNode("li", "subcall");
    if (!isObject(item)) {
      node.appendChild(createNode("span", "subcall-index", String(index)));
      node.appendChild(createNode("span", "subcall-body", Core.stringifyForDisplay(item)));
      return node;
    }
    node.appendChild(createNode("span", "subcall-index", String(index)));
    const head = createNode("span", "subcall-head");
    head.appendChild(createNode("span", "", joinKnown(item.step, item.action_kind, " · ") || "result"));
    if (item.ok === false) head.appendChild(badge("failed", "danger"));
    else if (item.ok === true) head.appendChild(badge("ok", "success"));
    if (item.terminal === true) head.appendChild(badge("terminal", "purple"));
    node.appendChild(head);
    const body = createNode("div", "subcall-body");
    const lines = [item.summary || "result without summary"];
    if (isObject(item.selected) && Object.keys(item.selected).length) {
      lines.push("selected: " + Object.keys(item.selected).join(", "));
    }
    if (Array.isArray(item.missing_paths) && item.missing_paths.length) {
      lines.push("missing paths: " + item.missing_paths.map(function (path) { return Core.stringifyForDisplay(path, 0); }).join(", "));
    }
    appendSubcallSummary(body, lines.join("\n"));
    node.appendChild(body);
    return node;
  }

  function appendSubcallSummary(body, text) {
    if (text.length <= 320) body.textContent = text;
    else body.appendChild(savedSummary("Full subcall summary", text));
  }

  function reviewSection(row) {
    const assessment = row.followingAssessment;
    const section = storySection(
      assessment ? "Reviewed on iteration " + row.assessmentIteration : "Review",
      "the next decision's previous_verification",
    );
    if (!assessment) {
      section.appendChild(missingText(missingAssessmentText(row)));
      return section;
    }
    const line = createNode("div", "review-line");
    line.appendChild(badge(String(assessment.status), assessmentVariant(String(assessment.status))));
    section.appendChild(line);
    section.appendChild(storyList(assessment.evidence, "no evidence recorded"));
    const extra = Object.keys(assessment).filter(function (key) { return key !== "status" && key !== "evidence"; });
    if (extra.length) section.appendChild(jsonDisclosure("Full assessment", assessment));
    return section;
  }

  function missingAssessmentText(row) {
    return row.assessmentLinkStatus === "run_ended"
      ? "Not assessed: no later decision was recorded before the run ended."
      : "Review unavailable: the following AgentWorkingState does not link this outcome.";
  }

  function episodeSection(row) {
    const section = storySection("Episode", "memory writes linked to this decision");
    if (row.closedEpisode) {
      const episode = row.closedEpisode;
      const block = storyBlock("Episode closed");
      block.appendChild(factsGrid([
        ["Record", episode.record_id, "mono"],
        ["Write disposition", row.episodeWriteDisposition],
        ["Recorded", formatTimestamp(row.episodeClosedAt), "mono"],
        ["Verification", isObject(episode.verification) ? episode.verification.status : null],
      ]));
      block.appendChild(jsonDisclosure("Episode record", episode));
      section.appendChild(block);
    }
    if (row.episodeCommit) {
      const commit = row.episodeCommit;
      const block = storyBlock("Memory commit");
      block.appendChild(factsGrid([
        ["Applied", commit.applied],
        ["Revision", joinKnown(commit.previous_revision, commit.new_revision, " → "), "mono"],
        ["Recorded", formatTimestamp(row.episodeCommittedAt), "mono"],
        ["Reason", commit.reason],
      ]));
      block.appendChild(jsonDisclosure("Commit record", commit));
      section.appendChild(block);
    }
    return section;
  }

  /* ---------------------------------------------------------------------- */
  /* State, telemetry and raw data                                          */
  /* ---------------------------------------------------------------------- */

  function renderStateDetail(row) {
    clearNode(elements.stateDetail);
    if (!row) {
      elements.stateDetail.hidden = true;
      return;
    }
    elements.stateDetail.hidden = false;
    const heading = createNode("div", "section-heading");
    const copy = createNode("div");
    copy.appendChild(createNode("p", "eyebrow", "Iteration " + row.iteration));
    copy.appendChild(createNode("h2", "", "State, telemetry and raw data"));
    heading.appendChild(copy);
    heading.appendChild(createNode("p", "", "Everything the trace saved for this decision. Sections open on demand."));
    elements.stateDetail.appendChild(heading);

    elements.stateDetail.appendChild(disclosure("Pre-action EnvironmentState", row.preState.status || null, function (body) {
      body.appendChild(metricTable([
        ["environment", row.preState.profile && row.preState.profile.environment_id],
        ["profile version", row.preState.profile && row.preState.profile.version],
        ["status", row.preState.status],
        ["step", row.progress && row.progress.step],
        ["step limit", row.progress && row.progress.step_limit],
        ["simulation time", row.preTime],
        ["post-action time", row.postTime],
        ["delta simulation time", formatDuration(row.simulationDeltaSeconds)],
        ["recorded at", row.recordedAt],
        ["trace sequence", row.sequence],
      ]));
    }));
    elements.stateDetail.appendChild(jsonDisclosure("Environment decision view", row.decisionView));
    elements.stateDetail.appendChild(jsonDisclosure("Latest observation (full)", row.preObservation));
    elements.stateDetail.appendChild(jsonDisclosure("AgentWorkingState", row.agentWorkingState));
    elements.stateDetail.appendChild(jsonDisclosure("MemoryBrief", row.memoryBrief));
    elements.stateDetail.appendChild(jsonDisclosure("Environment profile", row.context.environment_profile));
    elements.stateDetail.appendChild(disclosure("Model telemetry", row.telemetry ? formatDuration(row.telemetry.duration_seconds) : "missing", function (body) {
      body.appendChild(telemetryTable(row));
    }));
    elements.stateDetail.appendChild(disclosure("Hashes", null, function (body) {
      body.appendChild(metricTable([
        ["context SHA-256", row.payload.context_sha256],
        ["provider instructions SHA-256", row.telemetry && row.telemetry.provider_instructions_sha256],
      ]));
    }));
    elements.stateDetail.appendChild(jsonDisclosure("Capabilities catalog", row.capabilities));
    elements.stateDetail.appendChild(disclosure("Raw DecisionTrace event", "line " + Core.formatScalar(row.lineNumber), function (body) {
      body.appendChild(createNode("pre", "json-block", row.rawLine));
    }));
  }

  function telemetryTable(row) {
    if (!row.telemetry) return metricTable([["telemetry", null]]);
    const telemetry = row.telemetry;
    const usage = telemetry.token_usage || {};
    return metricTable([
      ["provider", telemetry.provider],
      ["requested model", telemetry.requested_model],
      ["reported model", telemetry.reported_model],
      ["requested effort", telemetry.requested_effort],
      ["reported effort", telemetry.reported_effort],
      ["thread mode", telemetry.thread_mode],
      ["attempts", telemetry.attempts],
      ["duration", formatDuration(telemetry.duration_seconds)],
      ["SDK", joinKnown(telemetry.sdk_name, telemetry.sdk_version, " ")],
      ["input tokens", usage.input_tokens],
      ["output tokens", usage.output_tokens],
      ["reasoning tokens", usage.reasoning_output_tokens],
      ["total tokens", usage.total_tokens],
      ["cached input tokens", usage.cached_input_tokens],
    ]);
  }

  /* ---------------------------------------------------------------------- */
  /* Strategy map                                                           */
  /* ---------------------------------------------------------------------- */

  function renderStrategyEpisodes(run) {
    clearNode(elements.strategyEpisodes);
    if (!run || !run.strategyEpisodes.length) return;
    const fragment = document.createDocumentFragment();
    run.strategyEpisodes.forEach(function (episode, index) {
      const node = createNode("button", "strategy-episode");
      node.type = "button";
      node.dataset.episodeId = episode.id;
      node.setAttribute("aria-pressed", episode.id === state.selectedEpisodeId ? "true" : "false");
      if (episode.hasScale || episode.hasFix || episode.hasContradiction) node.classList.add("is-signal");
      node.appendChild(
        createNode(
          "span",
          "strategy-episode-index",
          "Strategy " + (index + 1) + " · #" + episode.startedIteration +
            (episode.endedIteration === episode.startedIteration ? "" : "–" + episode.endedIteration),
        ),
      );
      node.appendChild(createNode("strong", "", episode.strategy || "Decision unavailable"));
      const facts = [episode.rows.length + " decision" + (episode.rows.length === 1 ? "" : "s")];
      if (episode.hasScale) facts.push("scale");
      if (episode.hasFix) facts.push("fix");
      if (episode.hasIncident) facts.push("incident");
      if (episode.hasContradiction) facts.push("contradicted");
      node.appendChild(createNode("span", "strategy-episode-meta", facts.join(" · ")));
      node.addEventListener("click", function () {
        state.selectedEpisodeId = episode.id;
        state.selectedIteration = episode.startedIteration;
        state.cadenceExpanded = false;
        state.mapView.runId = null;
        renderStrategyEpisodes(run);
        renderDecisionMap(run);
        syncTimelineSelection();
        renderDecisionDetail(selectedRow());
        renderStateDetail(selectedRow());
      });
      fragment.appendChild(node);
    });
    elements.strategyEpisodes.appendChild(fragment);
    const selected = elements.strategyEpisodes.querySelector(".strategy-episode[aria-pressed='true']");
    if (selected) selected.scrollIntoView({ inline: "center", block: "nearest" });
  }

  function renderDecisionMap(run) {
    clearNode(elements.mapSvg);
    clearNode(elements.mapMinimap);
    clearNode(elements.mapDetail);
    state.mapView.group = null;
    if (!run || !run.strategyEpisodes.length) {
      elements.mapDetail.appendChild(createNode("div", "empty-panel", "No saved decisions."));
      return;
    }
    let episode = run.strategyEpisodes.find(function (item) {
      return item.id === state.selectedEpisodeId;
    });
    if (!episode) {
      episode = episodeForIteration(run, state.selectedIteration) || run.strategyEpisodes[0];
      state.selectedEpisodeId = episode.id;
    }
    const items = state.cadenceExpanded
      ? episode.rows.map(function (row) { return { kind: "decision", row: row }; })
      : episode.items;
    elements.mapEpisodeLabel.textContent =
      "Strategy · iterations " + episode.startedIteration + "–" + episode.endedIteration;
    elements.mapEpisodeTitle.textContent = episode.strategy || "Decision unavailable";
    elements.mapEpisodeSummary.textContent =
      episode.rows.length + " recorded decisions · links are recorded chronology";
    const hasCadence = episode.items.some(function (item) { return item.kind === "cadence"; });
    elements.mapExpandCadence.hidden = !hasCadence && !state.cadenceExpanded;
    elements.mapExpandCadence.textContent = state.cadenceExpanded ? "Fold cadence" : "Expand cadence";
    const contentWidth =
      MAP_PADDING * 2 +
      items.length * MAP_NODE_WIDTH +
      Math.max(0, items.length - 1) * MAP_NODE_GAP;
    const contentHeight = MAP_PADDING * 2 + MAP_NODE_HEIGHT;
    state.mapView.contentWidth = contentWidth;
    state.mapView.contentHeight = contentHeight;
    setMapViewportBox();

    const definitions = createSvgNode("defs");
    const marker = createSvgNode("marker");
    setAttributes(marker, {
      id: "decision-map-arrow",
      viewBox: "0 0 10 10",
      refX: 9,
      refY: 5,
      markerWidth: 5,
      markerHeight: 5,
      orient: "auto-start-reverse",
    });
    const arrow = createSvgNode("path");
    arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
    marker.appendChild(arrow);
    definitions.appendChild(marker);
    elements.mapSvg.appendChild(definitions);

    const group = createSvgNode("g", "decision-map-world");
    state.mapView.group = group;
    const edgeLayer = createSvgNode("g", "map-chronology-layer");
    const nodeLayer = createSvgNode("g", "map-node-layer");
    for (let index = 1; index < items.length; index += 1) {
      const previousX = mapNodeX(index - 1) + MAP_NODE_WIDTH;
      const nextX = mapNodeX(index);
      const y = MAP_PADDING + MAP_NODE_HEIGHT / 2;
      const line = createSvgNode("line", "map-chronology");
      setAttributes(line, { x1: previousX + 6, x2: nextX - 7, y1: y, y2: y });
      edgeLayer.appendChild(line);
    }
    items.forEach(function (item, index) {
      nodeLayer.appendChild(
        item.kind === "cadence"
          ? renderCadenceMapNode(item, index)
          : renderDecisionMapNode(item.row, index),
      );
    });
    appendChildren(group, edgeLayer, nodeLayer);
    elements.mapSvg.appendChild(group);
    renderMapMinimap(items);

    const mapIdentity = run.runId + ":" + episode.id + ":" + (state.cadenceExpanded ? "expanded" : "folded");
    if (state.mapView.runId !== mapIdentity) {
      state.mapView.runId = mapIdentity;
      state.mapView.scale = 1;
      state.mapView.x = 0;
      state.mapView.y = 0;
      state.mapView.minScale = MAP_MIN_SCALE;
      requestAnimationFrame(function () {
        if (state.tab === "map") fitDecisionMap();
        else updateDecisionMapTransform();
      });
    } else {
      updateDecisionMapTransform();
    }
    updateDecisionMapState(run);
  }

  function renderDecisionMapNode(row, index) {
    const group = createSvgNode("g", "decision-map-node " + decisionMapTone(row));
    group.dataset.iteration = String(row.iteration);
    group.dataset.endIteration = String(row.iteration);
    group.setAttribute("role", "button");
    group.setAttribute("tabindex", row.iteration === state.selectedIteration ? "0" : "-1");
    group.setAttribute(
      "aria-label",
      "Iteration " + row.iteration + ", " + (row.actionName || "missing action") + ". Select for saved reasoning.",
    );
    group.setAttribute("transform", "translate(" + mapNodeX(index) + " " + MAP_PADDING + ")");
    group.addEventListener("click", function () {
      if (state.mapView.moved) return;
      selectIteration(row.iteration, false, true);
    });
    group.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectIteration(row.iteration, false, true);
      }
    });

    const card = createSvgNode("rect", "map-node-card");
    setAttributes(card, { x: 0, y: 0, width: MAP_NODE_WIDTH, height: MAP_NODE_HEIGHT, rx: 10 });
    const tone = createSvgNode("rect", "map-node-tone");
    setAttributes(tone, { x: 0, y: 0, width: 8, height: MAP_NODE_HEIGHT, rx: 4 });
    const title = createSvgNode("title");
    title.textContent =
      "Iteration " + row.iteration + " · " + (row.actionName || "missing") + " · chronology only";
    group.appendChild(title);
    appendChildren(group, card, tone);

    const header = createSvgNode("g", "map-node-header-copy");
    appendMapText(header, 18, 26, "#" + row.iteration, "map-node-iteration");
    appendMapText(header, 66, 26, row.phase || "decision", "map-node-action");
    renderMapNodeMarkers(header, row);
    group.appendChild(header);

    for (const y of [44, 132, 242, 328]) {
      const divider = createSvgNode("line", "map-section-line");
      setAttributes(divider, { x1: 14, x2: MAP_NODE_WIDTH - 14, y1: y, y2: y });
      group.appendChild(divider);
    }

    const copy = createSvgNode("g", "map-node-copy");
    appendMapBlock(
      copy,
      "INPUT",
      (row.facts[0] || (row.preObservation && row.preObservation.summary)),
      56,
      3,
    );
    appendMapText(
      copy,
      18,
      120,
      row.competingHypotheses.length + " hypotheses considered",
      "map-node-hypothesis-count",
    );
    appendMapBlock(
      copy,
      "DECISION",
      (row.actionName || "missing") + " · " + summarizeArguments(row.arguments),
      144,
      2,
    );
    appendMapBlock(copy, "WHY", row.strategy, 190, 2);
    appendMapBlock(
      copy,
      "RESULT",
      row.observation ? row.observation.summary : row.failure ? row.failure.message : null,
      254,
      3,
    );
    const assessment = row.followingAssessment;
    appendMapBlock(
      copy,
      "NEXT REVIEW",
      assessment
        ? assessment.status + (assessment.evidence && assessment.evidence.length ? " · " + assessment.evidence[0] : "")
        : missingAssessmentText(row),
      342,
      3,
    );
    group.appendChild(copy);
    return group;
  }

  function renderCadenceMapNode(item, index) {
    const group = createSvgNode("g", "decision-map-node is-cadence");
    group.dataset.iteration = String(item.startIteration);
    group.dataset.endIteration = String(item.endIteration);
    group.setAttribute("role", "button");
    group.setAttribute("tabindex", "-1");
    group.setAttribute(
      "aria-label",
      "Cadence loop iterations " + item.startIteration + " through " + item.endIteration +
        ", " + item.cycles + " advances. Select to inspect and expand.",
    );
    group.setAttribute("transform", "translate(" + mapNodeX(index) + " " + MAP_PADDING + ")");
    function expandCadence() {
      state.selectedIteration = item.startIteration;
      state.cadenceExpanded = true;
      state.mapView.runId = null;
      renderDecisionMap(selectedRun());
      syncTimelineSelection();
      renderDecisionDetail(selectedRow());
      renderStateDetail(selectedRow());
    }
    group.addEventListener("click", function () {
      if (state.mapView.moved) return;
      expandCadence();
    });
    group.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        expandCadence();
      }
    });
    const card = createSvgNode("rect", "map-node-card map-cadence-card");
    setAttributes(card, { x: 0, y: 0, width: MAP_NODE_WIDTH, height: MAP_NODE_HEIGHT, rx: 12 });
    const tone = createSvgNode("rect", "map-node-tone");
    setAttributes(tone, { x: 0, y: 0, width: 8, height: MAP_NODE_HEIGHT, rx: 4 });
    appendChildren(group, card, tone);
    const copy = createSvgNode("g", "map-node-copy");
    appendMapText(copy, 18, 30, "#" + item.startIteration + "–" + item.endIteration, "map-node-iteration");
    appendMapBlock(copy, "CADENCE LOOP", item.cycles + " time advances · " + item.logReads + " log reads", 74, 2);
    appendMapBlock(copy, "WHY FOLDED", "Same exact strategy; alternating eligible stable cadence without fixes, scaling, failures, DDoS, or contradicted review.", 150, 4);
    appendMapBlock(copy, "OPEN", "Click to reveal every recorded decision.", 356, 2);
    group.appendChild(copy);
    return group;
  }

  function appendMapBlock(parent, label, value, y, maxLines) {
    appendMapText(parent, 18, y, label, "map-node-label");
    const lines = wrapMapText(value, 34, maxLines);
    lines.forEach(function (line, index) {
      appendMapText(parent, 18, y + 20 + index * 16, line, "map-node-text");
    });
    if (!lines.length) appendMapText(parent, 18, y + 20, "missing", "map-node-text is-missing");
  }

  function appendMapText(parent, x, y, value, className) {
    const textNode = createSvgNode("text", className);
    setAttributes(textNode, { x: x, y: y });
    textNode.textContent = isKnown(value) ? String(value) : "missing";
    parent.appendChild(textNode);
    return textNode;
  }

  function wrapMapText(value, maxCharacters, maxLines) {
    if (!isKnown(value) || value === "") return [];
    const words = String(value).replace(/\s+/g, " ").trim().split(" ");
    const lines = [];
    let line = "";
    for (const word of words) {
      const candidate = line ? line + " " + word : word;
      if (candidate.length <= maxCharacters || !line) {
        line = candidate;
      } else {
        lines.push(line);
        line = word;
        if (lines.length === maxLines) break;
      }
    }
    if (lines.length < maxLines && line) lines.push(line);
    if (lines.length === maxLines && words.join(" ").length > lines.join(" ").length) {
      lines[maxLines - 1] = lines[maxLines - 1].slice(0, Math.max(1, maxCharacters - 1)) + "…";
    }
    return lines.slice(0, maxLines);
  }

  function renderMapNodeMarkers(parent, row) {
    const markers = [];
    if (row.incidentCodes.length) markers.push("incident");
    if (row.freeTextIncidentCodes.length) markers.push("text");
    if (row.groups.has("fixes")) markers.push("fix");
    if (row.groups.has("scaling")) markers.push("scale");
    if (row.groups.has("deployments")) markers.push("deploy");
    if (row.activeOperationDuringAdvance) markers.push("operation");
    if (row.followingAssessment && row.followingAssessment.status === "contradicted") {
      markers.push("contradicted");
    }
    markers.slice(0, 6).forEach(function (marker, index) {
      const dot = createSvgNode("circle", "map-marker map-marker-" + marker);
      setAttributes(dot, { cx: MAP_NODE_WIDTH - 16 - index * 12, cy: 20, r: 4 });
      const title = createSvgNode("title");
      title.textContent = marker;
      dot.appendChild(title);
      parent.appendChild(dot);
    });
  }

  function decisionMapTone(row) {
    if (row.incidentCodes.length || row.failedObservation) return "is-incident";
    if (row.freeTextIncidentCodes.length) return "is-heuristic";
    if (row.groups.has("fixes")) return "is-fix";
    if (row.groups.has("scaling")) return "is-scaling";
    if (row.groups.has("deployments")) return "is-deployment";
    if (row.terminalDecision || row.terminalObservation) return "is-terminal";
    if (row.groups.has("advances")) return "is-advance";
    return "is-observation";
  }

  function mapNodeX(index) {
    return MAP_PADDING + index * (MAP_NODE_WIDTH + MAP_NODE_GAP);
  }

  function updateDecisionMapState(run) {
    if (!run || !state.mapView.group) return;
    const visible = new Set(state.visibleRows.map(function (row) { return String(row.iteration); }));
    const episode = run.strategyEpisodes.find(function (item) {
      return item.id === state.selectedEpisodeId;
    });
    elements.mapMatchLabel.textContent = episode
      ? episode.rows.length + " decisions in strategy"
      : "no strategy selected";
    for (const node of state.mapView.group.querySelectorAll(".decision-map-node")) {
      const start = Number(node.dataset.iteration);
      const end = Number(node.dataset.endIteration || node.dataset.iteration);
      const selected = Number(state.selectedIteration) >= start && Number(state.selectedIteration) <= end;
      let matches = false;
      for (let iteration = start; iteration <= end; iteration += 1) {
        if (visible.has(String(iteration))) {
          matches = true;
          break;
        }
      }
      node.classList.toggle("is-selected", selected);
      node.classList.toggle("is-filtered-out", !matches);
      node.setAttribute("tabindex", selected ? "0" : "-1");
    }
    renderDecisionMapDetail(run, selectedRow());
    updateMapMinimapViewport();
  }

  function renderDecisionMapDetail(run, row) {
    clearNode(elements.mapDetail);
    if (!row) {
      elements.mapDetail.appendChild(createNode("div", "empty-panel", "Select a decision node."));
      return;
    }
    renderDecisionStory(elements.mapDetail, row, { compact: true });
  }

  function renderMapMinimap(items) {
    const width = 240;
    const height = 70;
    const padding = 7;
    elements.mapMinimap.setAttribute("viewBox", "0 0 " + width + " " + height);
    const background = createSvgNode("rect", "map-minimap-bg");
    setAttributes(background, { x: 0, y: 0, width: width, height: height, rx: 6 });
    elements.mapMinimap.appendChild(background);
    const plotWidth = width - padding * 2;
    const step = plotWidth / Math.max(1, items.length);
    items.forEach(function (item, index) {
      const row = item.kind === "decision" ? item.row : null;
      const bar = createSvgNode(
        "rect",
        "map-minimap-node " + (item.kind === "cadence" ? "is-cadence" : decisionMapTone(row)),
      );
      setAttributes(bar, {
        x: padding + index * step,
        y: padding + 8,
        width: Math.max(1, step - 0.5),
        height: height - padding * 2 - 16,
      });
      bar.dataset.iteration = String(item.kind === "cadence" ? item.startIteration : row.iteration);
      elements.mapMinimap.appendChild(bar);
    });
    const viewport = createSvgNode("rect", "map-minimap-viewport");
    viewport.dataset.mapViewport = "true";
    elements.mapMinimap.appendChild(viewport);
  }

  function setMapViewportBox() {
    const width = Math.max(320, elements.mapCanvas.clientWidth || 1000);
    const height = Math.max(420, elements.mapCanvas.clientHeight || 600);
    elements.mapSvg.setAttribute("viewBox", "0 0 " + width + " " + height);
  }

  function mapViewportSize() {
    return {
      width: Math.max(320, elements.mapCanvas.clientWidth || 1000),
      height: Math.max(420, elements.mapCanvas.clientHeight || 600),
    };
  }

  function fitDecisionMap() {
    if (!state.mapView.group || !state.mapView.contentWidth) return;
    setMapViewportBox();
    const viewport = mapViewportSize();
    const scale = Math.min(
      (viewport.width - 32) / state.mapView.contentWidth,
      (viewport.height - 32) / state.mapView.contentHeight,
      1,
    );
    state.mapView.minScale = Math.min(MAP_MIN_SCALE, scale);
    state.mapView.scale = Math.max(state.mapView.minScale, scale);
    state.mapView.x = (viewport.width - state.mapView.contentWidth * state.mapView.scale) / 2;
    state.mapView.y = (viewport.height - state.mapView.contentHeight * state.mapView.scale) / 2;
    updateDecisionMapTransform();
  }

  function centerSelectedMapNode() {
    const run = selectedRun();
    if (!run || !run.rows.length || !state.mapView.group) return;
    const nodes = Array.from(state.mapView.group.querySelectorAll(".decision-map-node"));
    const index = nodes.findIndex(function (node) {
      const start = Number(node.dataset.iteration);
      const end = Number(node.dataset.endIteration || node.dataset.iteration);
      return Number(state.selectedIteration) >= start && Number(state.selectedIteration) <= end;
    });
    if (index < 0) return;
    const viewport = mapViewportSize();
    state.mapView.scale = Math.max(state.mapView.scale, 0.9);
    state.mapView.scale = Math.min(state.mapView.scale, MAP_MAX_SCALE);
    const worldX = mapNodeX(index) + MAP_NODE_WIDTH / 2;
    const worldY = MAP_PADDING + MAP_NODE_HEIGHT / 2;
    state.mapView.x = viewport.width / 2 - worldX * state.mapView.scale;
    state.mapView.y = viewport.height / 2 - worldY * state.mapView.scale;
    updateDecisionMapTransform();
  }

  function zoomDecisionMap(factor, clientX, clientY) {
    if (!state.mapView.group) return;
    const rectangle = elements.mapSvg.getBoundingClientRect();
    const localX = clientX === undefined ? rectangle.width / 2 : clientX - rectangle.left;
    const localY = clientY === undefined ? rectangle.height / 2 : clientY - rectangle.top;
    const previousScale = state.mapView.scale;
    const nextScale = Math.max(
      state.mapView.minScale,
      Math.min(MAP_MAX_SCALE, previousScale * factor),
    );
    const worldX = (localX - state.mapView.x) / previousScale;
    const worldY = (localY - state.mapView.y) / previousScale;
    state.mapView.scale = nextScale;
    state.mapView.x = localX - worldX * nextScale;
    state.mapView.y = localY - worldY * nextScale;
    updateDecisionMapTransform();
  }

  function updateDecisionMapTransform() {
    if (!state.mapView.group) return;
    state.mapView.group.setAttribute(
      "transform",
      "translate(" + state.mapView.x + " " + state.mapView.y + ") scale(" + state.mapView.scale + ")",
    );
    elements.mapSvg.classList.toggle("is-map-overview", state.mapView.scale < 0.34);
    elements.mapSvg.classList.toggle("is-map-distant", state.mapView.scale < 0.12);
    elements.mapZoomLabel.textContent = Math.round(state.mapView.scale * 100) + "%";
    updateMapMinimapViewport();
  }

  function updateMapMinimapViewport() {
    const viewportRect = elements.mapMinimap.querySelector("[data-map-viewport]");
    if (!viewportRect || !state.mapView.contentWidth || !state.mapView.scale) return;
    const viewport = mapViewportSize();
    const miniWidth = 240;
    const miniHeight = 70;
    const padding = 7;
    const plotWidth = miniWidth - padding * 2;
    const plotHeight = miniHeight - padding * 2;
    const worldLeft = -state.mapView.x / state.mapView.scale;
    const worldTop = -state.mapView.y / state.mapView.scale;
    const worldWidth = viewport.width / state.mapView.scale;
    const worldHeight = viewport.height / state.mapView.scale;
    const x = padding + (worldLeft / state.mapView.contentWidth) * plotWidth;
    const y = padding + (worldTop / state.mapView.contentHeight) * plotHeight;
    const width = (worldWidth / state.mapView.contentWidth) * plotWidth;
    const height = (worldHeight / state.mapView.contentHeight) * plotHeight;
    setAttributes(viewportRect, {
      x: Math.max(padding, Math.min(miniWidth - padding, x)),
      y: Math.max(padding, Math.min(miniHeight - padding, y)),
      width: Math.max(2, Math.min(plotWidth, width)),
      height: Math.max(2, Math.min(plotHeight, height)),
    });
  }

  function onDecisionMapPointerDown(event) {
    if (event.button !== 0) return;
    state.mapView.dragging = true;
    state.mapView.moved = false;
    state.mapView.pointerId = event.pointerId;
    state.mapView.pointerX = event.clientX;
    state.mapView.pointerY = event.clientY;
    elements.mapSvg.setPointerCapture(event.pointerId);
    elements.mapSvg.classList.add("is-panning");
  }

  function onDecisionMapPointerMove(event) {
    if (!state.mapView.dragging || state.mapView.pointerId !== event.pointerId) return;
    const deltaX = event.clientX - state.mapView.pointerX;
    const deltaY = event.clientY - state.mapView.pointerY;
    if (Math.abs(deltaX) + Math.abs(deltaY) > 2) state.mapView.moved = true;
    state.mapView.x += deltaX;
    state.mapView.y += deltaY;
    state.mapView.pointerX = event.clientX;
    state.mapView.pointerY = event.clientY;
    updateDecisionMapTransform();
  }

  function onDecisionMapPointerUp(event) {
    if (state.mapView.pointerId !== event.pointerId) return;
    state.mapView.dragging = false;
    state.mapView.pointerId = null;
    elements.mapSvg.classList.remove("is-panning");
    setTimeout(function () { state.mapView.moved = false; }, 0);
  }

  function onDecisionMapWheel(event) {
    event.preventDefault();
    zoomDecisionMap(Math.exp(-event.deltaY * 0.0015), event.clientX, event.clientY);
  }

  function onMapMinimapPointer(event) {
    if (!state.mapView.contentWidth || !state.mapView.scale) return;
    const rectangle = elements.mapMinimap.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (event.clientX - rectangle.left) / rectangle.width));
    const worldX = ratio * state.mapView.contentWidth;
    const viewport = mapViewportSize();
    state.mapView.x = viewport.width / 2 - worldX * state.mapView.scale;
    updateDecisionMapTransform();
  }

  /* ---------------------------------------------------------------------- */
  /* Diagnostics                                                            */
  /* ---------------------------------------------------------------------- */

  function renderCharts(run) {
    clearNode(elements.chartsGrid);
    if (!run) return;
    const charts = run.charts;
    const simulationPoints = simulationElapsedPoints(charts.simulationTime);
    elements.chartsGrid.appendChild(
      plotCard(
        "Simulation time by iteration",
        "Elapsed simulator seconds from the first saved post-action clock.",
        [{ name: "simulation elapsed", points: simulationPoints }],
        "line",
      ),
    );
    elements.chartsGrid.appendChild(
      plotCard(
        "Run progress",
        "Saved simulator timestamps / saved horizon; iteration budget only when configured.",
        charts.budgetHorizon.map(cumulativeToPlot),
        "line",
      ),
    );
    elements.chartsGrid.appendChild(
      plotCard(
        "Simulation-time delta",
        "Exact pre/post timestamp difference; missing clocks remain gaps.",
        [{ name: "clock delta", points: charts.simulationDelta.map(metricToPoint) }],
        "scatter",
      ),
    );
    elements.chartsGrid.appendChild(
      plotCard(
        "Time advance requested vs applied",
        "Only advance actions; mismatches remain independently visible in the decision list.",
        [
          { name: "requested", points: charts.advances.map(function (item) { return { x: item.iteration, y: chartNumber(item.requested), meta: "requested" }; }) },
          { name: "applied", points: charts.advances.map(function (item) { return { x: item.iteration, y: chartNumber(item.applied), meta: "applied" }; }) },
        ],
        "scatter",
      ),
    );
    elements.chartsGrid.appendChild(
      plotCard(
        "Cumulative decision durations",
        "Model and execute/reduce Environment durations from DecisionTrace; manifest totals may include other ports.",
        charts.durations.map(cumulativeToPlot),
        "line",
      ),
    );
    elements.chartsGrid.appendChild(
      plotCard(
        "Cumulative token usage",
        "Each series stops after the first missing or unsafe value.",
        charts.tokens.map(cumulativeToPlot),
        "line",
      ),
    );
    elements.chartsGrid.appendChild(actionDistributionCard(charts.actionCounts));
    elements.chartsGrid.appendChild(markerStripCard(charts.markers));
  }

  function plotCard(title, description, series, mode) {
    const card = createNode("article", "chart-card");
    card.appendChild(createNode("h3", "", title));
    card.appendChild(createNode("p", "", description));
    const usable = series.filter(function (item) {
      return item.points.some(function (point) { return point.y !== null && Number.isFinite(point.y); });
    });
    if (!usable.length) {
      card.appendChild(createNode("div", "empty-panel", "No saved measurements."));
      return card;
    }
    const legend = createNode("div", "chart-legend");
    usable.forEach(function (item, index) {
      const legendItem = createNode("span", "legend-item");
      const swatch = createNode("span", "legend-swatch");
      swatch.style.backgroundColor = COLORS[index % COLORS.length];
      appendChildren(legendItem, swatch, createNode("span", "", item.name + (item.incomplete ? " · incomplete" : "")));
      legend.appendChild(legendItem);
    });
    card.appendChild(legend);
    card.appendChild(buildPlot(usable, mode));
    return card;
  }

  function buildPlot(series, mode) {
    const width = 760;
    const height = 210;
    const padding = { left: 54, right: 18, top: 12, bottom: 30 };
    const points = series.flatMap(function (item) {
      return item.points.filter(function (point) { return point.y !== null && Number.isFinite(point.y); });
    });
    let minX = Math.min.apply(null, points.map(function (point) { return point.x; }));
    let maxX = Math.max.apply(null, points.map(function (point) { return point.x; }));
    let minY = Math.min.apply(null, points.map(function (point) { return point.y; }));
    let maxY = Math.max.apply(null, points.map(function (point) { return point.y; }));
    if (minX === maxX) maxX = minX + 1;
    if (minY === maxY) {
      const offset = Math.abs(minY) * 0.05 || 1;
      minY -= offset;
      maxY += offset;
    }
    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom;
    const xScale = function (value) { return padding.left + ((value - minX) / (maxX - minX)) * chartWidth; };
    const yScale = function (value) { return padding.top + chartHeight - ((value - minY) / (maxY - minY)) * chartHeight; };
    const svg = createSvgNode("svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Chart with " + series.length + " series");
    for (let index = 0; index <= 4; index += 1) {
      const y = padding.top + (chartHeight * index) / 4;
      const grid = createSvgNode("line", "chart-grid-line");
      setAttributes(grid, { x1: padding.left, x2: width - padding.right, y1: y, y2: y });
      svg.appendChild(grid);
    }
    const xAxis = createSvgNode("line", "chart-axis");
    setAttributes(xAxis, { x1: padding.left, x2: width - padding.right, y1: height - padding.bottom, y2: height - padding.bottom });
    const yAxis = createSvgNode("line", "chart-axis");
    setAttributes(yAxis, { x1: padding.left, x2: padding.left, y1: padding.top, y2: height - padding.bottom });
    appendChildren(svg, xAxis, yAxis);
    addChartLabel(svg, padding.left, height - 9, String(minX), "start");
    addChartLabel(svg, width - padding.right, height - 9, String(maxX), "end");
    addChartLabel(svg, padding.left - 7, padding.top + 3, compactChartNumber(maxY), "end");
    addChartLabel(svg, padding.left - 7, height - padding.bottom, compactChartNumber(minY), "end");

    series.forEach(function (item, seriesIndex) {
      const color = COLORS[seriesIndex % COLORS.length];
      if (mode === "line") {
        let pathData = "";
        let drawing = false;
        for (const point of item.points) {
          if (point.y === null || !Number.isFinite(point.y)) {
            drawing = false;
            continue;
          }
          pathData += (drawing ? " L " : " M ") + xScale(point.x).toFixed(2) + " " + yScale(point.y).toFixed(2);
          drawing = true;
        }
        if (pathData) {
          const path = createSvgNode("path", "chart-line");
          path.setAttribute("d", pathData);
          path.setAttribute("stroke", color);
          svg.appendChild(path);
        }
      }
      for (const point of item.points) {
        if (point.y === null || !Number.isFinite(point.y)) continue;
        const circle = createSvgNode("circle", "chart-point");
        setAttributes(circle, { cx: xScale(point.x), cy: yScale(point.y), r: mode === "scatter" ? 3.6 : 2.4 });
        circle.setAttribute("fill", color);
        const title = createSvgNode("title");
        title.textContent = item.name + " · iteration " + point.x + " · " + point.y + (point.meta ? " · " + point.meta : "");
        circle.appendChild(title);
        svg.appendChild(circle);
      }
    });
    return svg;
  }

  function setAttributes(element, values) {
    for (const entry of Object.entries(values)) element.setAttribute(entry[0], String(entry[1]));
  }

  function addChartLabel(svg, x, y, value, anchor) {
    const label = createSvgNode("text", "chart-label");
    setAttributes(label, { x: x, y: y, "text-anchor": anchor });
    label.textContent = value;
    svg.appendChild(label);
  }

  function actionDistributionCard(counts) {
    const card = createNode("article", "chart-card");
    card.appendChild(createNode("h3", "", "Action distribution"));
    card.appendChild(createNode("p", "", "Exact count of selected capability names."));
    if (!counts.length) {
      card.appendChild(createNode("div", "empty-panel", "No decisions."));
      return card;
    }
    const max = Math.max.apply(null, counts.map(function (item) { return item.count; }));
    const list = createNode("div", "bar-list");
    for (const item of counts) {
      const row = createNode("div", "bar-row");
      const track = createNode("div", "bar-track");
      const fill = createNode("div", "bar-fill");
      fill.style.width = ((item.count / max) * 100).toFixed(2) + "%";
      track.appendChild(fill);
      appendChildren(row, createNode("span", "", item.name), track, createNode("strong", "", item.count));
      list.appendChild(row);
    }
    card.appendChild(list);
    return card;
  }

  function markerStripCard(markers) {
    const card = createNode("article", "chart-card is-wide");
    card.appendChild(createNode("h3", "", "Incident and operation markers"));
    card.appendChild(
      createNode(
        "p",
        "",
        "Chronological proximity is visible; the viewer does not claim causal error → evidence → mitigation links.",
      ),
    );
    if (!markers.length) {
      card.appendChild(createNode("div", "empty-panel", "No structured markers."));
      return card;
    }
    const strip = createNode("div", "marker-strip");
    for (const marker of markers) {
      const incident = marker.codes.length > 0 || marker.freeTextCodes.length > 0;
      const mitigation = marker.groups.includes("fixes") || marker.groups.includes("scaling") || marker.groups.includes("deployments");
      const event = createNode("div", "marker-event" + (incident ? " is-incident" : mitigation ? " is-mitigation" : ""));
      event.appendChild(createNode("strong", "", "#" + marker.iteration));
      event.appendChild(createNode("span", "", marker.action || "failure"));
      if (marker.codes.length) event.appendChild(createNode("span", "", marker.codes.join(", ")));
      if (marker.freeTextCodes.length) {
        const heuristic = createNode("span", "", marker.freeTextCodes.join(", ") + " · text heuristic");
        heuristic.title = "Exact incident code found only in saved free text";
        event.appendChild(heuristic);
      }
      if (marker.scaleDirection !== "unknown") event.appendChild(createNode("span", "", "requested " + marker.scaleDirection));
      strip.appendChild(event);
    }
    card.appendChild(strip);
    return card;
  }

  function renderTimingSummary(run) {
    clearNode(elements.timingSummary);
    if (!run || !run.timing) return;
    const heading = createNode("div", "section-heading");
    const copy = createNode("div");
    copy.appendChild(createNode("p", "eyebrow", "End of recorded log"));
    copy.appendChild(createNode("h2", "", "Where wall time went"));
    copy.lastChild.id = "timing-summary-title";
    heading.appendChild(copy);
    heading.appendChild(
      createNode(
        "p",
        "",
        formatTimestamp(run.timing.startedAt) + " → " + formatTimestamp(run.timing.finishedAt),
      ),
    );
    elements.timingSummary.appendChild(heading);

    const known = run.timing.phases.filter(function (phase) {
      return typeof phase.durationSeconds === "number" && Number.isFinite(phase.durationSeconds);
    });
    const max = Math.max.apply(null, known.map(function (phase) { return phase.durationSeconds; }).concat([0]));
    const list = createNode("div", "timing-list");
    for (const phase of run.timing.phases) {
      const row = createNode("div", "timing-row");
      row.appendChild(createNode("strong", "", phase.label));
      const track = createNode("div", "timing-track");
      const fill = createNode("div", "timing-fill");
      const width = max > 0 && typeof phase.durationSeconds === "number"
        ? Math.max(0, Math.min(100, (phase.durationSeconds / max) * 100))
        : 0;
      fill.style.width = width.toFixed(2) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(createNode("span", "mono", formatDuration(phase.durationSeconds)));
      row.appendChild(createNode("small", "", phase.note));
      list.appendChild(row);
    }
    elements.timingSummary.appendChild(list);
    const longest = run.timing.longestDecision;
    elements.timingSummary.appendChild(
      createNode(
        "p",
        "formula-note",
        "Durations overlap: Environment includes transport; learner may run after the run; bootstrap may be cached or precede it. " +
          "The trace does not time policy validation or context assembly separately. " +
          (longest
            ? "Slowest decision was #" + longest.iteration + " (" + (longest.actionName || "no action") + ") at " + formatDuration(longest.totalSeconds) + "."
            : "No per-decision timing was recorded."),
      ),
    );
  }

  /* ---------------------------------------------------------------------- */
  /* Memory & experiment                                                    */
  /* ---------------------------------------------------------------------- */

  function renderExperiment() {
    clearNode(elements.experimentContent);
    if (!state.session) return;
    const left = createNode("div", "experiment-column");
    const right = createNode("div", "experiment-column");
    renderMemoryLifecycle(left, selectedRun());
    renderBootstrap(left, state.session.bootstrap);
    renderSpec(left, state.session.spec);
    renderSummary(left, state.session.summary);
    renderComparison(right, state.session.comparison);
    renderCorpus(right, state.session.corpus);
    appendChildren(elements.experimentContent, left, right);
  }

  function renderMemoryLifecycle(parent, run) {
    const card = createNode("article", "experiment-card memory-lifecycle");
    card.appendChild(createNode("p", "eyebrow", "Memory lifecycle"));
    card.appendChild(createNode("h2", "", run ? "Recall, episodes, and learning" : "No run selected"));
    if (!run) {
      card.appendChild(createNode("p", "missing", "No run is selected."));
      parent.appendChild(card);
      return;
    }
    const recalled = run.rows.reduce(function (total, row) {
      const ids = row.retrievalDiagnostics && row.retrievalDiagnostics.selected_record_ids;
      return total + (Array.isArray(ids) ? ids.length : 0);
    }, 0);
    const closed = run.rows.filter(function (row) { return row.closedEpisode; }).length;
    const committed = run.rows.filter(function (row) { return row.episodeCommit && row.episodeCommit.applied; }).length;
    card.appendChild(
      metricTable([
        ["database", (run.memoryView && run.memoryView.database_id) ?? run.manifest.memory_database_id],
        ["start revision", (run.memoryView && run.memoryView.revision) ?? run.manifest.memory_start_revision],
        ["end revision", run.manifest.memory_end_revision],
        ["selected records", recalled],
        ["closed episodes", closed],
        ["committed episodes", committed],
        ["learning operations", run.learningOperations.length],
      ]),
    );
    if (!run.learningOperations.length) {
      card.appendChild(createNode("p", "missing", "No learning stream references this run."));
    }
    for (const operation of run.learningOperations) {
      card.appendChild(learningOperationCard(operation));
    }
    parent.appendChild(card);
  }

  function learningOperationCard(operation) {
    const details = disclosure(operation.operationId || "learning operation", operation.status, function (body) {
      body.appendChild(
        metricTable([
          ["trigger", operation.trigger],
          ["trigger run", operation.triggerRunId],
          ["trigger episode", operation.triggerEpisodeId],
          ["selected runs", operation.selectedRunIds.join(", ")],
          ["selected evidence groups", operation.selectedEvidenceGroupIds.join(", ")],
          ["started", operation.startedAt],
          ["finished", operation.finishedAt],
          ["wall duration", formatDuration(operation.wallDurationSeconds)],
          ["base view", operation.baseView],
          ["end view", operation.endView],
          ["learner duration", operation.learnerTelemetry && formatDuration(operation.learnerTelemetry.duration_seconds)],
        ]),
      );
      for (const evaluation of operation.evaluations) {
        body.appendChild(storyBlock(null, jsonDisclosure("Lesson evaluation · seq " + evaluation.sequence, evaluation)));
      }
      for (const activation of operation.activations) {
        body.appendChild(storyBlock(null, jsonDisclosure("Lesson activation · seq " + activation.sequence, activation)));
      }
      if (operation.failure) body.appendChild(storyBlock(null, jsonDisclosure("Learning failure", operation.failure)));
    });
    details.classList.add("learning-operation");
    return details;
  }

  function renderBootstrap(parent, bootstrap) {
    if (!bootstrap) return;
    const card = createNode("article", "experiment-card");
    card.appendChild(createNode("p", "eyebrow", "Environment bootstrap"));
    card.appendChild(createNode("h2", "", bootstrap.identity && bootstrap.identity.environment_id || "Bootstrap artifact"));
    card.appendChild(
      metricTable([
        ["schema version", bootstrap.schema_version],
        ["profile version", bootstrap.bundle && bootstrap.bundle.profile && bootstrap.bundle.profile.profile_version],
        ["source bytes", bootstrap.source_text ? new TextEncoder().encode(bootstrap.source_text).length : null],
      ]),
    );
    card.appendChild(jsonDisclosure("Bootstrap source and normalized bundle", bootstrap));
    parent.appendChild(card);
  }

  function renderComparison(parent, comparison) {
    if (!comparison) return;
    const card = createNode("article", "experiment-card");
    card.appendChild(createNode("p", "eyebrow", "Paired comparison"));
    card.appendChild(createNode("h2", "", comparison.comparison_id));
    card.appendChild(
      metricTable([
        ["mode", comparison.comparison_mode],
        ["median score: candidate − baseline", comparison.median_candidate_minus_baseline_score],
        ["median cost: candidate − baseline", Core.formatMinor(comparison.median_candidate_minus_baseline_cost_minor).compact],
        ["wins / losses / ties", comparison.candidate_wins + " / " + comparison.candidate_losses + " / " + comparison.ties],
        ["complete paired seeds", comparison.complete_paired_seeds],
        ["partial paired seeds", comparison.partial_paired_seeds],
        ["worst regression", comparison.worst_regression],
      ]),
    );
    parent.appendChild(card);
  }

  function renderSpec(parent, spec) {
    const card = createNode("article", "experiment-card");
    card.appendChild(createNode("p", "eyebrow", "Experiment controls"));
    const ordinaryRun = !spec && state.session.corpus && !state.session.corpus.spec;
    card.appendChild(createNode("h2", "", spec ? spec.experiment_id || "Resolved spec" : ordinaryRun ? "Ordinary run" : "Resolved spec not loaded"));
    if (!spec) {
      card.appendChild(createNode("p", ordinaryRun ? "source-note" : "missing", ordinaryRun
        ? "This corpus comes from an ordinary run; no experiment controls were supplied."
        : "Add spec.resolved.json or summary.json for experiment controls."));
    } else {
      card.appendChild(
        metricTable([
          ["schema version", spec.schema_version],
          ["seeds", Array.isArray(spec.seeds) ? spec.seeds.join(", ") : null],
          ["repeats", spec.repeats],
          ["max iterations", spec.max_iterations],
          ["memory mode", spec.memory && spec.memory.mode],
          ["memory database", spec.memory && spec.memory.database_id],
          ["memory revision", spec.memory && (spec.memory.revision ?? spec.memory.expected_start_revision)],
          ["learning trigger", spec.memory && spec.memory.trigger],
          ["learner model", spec.memory && spec.memory.learner && spec.memory.learner.model],
          ["provider", spec.reasoner && spec.reasoner.provider],
          ["model", spec.reasoner && spec.reasoner.model],
          ["effort", spec.reasoner && spec.reasoner.effort],
          ["thread mode", spec.reasoner && spec.reasoner.thread_mode],
        ]),
      );
      card.appendChild(jsonDisclosure("Raw resolved controls", spec));
    }
    parent.appendChild(card);
  }

  function renderSummary(parent, summary) {
    const card = createNode("article", "experiment-card");
    card.appendChild(createNode("p", "eyebrow", "Experiment report"));
    card.appendChild(createNode("h2", "", summary ? "Attempts and aggregates" : "Summary not loaded"));
    if (!summary) {
      card.appendChild(createNode("p", "missing", "Add summary.json schema v5 for experiment aggregates."));
      parent.appendChild(card);
      return;
    }
    card.appendChild(
      metricTable([
        ["planned attempts", summary.planned_attempts],
        ["attempts with result", summary.attempts_with_result],
        ["failed attempts", summary.failed_attempts],
        ["completed attempts", summary.completed_attempts],
        ["completion rate", formatPercent(summary.completion_rate)],
        ["complete seeds", summary.complete_seeds],
        ["partial seeds", summary.partial_seeds],
        ["insufficient data", summary.insufficient_data],
        ["median score", summary.median_score],
        ["p10 score", summary.p10_score],
        ["median total cost", Core.formatMinor(summary.median_total_cost_minor).compact],
        ["minimum uptime", formatPercent(summary.min_uptime_ratio)],
        ["memory database", summary.memory_database_id],
        ["memory revision", joinKnown(summary.memory_start_revision, summary.memory_end_revision, " → ")],
        ["recalls", summary.recall_count],
        ["selected memory records", summary.selected_memory_records],
        ["memory commits", summary.memory_commit_count],
        ["context bytes", summary.context_bytes],
        ["bootstrap turns / duration", reasonerRoleSummary(summary.bootstrap_reasoner)],
        ["decision turns / duration", reasonerRoleSummary(summary.decision_reasoner)],
        ["learner turns / duration", reasonerRoleSummary(summary.learner_reasoner)],
      ]),
    );
    card.appendChild(
      createNode(
        "p",
        "formula-note",
        "Outcome statistics are copied from the saved report. Data completeness is separate from simulator completion.",
      ),
    );
    const attempts = createNode("div", "attempt-list");
    for (const attempt of summary.attempts || []) attempts.appendChild(attemptRow(attempt));
    card.appendChild(attempts);
    if (Array.isArray(summary.seed_aggregates)) {
      card.appendChild(jsonDisclosure("Seed aggregates", summary.seed_aggregates));
    }
    parent.appendChild(card);
  }

  function attemptRow(attempt) {
    const node = createNode("button", "attempt-row");
    node.type = "button";
    const heading = createNode("span", "attempt-heading");
    heading.appendChild(createNode("span", "", "seed " + attempt.seed + " · repeat " + attempt.repeat_index));
    const status = attempt.result ? attempt.result.status : "failed";
    heading.appendChild(badge(status, status === "completed" ? "success" : status === "failed" ? "danger" : "warning"));
    node.appendChild(heading);
    node.appendChild(createNode("span", "attempt-meta", attempt.run_id || "run_id missing"));
    if (attempt.result) {
      const metrics = Core.collectOutcomeMetrics(attempt.result);
      node.appendChild(createNode("span", "attempt-meta", "score " + Core.formatScalar(metrics.score) +
        " · cost " + Core.formatMinor(metrics.total_cost_minor).compact));
    }
    node.addEventListener("click", function () {
      if (attempt.run_id && state.session.runs.some(function (run) { return run.runId === attempt.run_id; })) {
        state.runId = attempt.run_id;
        const run = selectedRun();
        state.selectedIteration = run.rows.length ? run.rows[0].iteration : null;
        renderAll();
        switchTab("run");
      } else {
        state.notices = [{ level: "warning", text: "Attempt run is not present in the loaded trace." }];
        renderMessages();
      }
    });
    return node;
  }

  function renderCorpus(parent, corpus) {
    const card = createNode("article", "experiment-card");
    card.appendChild(createNode("p", "eyebrow", "Decision corpus"));
    card.appendChild(createNode("h2", "", corpus ? "Search source decisions" : "Corpus not loaded"));
    if (!corpus) {
      card.appendChild(createNode("p", "missing", "Add decision-corpus.json to search by decision_id."));
      parent.appendChild(card);
      return;
    }
    const search = createNode("input", "corpus-search");
    search.type = "search";
    search.placeholder = "Full or prefix decision_id";
    search.value = state.corpusQuery;
    search.setAttribute("aria-label", "Search corpus by decision ID");
    search.addEventListener("input", function () {
      state.corpusQuery = search.value.trim().toLocaleLowerCase();
      renderExperiment();
      const nextSearch = elements.experimentContent.querySelector(".corpus-search");
      if (nextSearch) {
        nextSearch.focus();
        nextSearch.setSelectionRange(state.corpusQuery.length, state.corpusQuery.length);
      }
    });
    card.appendChild(search);
    const matches = corpusMatches(corpus, state.corpusQuery);
    const visible = matches.slice(0, 200);
    card.appendChild(createNode("p", "source-note", visible.length + " shown · " + matches.length + " matched · limit 200"));
    const results = createNode("div", "corpus-results");
    for (const match of visible) results.appendChild(corpusRow(match));
    if (!visible.length) results.appendChild(createNode("div", "empty-panel", "No decision_id matches."));
    card.appendChild(results);
    parent.appendChild(card);

    const selected = state.corpusSelection
      ? matches.find(function (match) {
          return match.decision.decision_id === state.corpusSelection.decisionId && match.run.run_id === state.corpusSelection.runId;
        }) || findCorpusDecision(corpus, state.corpusSelection)
      : null;
    if (selected) parent.appendChild(corpusDetailCard(selected));
  }

  function corpusMatches(corpus, query) {
    const results = [];
    for (const run of corpus.runs || []) {
      for (const decision of run.decisions || []) {
        if (!query || String(decision.decision_id).toLocaleLowerCase().startsWith(query)) {
          results.push({ run: run, decision: decision });
        }
      }
    }
    return results;
  }

  function findCorpusDecision(corpus, selection) {
    for (const run of corpus.runs || []) {
      if (run.run_id !== selection.runId) continue;
      const decision = (run.decisions || []).find(function (item) { return item.decision_id === selection.decisionId; });
      if (decision) return { run: run, decision: decision };
    }
    return null;
  }

  function corpusRow(match) {
    const node = createNode("button", "corpus-row");
    node.type = "button";
    node.appendChild(createNode("strong", "mono", match.decision.decision_id));
    node.appendChild(
      createNode(
        "span",
        "corpus-meta",
        "run " + match.run.run_id + " · seed " + Core.formatScalar(match.decision.seed) + " · repeat " + Core.formatScalar(match.decision.repeat_index) + " · iteration " + match.decision.iteration,
      ),
    );
    node.addEventListener("click", function () {
      state.corpusSelection = { runId: match.run.run_id, decisionId: match.decision.decision_id };
      renderExperiment();
    });
    return node;
  }

  function corpusDetailCard(match) {
    const card = createNode("article", "experiment-card");
    card.appendChild(createNode("p", "eyebrow", "Selected source decision"));
    card.appendChild(createNode("h2", "mono", match.decision.decision_id));
    const open = button("Open run iteration " + match.decision.iteration, "button button-small", function () {
      const run = state.session.runs.find(function (item) { return item.runId === match.run.run_id; });
      if (!run) {
        state.notices = [{ level: "warning", text: "Corpus run is not present in the loaded trace." }];
        renderMessages();
        return;
      }
      state.runId = run.runId;
      state.selectedIteration = match.decision.iteration;
      renderAll();
      switchTab("run");
    });
    card.appendChild(open);
    card.appendChild(
      metricTable([
        ["run_id", match.run.run_id],
        ["seed", match.decision.seed],
        ["repeat_index", match.decision.repeat_index],
        ["iteration", match.decision.iteration],
        ["context SHA-256", match.decision.context_sha256],
        ["system prompt SHA-256", match.decision.system_prompt_sha256],
        ["schema SHA-256", match.decision.schema_sha256],
        ["recalled record IDs", joinList(match.decision.recalled_record_ids)],
      ]),
    );
    card.appendChild(jsonDisclosure("Source context", match.decision.context));
    card.appendChild(jsonDisclosure("Decision", match.decision.decision));
    card.appendChild(jsonDisclosure("Policy result", match.decision.policy_result));
    card.appendChild(jsonDisclosure("Following observation", match.decision.following_observation));
    card.appendChild(jsonDisclosure("Final run outcome", match.run.final_outcome || match.run.final_failure));
    return card;
  }

  /* ---------------------------------------------------------------------- */
  /* Tabs, keyboard, reset                                                  */
  /* ---------------------------------------------------------------------- */

  function switchTab(tabName) {
    state.tab = tabName;
    elements.runPanel.hidden = tabName !== "run";
    elements.mapPanel.hidden = tabName !== "map";
    elements.chartsPanel.hidden = tabName !== "charts";
    elements.experimentPanel.hidden = tabName !== "experiment";
    for (const tab of elements.tabs) {
      const selected = tab.dataset.tab === tabName;
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.tabIndex = selected ? 0 : -1;
    }
    if (tabName === "map") {
      requestAnimationFrame(function () {
        setMapViewportBox();
        if (state.mapView.runId !== state.runId || state.mapView.scale === 1) fitDecisionMap();
        else updateDecisionMapTransform();
      });
    }
    elements.keyboardHelp.textContent =
      tabName === "map"
        ? "Map keys: ←/→ or j/k · +/− zoom · 0 fit · f center"
        : "Keys: ↑/↓ or j/k · Home/End · / search";
  }

  function onTabListKeydown(event) {
    const index = elements.tabs.findIndex(function (tab) { return tab === document.activeElement; });
    if (index < 0) return;
    let next = null;
    if (event.key === "ArrowRight") next = (index + 1) % TAB_ORDER.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + TAB_ORDER.length) % TAB_ORDER.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = TAB_ORDER.length - 1;
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    switchTab(elements.tabs[next].dataset.tab);
    elements.tabs[next].focus();
  }

  function resetFilters(render) {
    state.filters = emptyFilters();
    elements.search.value = "";
    elements.iterationMin.value = "";
    elements.iterationMax.value = "";
    elements.actionFilter.value = "";
    if (render && state.session) renderAll();
  }

  function resetApplication() {
    state.trace = null;
    state.companions = [];
    state.session = null;
    state.runId = null;
    state.selectedIteration = null;
    state.selectedEpisodeId = null;
    state.cadenceExpanded = false;
    state.visibleRows = [];
    state.notices = [];
    state.corpusQuery = "";
    state.corpusSelection = null;
    state.tab = "run";
    state.mapView.runId = null;
    state.mapView.group = null;
    resetFilters(false);
    setFilterDrawer(false);
    clearNode(elements.messages);
    elements.messages.hidden = true;
    elements.workspace.hidden = true;
    elements.dropZone.hidden = false;
    elements.reset.hidden = true;
  }

  function moveSelection(direction, restoreTimelineFocus) {
    if (!state.visibleRows.length) return;
    let index = state.visibleRows.findIndex(function (row) { return row.iteration === state.selectedIteration; });
    if (direction === "home") index = 0;
    else if (direction === "end") index = state.visibleRows.length - 1;
    else index = Math.max(0, Math.min(state.visibleRows.length - 1, index + direction));
    selectIteration(state.visibleRows[index].iteration, true);
    if (restoreTimelineFocus) {
      const selected = elements.timelineRows.querySelector(".timeline-row[aria-current='true']");
      if (selected) selected.focus();
    }
  }

  function moveMapSelection(direction) {
    const run = selectedRun();
    if (!run || !run.rows.length) return;
    let index = run.rows.findIndex(function (row) {
      return Number(row.iteration) === Number(state.selectedIteration);
    });
    index = Math.max(0, Math.min(run.rows.length - 1, index + direction));
    selectIteration(run.rows[index].iteration, false, true);
    centerSelectedMapNode();
    const selected = elements.mapSvg.querySelector(".decision-map-node.is-selected");
    if (selected) selected.focus();
  }

  function onKeyboard(event) {
    if (!state.session || event.metaKey || event.ctrlKey || event.altKey) return;
    const tag = event.target && event.target.tagName ? event.target.tagName.toLocaleLowerCase() : "";
    const editing = tag === "input" || tag === "select" || tag === "textarea" || event.target.isContentEditable;
    const focusedTimelineRow = event.target && typeof event.target.closest === "function"
      ? event.target.closest(".timeline-row")
      : null;
    const otherNativeControl = (tag === "button" || tag === "summary" || tag === "a") && !focusedTimelineRow;
    if (state.tab === "run" && event.key === "/" && !editing) {
      event.preventDefault();
      elements.search.focus();
      return;
    }
    if (editing || (state.tab === "run" && otherNativeControl)) return;
    if (state.tab === "map") {
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        zoomDecisionMap(1.25);
      } else if (event.key === "-" || event.key === "_") {
        event.preventDefault();
        zoomDecisionMap(0.8);
      } else if (event.key === "0") {
        event.preventDefault();
        fitDecisionMap();
      } else if (event.key === "f") {
        event.preventDefault();
        centerSelectedMapNode();
      } else if (event.key === "ArrowRight" || event.key === "j") {
        event.preventDefault();
        moveMapSelection(1);
      } else if (event.key === "ArrowLeft" || event.key === "k") {
        event.preventDefault();
        moveMapSelection(-1);
      }
      return;
    }
    if (state.tab !== "run") return;
    if (event.key === "ArrowDown" || event.key === "j") {
      event.preventDefault();
      moveSelection(1, Boolean(focusedTimelineRow));
    } else if (event.key === "ArrowUp" || event.key === "k") {
      event.preventDefault();
      moveSelection(-1, Boolean(focusedTimelineRow));
    } else if (event.key === "Home") {
      event.preventDefault();
      moveSelection("home", Boolean(focusedTimelineRow));
    } else if (event.key === "End") {
      event.preventDefault();
      moveSelection("end", Boolean(focusedTimelineRow));
    }
  }

  /* ---------------------------------------------------------------------- */
  /* Formatting                                                             */
  /* ---------------------------------------------------------------------- */

  function metricToPoint(item) {
    return { x: Number(item.iteration), y: chartNumber(item.value), meta: item.source };
  }

  function cumulativeToPlot(item) {
    return {
      name: item.name,
      incomplete: item.incomplete,
      points: item.points.map(function (point) { return { x: Number(point.iteration), y: chartNumber(point.value) }; }),
    };
  }

  function simulationElapsedPoints(points) {
    if (!points.length) return [];
    const parsed = points
      .map(function (point) { return { iteration: point.iteration, millis: Date.parse(point.timestamp), source: point.source }; })
      .filter(function (point) { return Number.isFinite(point.millis); });
    if (!parsed.length) return [];
    const base = parsed[0].millis;
    return parsed.map(function (point) {
      return { x: Number(point.iteration), y: (point.millis - base) / 1000, meta: point.source };
    });
  }

  function chartNumber(value) {
    return Core.toChartNumber(value);
  }

  function compactChartNumber(value) {
    if (!Number.isFinite(value)) return "missing";
    return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(value);
  }

  function summarizeArguments(value, limit) {
    const entries = Object.entries(value || {});
    const max = limit || 2;
    if (!entries.length) return "—";
    const parts = entries.slice(0, max).map(function (entry) {
      const rendered = isPlainScalar(entry[1]) ? Core.formatScalar(entry[1]) : Core.stringifyForDisplay(entry[1], 0);
      return entry[0] + "=" + rendered;
    });
    if (entries.length > max) parts.push("+" + (entries.length - max));
    return parts.join(" · ");
  }

  function isPlainScalar(value) {
    return value === null || ["string", "number", "bigint", "boolean"].includes(typeof value);
  }

  function tokenSummary(usage) {
    if (!usage) return "missing";
    return "in " + shortScalar(usage.input_tokens) + " · out " + shortScalar(usage.output_tokens) + " · reasoning " + shortScalar(usage.reasoning_output_tokens);
  }

  function shortScalar(value) {
    if (!isKnown(value)) return "–";
    const number = chartNumber(value);
    return number === null ? Core.formatScalar(value) : compactChartNumber(number);
  }

  function formatSimulationTime(value) {
    if (!value) return "missing";
    return value.replace("T", " ").replace(/(?:\.\d+)?Z$/, "Z");
  }

  function formatTimestamp(value) {
    if (!value) return "missing";
    const parsed = Date.parse(value);
    if (!Number.isFinite(parsed)) return String(value);
    return new Date(parsed).toISOString().replace("T", " ").replace(".000Z", "Z");
  }

  function formatDuration(value) {
    const number = chartNumber(value);
    if (number === null) return "missing";
    if (Math.abs(number) >= 86400) return (number / 86400).toFixed(2) + "d";
    if (Math.abs(number) >= 3600) return (number / 3600).toFixed(2) + "h";
    if (Math.abs(number) >= 60) return (number / 60).toFixed(2) + "m";
    return number.toFixed(number >= 10 ? 2 : 3).replace(/\.0+$/, "") + "s";
  }

  function formatPercent(value) {
    const number = chartNumber(value);
    return number === null ? "missing" : (number * 100).toFixed(1) + "%";
  }

  function reasonerRoleSummary(role) {
    if (!role) return null;
    return Core.formatScalar(role.turns) + " / " + formatDuration(role.duration_seconds);
  }

  function displayValue(value) {
    if (Array.isArray(value) || (value && typeof value === "object")) return Core.stringifyForDisplay(value, 0);
    return Core.formatScalar(value);
  }

  function joinKnown(left, right, separator) {
    const values = [left, right].filter(function (value) { return isKnown(value) && value !== ""; });
    return values.length ? values.join(separator) : null;
  }

  function joinList(value) {
    return Array.isArray(value) && value.length ? value.join(", ") : null;
  }

  /* ---------------------------------------------------------------------- */
  /* Wiring                                                                 */
  /* ---------------------------------------------------------------------- */

  elements.picker.addEventListener("change", function () { loadFiles(elements.picker.files); });
  elements.reset.addEventListener("click", resetApplication);
  elements.runSelect.addEventListener("change", function () {
    state.runId = elements.runSelect.value;
    const run = selectedRun();
    state.selectedIteration = defaultStoryIteration(run);
    const episode = episodeForIteration(run, state.selectedIteration);
    state.selectedEpisodeId = episode ? episode.id : null;
    state.cadenceExpanded = false;
    state.mapView.runId = null;
    resetFilters(false);
    renderAll();
  });
  for (const tab of elements.tabs) {
    tab.addEventListener("click", function () { switchTab(tab.dataset.tab); });
  }
  const tabList = document.querySelector("[role='tablist']");
  if (tabList) tabList.addEventListener("keydown", onTabListKeydown);
  elements.search.addEventListener("input", function () {
    state.filters.text = elements.search.value;
    renderSelectionDependents();
  });
  elements.actionFilter.addEventListener("change", function () {
    state.filters.action = elements.actionFilter.value;
    renderSelectionDependents();
  });
  elements.filterToggle.addEventListener("click", function () {
    setFilterDrawer(elements.filterDrawer.hidden);
  });
  elements.iterationMin.addEventListener("input", function () {
    state.filters.minIteration = elements.iterationMin.value ? Number(elements.iterationMin.value) : null;
    renderSelectionDependents();
  });
  elements.iterationMax.addEventListener("input", function () {
    state.filters.maxIteration = elements.iterationMax.value ? Number(elements.iterationMax.value) : null;
    renderSelectionDependents();
  });
  elements.clearFilters.addEventListener("click", function () { resetFilters(true); });
  elements.mapZoomOut.addEventListener("click", function () { zoomDecisionMap(0.8); });
  elements.mapZoomIn.addEventListener("click", function () { zoomDecisionMap(1.25); });
  elements.mapFit.addEventListener("click", fitDecisionMap);
  elements.mapCenter.addEventListener("click", centerSelectedMapNode);
  elements.mapExpandCadence.addEventListener("click", function () {
    state.cadenceExpanded = !state.cadenceExpanded;
    state.mapView.runId = null;
    renderDecisionMap(selectedRun());
  });
  elements.mapSvg.addEventListener("wheel", onDecisionMapWheel, { passive: false });
  elements.mapSvg.addEventListener("pointerdown", onDecisionMapPointerDown);
  elements.mapSvg.addEventListener("pointermove", onDecisionMapPointerMove);
  elements.mapSvg.addEventListener("pointerup", onDecisionMapPointerUp);
  elements.mapSvg.addEventListener("pointercancel", onDecisionMapPointerUp);
  elements.mapMinimap.addEventListener("pointerdown", onMapMinimapPointer);
  window.addEventListener("resize", function () {
    if (state.session && state.tab === "map") {
      setMapViewportBox();
      updateDecisionMapTransform();
    }
  });
  document.addEventListener("keydown", onKeyboard);

  for (const eventName of ["dragenter", "dragover"]) {
    elements.dropZone.addEventListener(eventName, function (event) {
      event.preventDefault();
      elements.dropZone.classList.add("is-dragging");
    });
  }
  for (const eventName of ["dragleave", "drop"]) {
    elements.dropZone.addEventListener(eventName, function (event) {
      event.preventDefault();
      elements.dropZone.classList.remove("is-dragging");
    });
  }
  elements.dropZone.addEventListener("drop", function (event) {
    loadFiles(event.dataTransfer.files);
  });
})();
