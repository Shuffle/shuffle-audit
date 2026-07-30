(function bootstrapAuditDashboard(globalScope) {
  "use strict";

  const numberFormatter = new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 2,
  });
  const compactFormatter = new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  });

  const metricConfig = {
    monthly_app_runs: { label: "Monthly app runs", compact: true },
    workflow_count: { label: "Workflows", compact: false },
    environment_count: { label: "Environments", compact: false },
    node_count: { label: "Nodes", compact: true },
  };

  const state = {
    report: null,
    organizations: [],
    monthlySeries: [],
    selectedMonth: "",
    activeMetric: "monthly_app_runs",
    sortKey: "monthly_app_runs",
    sortDirection: "desc",
    search: "",
  };

  function asNumber(value, fallback = 0) {
    const numeric = Number(value);
    return Number.isFinite(numeric) && numeric >= 0 ? numeric : fallback;
  }

  function asNullableNumber(value) {
    if (value === null || value === undefined) {
      return null;
    }
    const numeric = Number(value);
    return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
  }

  function cleanString(value, fallback = "") {
    return typeof value === "string" ? value.trim() : fallback;
  }

  function normalizeOrganization(value, index) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(`metrics.per_org[${index}] must be an object`);
    }

    const orgName = cleanString(value.org_name, "Unnamed organization");
    const orgId = cleanString(value.org_id);
    if (!orgId) {
      throw new Error(`metrics.per_org[${index}] is missing org_id`);
    }

    const actionCount = asNullableNumber(value.action_count);
    const nodeCount = asNullableNumber(value.node_count);
    return {
      org_id: orgId,
      org_name: orgName,
      parent_org_id: cleanString(value.parent_org_id) || null,
      parent_org_name: cleanString(value.parent_org_name) || null,
      environment_count: asNullableNumber(value.environment_count),
      workflow_count: asNullableNumber(value.workflow_count),
      action_count: actionCount,
      node_count: nodeCount,
      trigger_count:
        actionCount !== null && nodeCount !== null
          ? Math.max(0, nodeCount - actionCount)
          : null,
      average_actions_per_workflow: asNullableNumber(
        value.average_actions_per_workflow,
      ),
      average_nodes_per_workflow: asNullableNumber(
        value.average_nodes_per_workflow,
      ),
      monthly_app_runs: asNullableNumber(value.monthly_app_runs),
    };
  }

  function normalizeMonthlySeries(rawSeries, reportingMonth, currentTotal, organizations) {
    const fallback = [
      {
        month: reportingMonth || "Current",
        monthly_app_runs: currentTotal,
        source: "current-month-counter",
        per_org: organizations.map((org) => ({
          org_id: org.org_id,
          org_name: org.org_name,
          monthly_app_runs: org.monthly_app_runs,
        })),
      },
    ];
    if (!Array.isArray(rawSeries) || rawSeries.length === 0) {
      return fallback;
    }

    const byMonth = new Map();
    rawSeries.forEach((entry, index) => {
      if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
        throw new Error(`metrics.monthly_app_runs_by_month[${index}] must be an object`);
      }
      const month = cleanString(entry.month);
      if (!/^\d{4}-\d{2}$/.test(month)) {
        throw new Error(
          `metrics.monthly_app_runs_by_month[${index}] has an invalid month`,
        );
      }
      const perOrgEntries = Array.isArray(entry.per_org) ? entry.per_org : [];
      const perOrg = perOrgEntries.map((orgEntry) => ({
        org_id: cleanString(orgEntry && orgEntry.org_id),
        org_name: cleanString(orgEntry && orgEntry.org_name, "Unnamed organization"),
        monthly_app_runs: asNullableNumber(
          orgEntry && orgEntry.monthly_app_runs,
        ),
      }));
      byMonth.set(month, {
        month,
        monthly_app_runs: asNumber(entry.monthly_app_runs),
        source: cleanString(entry.source, "daily-statistics"),
        per_org: perOrg,
      });
    });
    return [...byMonth.values()].sort((left, right) =>
      left.month.localeCompare(right.month),
    );
  }

  function normalizeReport(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      throw new Error("The report must be a JSON object.");
    }
    if (!raw.metrics || typeof raw.metrics !== "object") {
      throw new Error("The report is missing its metrics object.");
    }
    if (!Array.isArray(raw.metrics.per_org)) {
      throw new Error(
        "The report is missing metrics.per_org. Regenerate it with the current collector.",
      );
    }

    const metrics = raw.metrics;
    const organizations = metrics.per_org.map(normalizeOrganization);
    const status =
      raw.audit_status && typeof raw.audit_status === "object"
        ? raw.audit_status
        : {};

    return {
      schema_version: raw.schema_version,
      generated_at_utc: cleanString(raw.generated_at_utc),
      reporting_month_utc: cleanString(raw.reporting_month_utc),
      audit_status: {
        complete: status.complete === true,
        warnings: Array.isArray(status.warnings)
          ? status.warnings.map((item) => String(item))
          : [],
        errors: Array.isArray(status.errors)
          ? status.errors.map((item) => String(item))
          : [],
      },
      scope: raw.scope && typeof raw.scope === "object" ? raw.scope : {},
      metrics: {
        org_tenant_count: asNumber(metrics.org_tenant_count),
        cpu_core_count: asNumber(metrics.cpu_core_count),
        environment_count: asNumber(metrics.environment_count),
        workflow_count: asNumber(metrics.workflow_count),
        average_actions_per_workflow: asNumber(
          metrics.average_actions_per_workflow,
        ),
        average_nodes_per_workflow: asNumber(metrics.average_nodes_per_workflow),
        monthly_app_runs: asNumber(metrics.monthly_app_runs),
        per_org: organizations,
        monthly_app_runs_by_month: normalizeMonthlySeries(
          metrics.monthly_app_runs_by_month,
          cleanString(raw.reporting_month_utc),
          asNumber(metrics.monthly_app_runs),
          organizations,
        ),
      },
    };
  }

  function formatNumber(value, compact = false) {
    if (value === null || value === undefined) {
      return "Unknown";
    }
    return (compact ? compactFormatter : numberFormatter).format(value);
  }

  function formatDate(value) {
    if (!value) {
      return "Unknown";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return new Intl.DateTimeFormat("en", {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: "UTC",
    }).format(date);
  }

  function formatMonth(value) {
    if (!/^\d{4}-\d{2}$/.test(value || "")) {
      return value || "Unknown";
    }
    const date = new Date(`${value}-01T00:00:00Z`);
    return new Intl.DateTimeFormat("en", {
      month: "long",
      year: "numeric",
      timeZone: "UTC",
    }).format(date);
  }

  function createElement(tagName, className, text) {
    const element = document.createElement(tagName);
    if (className) {
      element.className = className;
    }
    if (text !== undefined) {
      element.textContent = text;
    }
    return element;
  }

  function setText(id, value) {
    document.getElementById(id).textContent = value;
  }

  function renderSummary(report) {
    const cards = [
      ["Organizations", report.metrics.org_tenant_count, "#ff8544"],
      ["CPU cores", report.metrics.cpu_core_count, "#9e9e9e"],
      ["Environments", report.metrics.environment_count, "#5cc879"],
      ["Workflows", report.metrics.workflow_count, "#ff8544"],
      ["Avg actions / workflow", report.metrics.average_actions_per_workflow, "#9e9e9e"],
      ["Avg nodes / workflow", report.metrics.average_nodes_per_workflow, "#9e9e9e"],
      ["Current-month app runs", report.metrics.monthly_app_runs, "#ff8544"],
    ];
    const grid = document.getElementById("summary-grid");
    grid.replaceChildren();

    cards.forEach(([label, value, accent]) => {
      const card = createElement("article", "summary-card");
      const accentBar = createElement("span", "summary-accent");
      accentBar.style.setProperty("--accent", accent);
      const numeric = createElement(
        "strong",
        "summary-value",
        formatNumber(value, value >= 10000),
      );
      numeric.title = formatNumber(value);
      card.append(
        accentBar,
        numeric,
        createElement("span", "summary-label", label),
      );
      grid.append(card);
    });
  }

  function selectedMonthEntry() {
    return (
      state.monthlySeries.find((entry) => entry.month === state.selectedMonth) ||
      state.monthlySeries[state.monthlySeries.length - 1] ||
      null
    );
  }

  function monthlyValueForOrg(org) {
    const entry = selectedMonthEntry();
    if (!entry) {
      return org.monthly_app_runs;
    }
    const orgEntry = entry.per_org.find((item) => item.org_id === org.org_id);
    return orgEntry ? orgEntry.monthly_app_runs : 0;
  }

  function metricValueForOrg(org, metric) {
    return metric === "monthly_app_runs" ? monthlyValueForOrg(org) : org[metric];
  }

  function svgElement(tagName, attributes = {}, text) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tagName);
    Object.entries(attributes).forEach(([name, value]) => {
      element.setAttribute(name, String(value));
    });
    if (text !== undefined) {
      element.textContent = text;
    }
    return element;
  }

  function renderTimeline() {
    const container = document.getElementById("timeline-chart");
    container.replaceChildren();
    if (!state.monthlySeries.length) {
      container.append(createElement("p", "table-empty", "No monthly history available."));
      return;
    }

    const width = 1080;
    const height = 275;
    const padding = { top: 20, right: 28, bottom: 44, left: 62 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const maximum = Math.max(
      1,
      ...state.monthlySeries.map((entry) => entry.monthly_app_runs),
    );
    const xForIndex = (index) =>
      state.monthlySeries.length === 1
        ? padding.left + plotWidth / 2
        : padding.left + (index / (state.monthlySeries.length - 1)) * plotWidth;
    const yForValue = (value) =>
      padding.top + plotHeight - (value / maximum) * plotHeight;

    const svg = svgElement("svg", {
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": "Monthly app runs over all available months",
    });
    const title = svgElement(
      "title",
      {},
      `Monthly app runs from ${formatMonth(state.monthlySeries[0].month)} to ${formatMonth(
        state.monthlySeries[state.monthlySeries.length - 1].month,
      )}`,
    );
    const definitions = svgElement("defs");
    const gradient = svgElement("linearGradient", {
      id: "timeline-gradient",
      x1: "0",
      y1: "0",
      x2: "0",
      y2: "1",
    });
    gradient.append(
      svgElement("stop", {
        offset: "0%",
        "stop-color": "#ff8544",
        "stop-opacity": "0.28",
      }),
      svgElement("stop", {
        offset: "100%",
        "stop-color": "#ff8544",
        "stop-opacity": "0",
      }),
    );
    definitions.append(gradient);
    svg.append(title, definitions);

    for (let step = 0; step <= 4; step += 1) {
      const value = (maximum / 4) * step;
      const y = yForValue(value);
      svg.append(
        svgElement("line", {
          x1: padding.left,
          y1: y,
          x2: width - padding.right,
          y2: y,
          class: "timeline-grid-line",
        }),
        svgElement(
          "text",
          {
            x: padding.left - 12,
            y: y + 4,
            "text-anchor": "end",
            class: "timeline-axis-label",
          },
          formatNumber(value, maximum >= 10000),
        ),
      );
    }

    const points = state.monthlySeries.map((entry, index) => ({
      x: xForIndex(index),
      y: yForValue(entry.monthly_app_runs),
      entry,
    }));
    const linePoints = points.map((point) => `${point.x},${point.y}`).join(" ");
    const areaPoints = [
      `${points[0].x},${padding.top + plotHeight}`,
      linePoints,
      `${points[points.length - 1].x},${padding.top + plotHeight}`,
    ].join(" ");
    svg.append(
      svgElement("polygon", {
        points: areaPoints,
        class: "timeline-area",
      }),
      svgElement("polyline", {
        points: linePoints,
        class: "timeline-line",
      }),
    );

    const labelInterval = Math.max(1, Math.ceil(points.length / 8));
    points.forEach((point, index) => {
      const circle = svgElement("circle", {
        cx: point.x,
        cy: point.y,
        r: 5,
        class: "timeline-point",
        tabindex: "0",
        "aria-label": `${formatMonth(point.entry.month)}: ${formatNumber(
          point.entry.monthly_app_runs,
        )} app runs`,
      });
      circle.append(
        svgElement(
          "title",
          {},
          `${formatMonth(point.entry.month)}: ${formatNumber(
            point.entry.monthly_app_runs,
          )} app runs`,
        ),
      );
      svg.append(circle);
      if (index % labelInterval === 0 || index === points.length - 1) {
        svg.append(
          svgElement(
            "text",
            {
              x: point.x,
              y: height - 12,
              "text-anchor": "middle",
              class: "timeline-axis-label",
            },
            point.entry.month,
          ),
        );
      }
    });

    container.append(svg);
    const first = state.monthlySeries[0].month;
    const last = state.monthlySeries[state.monthlySeries.length - 1].month;
    setText(
      "history-range",
      `${formatMonth(first)} — ${formatMonth(last)} · ${state.monthlySeries.length} month${
        state.monthlySeries.length === 1 ? "" : "s"
      } available`,
    );
  }

  function updateSelectedMonthViews() {
    const formattedMonth = formatMonth(state.selectedMonth);
    document.getElementById("comparison-heading").textContent =
      state.activeMetric === "monthly_app_runs"
        ? `App runs · ${formattedMonth}`
        : metricConfig[state.activeMetric].label;
    document.getElementById("monthly-runs-header").textContent =
      `Runs · ${formattedMonth}`;
    renderComparison();
    renderInsights();
    renderTable();
  }

  function renderComparison() {
    const config = metricConfig[state.activeMetric];
    const chart = document.getElementById("comparison-chart");
    const organizations = [...state.organizations].sort(
      (left, right) =>
        asNumber(metricValueForOrg(right, state.activeMetric), -1) -
          asNumber(metricValueForOrg(left, state.activeMetric), -1) ||
        left.org_name.localeCompare(right.org_name),
    );
    const maximum = Math.max(
      1,
      ...organizations.map((org) =>
        asNumber(metricValueForOrg(org, state.activeMetric)),
      ),
    );

    chart.replaceChildren();
    if (!organizations.length) {
      chart.append(createElement("p", "table-empty", "No organization data available."));
      return;
    }

    organizations.forEach((org) => {
      const value = metricValueForOrg(org, state.activeMetric);
      const row = createElement("div", "chart-row");
      const label = createElement("span", "chart-label", org.org_name);
      label.title = `${org.org_name} — ${org.org_id}`;
      const track = createElement("div", "bar-track");
      const bar = createElement("div", "bar-fill");
      const percentage = value === null ? 0 : (value / maximum) * 100;
      bar.style.width = `${Math.max(0, percentage)}%`;
      bar.setAttribute(
        "aria-label",
        `${org.org_name}: ${formatNumber(value)} ${config.label}`,
      );
      bar.title = `${org.org_name}: ${formatNumber(value)} ${config.label}`;
      track.append(bar);
      const valueElement = createElement(
        "strong",
        "chart-value",
        formatNumber(value, config.compact),
      );
      valueElement.title = formatNumber(value);
      row.append(label, track, valueElement);
      chart.append(row);
    });
  }

  function renderComposition() {
    const totalActions = state.organizations.reduce(
      (total, org) => total + asNumber(org.action_count),
      0,
    );
    const totalNodes = state.organizations.reduce(
      (total, org) => total + asNumber(org.node_count),
      0,
    );
    const totalTriggers = Math.max(0, totalNodes - totalActions);
    const actionShare = totalNodes ? (totalActions / totalNodes) * 100 : 0;
    const donut = document.getElementById("composition-donut");
    donut.style.background = totalNodes
      ? `conic-gradient(var(--accent) 0 ${actionShare}%, var(--green) ${actionShare}% 100%)`
      : "conic-gradient(#363636 0 100%)";
    donut.setAttribute(
      "aria-label",
      `${formatNumber(totalActions)} actions and ${formatNumber(totalTriggers)} triggers`,
    );

    setText("total-nodes", formatNumber(totalNodes, totalNodes >= 10000));
    setText("total-actions", formatNumber(totalActions));
    setText("total-triggers", formatNumber(totalTriggers));
  }

  function bestOrganization(key) {
    return [...state.organizations]
      .filter((org) => metricValueForOrg(org, key) !== null)
      .sort(
        (left, right) =>
          metricValueForOrg(right, key) - metricValueForOrg(left, key) ||
          left.org_name.localeCompare(right.org_name),
      )[0];
  }

  function renderInsights() {
    const busiest = bestOrganization("monthly_app_runs");
    const workflowLeader = bestOrganization("workflow_count");
    const nodeLeader = bestOrganization("node_count");
    const runsPerWorkflow = [...state.organizations]
      .filter(
        (org) =>
          monthlyValueForOrg(org) !== null &&
          org.workflow_count !== null &&
          org.workflow_count > 0,
      )
      .map((org) => ({
        ...org,
        selected_month_app_runs: monthlyValueForOrg(org),
        ratio: monthlyValueForOrg(org) / org.workflow_count,
      }))
      .sort(
        (left, right) =>
          right.ratio - left.ratio || left.org_name.localeCompare(right.org_name),
      )[0];

    const insights = [
      {
        label: `Highest usage · ${formatMonth(state.selectedMonth)}`,
        org: busiest,
        value: busiest ? formatNumber(monthlyValueForOrg(busiest), true) : "—",
      },
      {
        label: "Largest workflow estate",
        org: workflowLeader,
        value: workflowLeader ? formatNumber(workflowLeader.workflow_count) : "—",
      },
      {
        label: "Most workflow nodes",
        org: nodeLeader,
        value: nodeLeader ? formatNumber(nodeLeader.node_count) : "—",
      },
      {
        label: `Runs per workflow · ${formatMonth(state.selectedMonth)}`,
        org: runsPerWorkflow,
        value: runsPerWorkflow ? formatNumber(runsPerWorkflow.ratio) : "—",
      },
    ];

    const list = document.getElementById("insight-list");
    list.replaceChildren();
    insights.forEach((insight, index) => {
      const item = createElement("div", "insight-item");
      const number = createElement(
        "span",
        "insight-index",
        String(index + 1).padStart(2, "0"),
      );
      const copy = createElement("div", "insight-copy");
      copy.append(
        createElement("strong", "", insight.org ? insight.org.org_name : "No data"),
        createElement("span", "", insight.label),
      );
      item.append(
        number,
        copy,
        createElement("strong", "insight-value", insight.value),
      );
      list.append(item);
    });
  }

  function compareOrganizations(left, right) {
    const key = state.sortKey;
    let comparison;
    if (key === "org_name") {
      comparison = left.org_name.localeCompare(right.org_name);
    } else {
      comparison =
        asNumber(metricValueForOrg(left, key), -1) -
        asNumber(metricValueForOrg(right, key), -1);
    }
    return state.sortDirection === "asc" ? comparison : -comparison;
  }

  function numericCell(value) {
    const cell = createElement("td", "numeric-cell", formatNumber(value));
    if (value !== null) {
      cell.title = formatNumber(value);
    }
    return cell;
  }

  function renderTable() {
    const query = state.search.toLocaleLowerCase();
    const organizations = state.organizations
      .filter(
        (org) =>
          !query ||
          org.org_name.toLocaleLowerCase().includes(query) ||
          org.org_id.toLocaleLowerCase().includes(query) ||
          (org.parent_org_name || "").toLocaleLowerCase().includes(query),
      )
      .sort(compareOrganizations);

    const body = document.getElementById("org-table-body");
    body.replaceChildren();
    organizations.forEach((org) => {
      const row = document.createElement("tr");
      const orgCell = createElement("td", "org-cell");
      orgCell.append(
        createElement("strong", "", org.org_name),
        createElement("span", "", org.org_id),
      );
      const parentCell = createElement(
        "td",
        "parent-cell",
        org.parent_org_name || "Root organization",
      );
      if (org.parent_org_id) {
        parentCell.title = `${org.parent_org_name || "Parent"} — ${org.parent_org_id}`;
      }
      row.append(
        orgCell,
        parentCell,
        numericCell(org.environment_count),
        numericCell(org.workflow_count),
        numericCell(org.action_count),
        numericCell(org.trigger_count),
        numericCell(org.average_nodes_per_workflow),
        numericCell(monthlyValueForOrg(org)),
      );
      body.append(row);
    });
    document.getElementById("table-empty").hidden = organizations.length !== 0;

    document.querySelectorAll(".sort-button").forEach((button) => {
      const active = button.dataset.sort === state.sortKey;
      button.classList.toggle("is-sorted", active);
      button.classList.toggle(
        "is-ascending",
        active && state.sortDirection === "asc",
      );
      button.setAttribute(
        "aria-sort",
        active
          ? state.sortDirection === "asc"
            ? "ascending"
            : "descending"
          : "none",
      );
    });
  }

  function renderNotes(report) {
    const panel = document.getElementById("notes-panel");
    const list = document.getElementById("notes-list");
    const notes = [
      ...report.audit_status.errors.map((text) => ({ text, error: true })),
      ...report.audit_status.warnings.map((text) => ({ text, error: false })),
    ];
    panel.hidden = notes.length === 0;
    list.replaceChildren();
    notes.forEach((note) => {
      list.append(
        createElement(
          "div",
          `note-item${note.error ? " is-error" : ""}`,
          note.text,
        ),
      );
    });
  }

  function renderReport(report) {
    state.report = report;
    state.organizations = report.metrics.per_org;
    state.monthlySeries = report.metrics.monthly_app_runs_by_month;
    state.selectedMonth =
      state.monthlySeries.find(
        (entry) => entry.month === report.reporting_month_utc,
      )?.month ||
      state.monthlySeries[state.monthlySeries.length - 1]?.month ||
      report.reporting_month_utc;
    state.search = "";
    document.getElementById("org-search").value = "";

    const monthSelect = document.getElementById("month-select");
    monthSelect.replaceChildren();
    state.monthlySeries.forEach((entry) => {
      const option = createElement("option", "", formatMonth(entry.month));
      option.value = entry.month;
      option.selected = entry.month === state.selectedMonth;
      monthSelect.append(option);
    });

    setText(
      "hero-subtitle",
      `${formatNumber(report.metrics.org_tenant_count)} organizations · ${formatNumber(
        report.metrics.workflow_count,
      )} workflows · ${formatNumber(
        report.metrics.monthly_app_runs,
        true,
      )} current-month app runs`,
    );
    setText("reporting-month", formatMonth(report.reporting_month_utc));
    setText("generated-at", formatDate(report.generated_at_utc));
    setText(
      "cpu-scope",
      cleanString(report.scope.cpu_scope, "Not specified").replaceAll("-", " "),
    );

    const status = document.getElementById("audit-status");
    status.textContent = report.audit_status.complete ? "Complete" : "Incomplete";
    status.className = `status-pill ${
      report.audit_status.complete ? "is-complete" : "is-incomplete"
    }`;
    const banner = document.getElementById("status-banner");
    banner.hidden = report.audit_status.complete;
    banner.textContent = report.audit_status.complete
      ? ""
      : "This report is marked incomplete. Treat totals as partial and review the collection errors below.";

    renderSummary(report);
    renderTimeline();
    updateSelectedMonthViews();
    renderComposition();
    renderNotes(report);

    document.getElementById("loading-view").hidden = true;
    document.getElementById("upload-view").hidden = true;
    document.getElementById("dashboard").hidden = false;
  }

  function showUpload(errorMessage = "") {
    document.getElementById("loading-view").hidden = true;
    document.getElementById("dashboard").hidden = true;
    document.getElementById("upload-view").hidden = false;
    setText("upload-error", errorMessage);
  }

  function parseAndRender(raw) {
    const report = normalizeReport(raw);
    renderReport(report);
    return report;
  }

  async function loadReportFile(file) {
    if (!file) {
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      showUpload("The selected report is larger than the 50 MiB safety limit.");
      return;
    }
    try {
      const raw = JSON.parse(await file.text());
      parseAndRender(raw);
    } catch (error) {
      showUpload(`Could not load report: ${error.message}`);
    }
  }

  async function loadDefaultReport() {
    try {
      const response = await fetch("report.json", {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      parseAndRender(await response.json());
    } catch (_error) {
      showUpload("Choose a report to begin.");
    }
  }

  function initializeBrowser() {
    const fileInput = document.getElementById("report-file");
    document
      .getElementById("choose-report-button")
      .addEventListener("click", () => fileInput.click());
    document
      .getElementById("load-another-button")
      .addEventListener("click", () => fileInput.click());
    document
      .getElementById("print-button")
      .addEventListener("click", () => globalScope.print());
    fileInput.addEventListener("change", () => {
      loadReportFile(fileInput.files[0]);
      fileInput.value = "";
    });

    document.querySelectorAll(".metric-tab").forEach((button) => {
      button.addEventListener("click", () => {
        state.activeMetric = button.dataset.metric;
        document.querySelectorAll(".metric-tab").forEach((tab) => {
          tab.classList.toggle("is-active", tab === button);
        });
        document.getElementById("month-control").hidden =
          state.activeMetric !== "monthly_app_runs";
        updateSelectedMonthViews();
      });
    });

    document.getElementById("month-select").addEventListener("change", (event) => {
      state.selectedMonth = event.target.value;
      updateSelectedMonthViews();
    });

    document.querySelectorAll(".sort-button").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.dataset.sort;
        if (state.sortKey === key) {
          state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
        } else {
          state.sortKey = key;
          state.sortDirection = key === "org_name" ? "asc" : "desc";
        }
        renderTable();
      });
    });

    document.getElementById("org-search").addEventListener("input", (event) => {
      state.search = event.target.value.trim();
      renderTable();
    });

    loadDefaultReport();
  }

  const publicApi = {
    asNumber,
    asNullableNumber,
    normalizeOrganization,
    normalizeMonthlySeries,
    normalizeReport,
    formatNumber,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = publicApi;
  }
  globalScope.ShuffleAuditDashboard = publicApi;

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initializeBrowser);
    } else {
      initializeBrowser();
    }
  }
})(typeof window !== "undefined" ? window : globalThis);
