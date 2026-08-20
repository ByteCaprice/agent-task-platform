(function () {
  "use strict";

  const state = {
    board: null,
    detail: null,
    activeTab: "overview",
    refreshTimer: null,
    density: "comfortable",
    optionSets: { route_tags: new Set(), agents: new Set(), callers: new Set(), statuses: new Set() },
  };

  const elements = {
    refresh: document.getElementById("refresh-button"),
    search: document.getElementById("search"),
    routeTag: document.getElementById("route-tag"),
    agent: document.getElementById("agent"),
    caller: document.getElementById("caller"),
    status: document.getElementById("status"),
    sort: document.getElementById("sort"),
    densityButtons: Array.from(document.querySelectorAll("[data-density]")),
    autoRefresh: document.getElementById("auto-refresh"),
    connectionStatus: document.getElementById("connection-status"),
    summary: document.getElementById("summary"),
    updatedAt: document.getElementById("updated-at"),
    error: document.getElementById("error-banner"),
    board: document.getElementById("board"),
    backdrop: document.getElementById("drawer-backdrop"),
    drawer: document.getElementById("run-drawer"),
    drawerStatus: document.getElementById("drawer-status"),
    drawerTitle: document.getElementById("drawer-title"),
    drawerRunId: document.getElementById("drawer-run-id"),
    drawerMetrics: document.getElementById("drawer-metrics"),
    drawerActions: document.getElementById("drawer-actions"),
    drawerContent: document.getElementById("drawer-content"),
    drawerClose: document.getElementById("drawer-close"),
    tabs: Array.from(document.querySelectorAll(".drawer-tab")),
  };

  function createElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function showError(message) {
    elements.error.textContent = message;
    elements.error.hidden = !message;
  }

  async function api(path, options) {
    const request = Object.assign({}, options || {});
    request.headers = Object.assign({}, request.headers || {});
    const response = await window.fetch(path, request);
    const contentType = response.headers.get("content-type") || "";
    const body = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const detail = body && body.detail
        ? body.detail
        : (body && body.error && body.error.message ? body.error.message : body);
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return body;
  }

  function boardQuery() {
    const params = new URLSearchParams();
    const values = {
      search: elements.search.value.trim(),
      route_tag: elements.routeTag.value,
      agent: elements.agent.value,
      caller: elements.caller.value,
      status: elements.status.value,
    };
    Object.keys(values).forEach(function (key) {
      if (values[key]) params.set(key, values[key]);
    });
    params.set("limit", "300");
    return params.toString();
  }

  async function loadBoard(refreshDetail) {
    elements.refresh.disabled = true;
    setConnectionState("syncing", "Syncing");
    try {
      const board = await api("/v1/kanban/board?" + boardQuery());
      state.board = board;
      mergeOptions(board.filters || {});
      renderSummary(board.summary || {});
      renderBoard(board.columns || []);
      elements.updatedAt.textContent = "Updated " + formatTimestamp(board.generated_at);
      setConnectionState("online", "Live");
      showError("");
      if (refreshDetail !== false) await refreshOpenRun();
    } catch (error) {
      setConnectionState("error", "Unavailable");
      showError("Unable to load board: " + error.message);
      if (!state.board) renderEmptyBoard("The board is temporarily unavailable.");
    } finally {
      elements.refresh.disabled = false;
    }
  }

  function setConnectionState(stateName, label) {
    elements.connectionStatus.className = "connection-status connection-status--" + stateName;
    elements.connectionStatus.lastElementChild.textContent = label;
  }

  function mergeOptions(filters) {
    Object.keys(state.optionSets).forEach(function (key) {
      (filters[key] || []).forEach(function (value) { state.optionSets[key].add(value); });
    });
    populateSelect(elements.routeTag, state.optionSets.route_tags, "All route tags");
    populateSelect(elements.agent, state.optionSets.agents, "All agents");
    populateSelect(elements.caller, state.optionSets.callers, "All callers");
    populateSelect(elements.status, state.optionSets.statuses, "All statuses");
  }

  function populateSelect(select, values, label) {
    const selected = select.value;
    select.replaceChildren(new Option(label, ""));
    Array.from(values).sort().forEach(function (value) {
      select.appendChild(new Option(value, value));
    });
    select.value = selected;
  }

  function renderSummary(summary) {
    const metrics = [
      ["Runs", summary.total || 0, "neutral"],
      ["Queued", summary.queued || 0, "queued"],
      ["Active", summary.active || 0, "running"],
      ["Callback", summary.waiting_callback || 0, "callback"],
      ["Succeeded", summary.succeeded || 0, "succeeded"],
      ["Needs attention", summary.failed || 0, "failed"],
      ["Success rate", summary.success_rate === null || summary.success_rate === undefined ? "-" : Math.round(summary.success_rate * 100) + "%", "rate"],
    ];
    elements.summary.replaceChildren();
    metrics.forEach(function (metric) {
      const item = createElement("div", "summary-item summary-item--" + metric[2]);
      item.appendChild(createElement("span", "summary-label", metric[0]));
      item.appendChild(createElement("strong", "summary-value", metric[1]));
      elements.summary.appendChild(item);
    });
  }

  function renderBoard(columns) {
    elements.board.replaceChildren();
    columns.forEach(function (column) {
      const section = createElement("section", "board-column board-column--" + column.id);
      const header = createElement("header", "column-header");
      const title = createElement("div", "column-title");
      title.appendChild(createElement("span", "column-dot", ""));
      title.appendChild(createElement("h2", "", column.label));
      header.appendChild(title);
      header.appendChild(createElement("span", "column-count", column.count));
      section.appendChild(header);

      const cards = createElement("div", "column-cards");
      if (!column.cards.length) cards.appendChild(createElement("div", "column-empty", "No runs"));
      sortedCards(column.cards).forEach(function (card) { cards.appendChild(renderCard(card)); });
      section.appendChild(cards);
      elements.board.appendChild(section);
    });
  }

  function sortedCards(cards) {
    const sorted = cards.slice();
    const mode = elements.sort.value;
    sorted.sort(function (left, right) {
      if (mode === "newest") return timestamp(right.create_time) - timestamp(left.create_time);
      if (mode === "oldest") return timestamp(left.create_time) - timestamp(right.create_time);
      if (mode === "longest") return (right.duration_seconds || 0) - (left.duration_seconds || 0);
      return (right.priority || 0) - (left.priority || 0)
        || timestamp(right.create_time) - timestamp(left.create_time);
    });
    return sorted;
  }

  function renderCard(card) {
    const button = createElement("button", "run-card run-card--" + statusTone(card.status));
    const isLongRunning = statusTone(card.status) === "running" && (card.duration_seconds || 0) >= 300;
    if (isLongRunning) button.classList.add("run-card--long-running");
    button.type = "button";
    button.dataset.runId = card.run_id;
    button.addEventListener("click", function () { openRun(card.run_id); });

    const top = createElement("div", "card-topline");
    const flags = createElement("div", "card-flags");
    flags.appendChild(createElement("span", "status-badge status-badge--" + statusTone(card.status), card.status));
    if (card.priority) flags.appendChild(createElement("span", "priority-badge", "P" + card.priority));
    top.appendChild(flags);
    top.appendChild(createElement("span", "card-age" + (isLongRunning ? " card-age--warning" : ""), timeAgo(card.create_time) + " old"));
    button.appendChild(top);
    button.appendChild(createElement("h3", "card-title", card.route_tag));
    const identity = createElement("div", "card-identity");
    const requestId = createElement("code", "card-request-id", card.request_id || "No request ID");
    requestId.title = card.request_id || "";
    identity.appendChild(requestId);
    const runId = createElement("code", "card-run-id", shortId(card.run_id));
    runId.title = card.run_id;
    identity.appendChild(runId);
    button.appendChild(identity);

    const meta = createElement("dl", "card-meta");
    appendMeta(meta, "Agent", card.agent ? card.agent.name + "@" + card.agent.version : "Unresolved");
    appendMeta(meta, "Caller", card.caller);
    appendMeta(meta, "Step", card.current_step);
    appendMeta(meta, "Worker", card.worker || "Not assigned");
    button.appendChild(meta);

    const stats = createElement("div", "card-stats");
    stats.appendChild(createElement("span", "", "Attempt " + card.attempts + "/" + card.max_attempts));
    if (card.duration_seconds !== null) stats.appendChild(createElement("span", "", formatDuration(card.duration_seconds)));
    button.appendChild(stats);

    if (card.error_message) button.appendChild(createElement("p", "card-error", card.error_message));
    if (card.callback_status && card.callback_status !== "PENDING") {
      button.appendChild(createElement("span", "callback-state", "Callback " + card.callback_status));
    }
    return button;
  }

  function appendMeta(list, label, value) {
    const row = createElement("div", "meta-row");
    row.appendChild(createElement("dt", "", label));
    row.appendChild(createElement("dd", "", value || "-"));
    list.appendChild(row);
  }

  function renderEmptyBoard(message) {
    elements.board.replaceChildren(createElement("div", "board-empty", message));
  }

  async function openRun(runId) {
    elements.drawer.setAttribute("aria-hidden", "false");
    elements.backdrop.hidden = false;
    elements.drawer.classList.add("is-open");
    elements.drawerContent.replaceChildren(createElement("div", "drawer-loading", "Loading run..."));
    try {
      state.detail = await api("/v1/kanban/runs/" + encodeURIComponent(runId));
      state.activeTab = "overview";
      renderDrawer();
    } catch (error) {
      elements.drawerContent.replaceChildren(createElement("div", "drawer-error", error.message));
    }
  }

  async function refreshOpenRun() {
    if (!state.detail || !elements.drawer.classList.contains("is-open")) return;
    const runId = state.detail.run.run_id;
    try {
      const detail = await api("/v1/kanban/runs/" + encodeURIComponent(runId));
      if (elements.drawer.classList.contains("is-open") && state.detail && state.detail.run.run_id === runId) {
        state.detail = detail;
        renderDrawer();
      }
    } catch (_error) {
      // Keep the last good detail view while the board-level error reports connectivity.
    }
  }

  function closeDrawer() {
    elements.drawer.classList.remove("is-open");
    elements.drawer.setAttribute("aria-hidden", "true");
    elements.backdrop.hidden = true;
    state.detail = null;
  }

  function renderDrawer() {
    if (!state.detail) return;
    const run = state.detail.run;
    elements.drawerStatus.className = "status-badge status-badge--" + statusTone(run.status);
    elements.drawerStatus.textContent = run.status;
    elements.drawerTitle.textContent = run.route_tag;
    elements.drawerRunId.textContent = run.run_id;
    elements.tabs.forEach(function (tab) {
      tab.classList.toggle("is-active", tab.dataset.tab === state.activeTab);
    });
    renderDrawerMetrics(run);
    renderActions();
    if (state.activeTab === "overview") renderOverview(run);
    if (state.activeTab === "input") renderJsonPanel("Input", run.input, run.files);
    if (state.activeTab === "output") renderJsonPanel("Output", run.output, null);
    if (state.activeTab === "timeline") renderTimeline();
  }

  function renderDrawerMetrics(run) {
    const modelCalls = state.detail.model_calls || [];
    const callbacks = state.detail.callbacks || [];
    const totalTokens = modelCalls.reduce(function (total, call) { return total + (call.total_tokens || 0); }, 0);
    const callbackAttempts = callbacks.reduce(function (total, callback) { return total + (callback.attempts || 0); }, 0);
    const duration = run.start_time
      ? Math.max(0, (timestamp(run.finish_time) || Date.now()) - timestamp(run.start_time)) / 1000
      : null;
    const metrics = [
      ["Duration", duration === null ? "-" : formatDuration(duration)],
      ["Attempts", run.attempts + " / " + run.max_attempts],
      ["Model calls", modelCalls.length],
      ["Tokens", totalTokens ? totalTokens.toLocaleString() : "-"],
      ["Callback tries", callbackAttempts],
    ];
    elements.drawerMetrics.replaceChildren();
    metrics.forEach(function (metric) {
      const item = createElement("div", "drawer-metric");
      item.appendChild(createElement("span", "", metric[0]));
      item.appendChild(createElement("strong", "", metric[1]));
      elements.drawerMetrics.appendChild(item);
    });
  }

  function renderActions() {
    elements.drawerActions.replaceChildren();
    const actions = state.detail.actions || {};
    if (actions.cancel) elements.drawerActions.appendChild(actionButton("Cancel run", "danger", cancelRun));
    if (actions.retry) elements.drawerActions.appendChild(actionButton("Retry run", "primary", retryRun));
    if (actions.resend_callback) elements.drawerActions.appendChild(actionButton("Resend callback", "", resendCallback));
  }

  function actionButton(label, tone, handler) {
    const button = createElement("button", "button" + (tone ? " button-" + tone : ""), label);
    button.type = "button";
    button.addEventListener("click", handler);
    return button;
  }

  async function runAction(path, confirmation) {
    if (confirmation && !window.confirm(confirmation)) return;
    Array.from(elements.drawerActions.querySelectorAll("button")).forEach(function (button) { button.disabled = true; });
    try {
      await api(path, { method: "POST" });
      const runId = state.detail.run.run_id;
      await loadBoard(false);
      state.detail = await api("/v1/kanban/runs/" + encodeURIComponent(runId));
      renderDrawer();
      showError("");
    } catch (error) {
      showError("Action failed: " + error.message);
      renderActions();
    }
  }

  function cancelRun() {
    const runId = state.detail.run.run_id;
    return runAction("/v1/kanban/runs/" + encodeURIComponent(runId) + "/cancel", "Cancel this run?");
  }

  function retryRun() {
    const runId = state.detail.run.run_id;
    return runAction("/v1/kanban/runs/" + encodeURIComponent(runId) + "/retry");
  }

  function resendCallback() {
    const runId = state.detail.run.run_id;
    return runAction("/v1/kanban/runs/" + encodeURIComponent(runId) + "/callbacks/resend");
  }

  function renderOverview(run) {
    const content = createElement("div", "detail-stack");
    const facts = createElement("dl", "detail-grid");
    [
      ["Request ID", run.request_id],
      ["Trace ID", run.trace_id],
      ["Caller", run.caller],
      ["Agent", run.agent ? run.agent.name + "@" + run.agent.version : "Unresolved"],
      ["Current step", run.current_step],
      ["Worker", run.worker],
      ["Attempts", run.attempts + " / " + run.max_attempts],
      ["Priority", run.priority],
      ["Callback", run.callback_status],
      ["Created", formatTimestamp(run.create_time)],
      ["Started", formatTimestamp(run.start_time)],
      ["Finished", formatTimestamp(run.finish_time)],
    ].forEach(function (item) { appendMeta(facts, item[0], item[1]); });
    content.appendChild(facts);
    if (run.error_message || run.dead_letter_reason) {
      const error = createElement("section", "detail-section detail-section--error");
      error.appendChild(createElement("h3", "", run.error_type || "Execution error"));
      error.appendChild(createElement("p", "", run.error_message || run.dead_letter_reason));
      content.appendChild(error);
    }
    elements.drawerContent.replaceChildren(content);
  }

  function renderJsonPanel(label, value, files) {
    const content = createElement("div", "detail-stack");
    const section = createElement("section", "detail-section");
    section.appendChild(jsonPanelHeader(label, value));
    const pre = createElement("pre", "json-view");
    pre.textContent = JSON.stringify(value === undefined ? null : value, null, 2);
    section.appendChild(pre);
    content.appendChild(section);
    if (files && files.length) {
      const fileSection = createElement("section", "detail-section");
      fileSection.appendChild(jsonPanelHeader("Files", files));
      const filePre = createElement("pre", "json-view");
      filePre.textContent = JSON.stringify(files, null, 2);
      fileSection.appendChild(filePre);
      content.appendChild(fileSection);
    }
    elements.drawerContent.replaceChildren(content);
  }

  function jsonPanelHeader(label, value) {
    const header = createElement("div", "detail-section-header");
    header.appendChild(createElement("h3", "", label));
    const copy = createElement("button", "copy-button", "Copy");
    copy.type = "button";
    copy.addEventListener("click", async function () {
      try {
        await navigator.clipboard.writeText(JSON.stringify(value === undefined ? null : value, null, 2));
        copy.textContent = "Copied";
        window.setTimeout(function () { copy.textContent = "Copy"; }, 1200);
      } catch (error) {
        showError("Unable to copy JSON: " + error.message);
      }
    });
    header.appendChild(copy);
    return header;
  }

  function renderTimeline() {
    const events = [];
    (state.detail.logs || []).forEach(function (log) {
      const category = String(log.component || "run").toLowerCase().includes("tool") ? "tool" : "run";
      events.push({ time: log.create_time, kind: log.event_type, source: log.component, text: log.message, tone: log.level, category: category });
    });
    (state.detail.model_calls || []).forEach(function (call) {
      const tokens = call.total_tokens ? " - " + call.total_tokens + " tokens" : "";
      events.push({ time: call.finish_time || call.start_time, kind: "Model " + call.status, source: call.model || call.provider, text: (call.error || call.output_summary || "Model call") + tokens, tone: call.error ? "ERROR" : "INFO", category: "model" });
    });
    (state.detail.callbacks || []).forEach(function (callback) {
      events.push({ time: callback.update_time, kind: "Callback " + callback.status, source: "callback", text: callback.last_error || (callback.attempts + " attempt(s)"), tone: callback.status === "FAILED" ? "ERROR" : "INFO", category: "callback" });
    });
    events.sort(function (a, b) { return new Date(b.time || 0) - new Date(a.time || 0); });
    const timeline = createElement("div", "timeline");
    if (!events.length) timeline.appendChild(createElement("div", "timeline-empty", "No events recorded"));
    events.forEach(function (event) {
      const item = createElement("article", "timeline-item timeline-item--" + String(event.tone || "INFO").toLowerCase() + " timeline-item--" + event.category);
      const header = createElement("div", "timeline-header");
      const label = createElement("div", "timeline-label");
      label.appendChild(createElement("span", "timeline-category", event.category));
      label.appendChild(createElement("strong", "", event.kind));
      header.appendChild(label);
      header.appendChild(createElement("time", "", formatTimestamp(event.time)));
      item.appendChild(header);
      item.appendChild(createElement("div", "timeline-source", event.source));
      item.appendChild(createElement("p", "", event.text));
      timeline.appendChild(item);
    });
    elements.drawerContent.replaceChildren(timeline);
  }

  function statusTone(status) {
    if (["CREATED", "QUEUED"].includes(status)) return "queued";
    if (["RUNNING", "WAITING_TOOL", "RETRYING"].includes(status)) return "running";
    if (["AGENT_SUCCEEDED", "WAITING_CALLBACK"].includes(status)) return "callback";
    if (status === "SUCCEEDED") return "succeeded";
    return "failed";
  }

  function formatTimestamp(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString();
  }

  function timeAgo(value) {
    if (!value) return "-";
    const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    if (seconds < 60) return Math.floor(seconds) + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h";
    return Math.floor(seconds / 86400) + "d";
  }

  function formatDuration(seconds) {
    if (seconds < 1) return Math.round(seconds * 1000) + "ms";
    if (seconds < 60) return seconds.toFixed(seconds < 10 ? 1 : 0) + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m " + Math.floor(seconds % 60) + "s";
    return Math.floor(seconds / 3600) + "h " + Math.floor((seconds % 3600) / 60) + "m";
  }

  function timestamp(value) {
    if (!value) return 0;
    const parsed = new Date(value).getTime();
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  function shortId(value) {
    if (!value || value.length <= 16) return value || "-";
    return value.slice(0, 8) + "..." + value.slice(-5);
  }

  function setDensity(density) {
    state.density = density;
    document.body.classList.toggle("density-compact", density === "compact");
    elements.densityButtons.forEach(function (button) {
      const active = button.dataset.density === density;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    try { window.localStorage.setItem("agent-task-platform-kanban-density", density); } catch (_error) { /* Storage is optional. */ }
  }

  function resetRefreshTimer() {
    if (state.refreshTimer) window.clearInterval(state.refreshTimer);
    state.refreshTimer = null;
    if (elements.autoRefresh.checked) state.refreshTimer = window.setInterval(loadBoard, 5000);
  }

  let searchTimer = null;
  elements.search.addEventListener("input", function () {
    if (searchTimer) window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(loadBoard, 300);
  });
  [elements.routeTag, elements.agent, elements.caller, elements.status].forEach(function (select) {
    select.addEventListener("change", loadBoard);
  });
  elements.sort.addEventListener("change", function () {
    if (state.board) renderBoard(state.board.columns || []);
  });
  elements.densityButtons.forEach(function (button) {
    button.addEventListener("click", function () { setDensity(button.dataset.density); });
  });
  elements.refresh.addEventListener("click", function () { loadBoard(); });
  elements.autoRefresh.addEventListener("change", resetRefreshTimer);
  elements.drawerClose.addEventListener("click", closeDrawer);
  elements.backdrop.addEventListener("click", closeDrawer);
  elements.tabs.forEach(function (tab) {
    tab.addEventListener("click", function () { state.activeTab = tab.dataset.tab; renderDrawer(); });
  });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape") closeDrawer(); });

  let initialDensity = "comfortable";
  try { initialDensity = window.localStorage.getItem("agent-task-platform-kanban-density") || initialDensity; } catch (_error) { /* Storage is optional. */ }
  setDensity(initialDensity === "compact" ? "compact" : "comfortable");
  resetRefreshTimer();
  loadBoard();
}());
