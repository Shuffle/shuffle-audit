"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const dashboard = require("./app.js");

function reportFixture() {
  return {
    schema_version: 1,
    generated_at_utc: "2026-07-29T16:28:36Z",
    reporting_month_utc: "2026-07",
    audit_status: {
      complete: true,
      errors: [],
      warnings: [],
    },
    scope: {
      cpu_scope: "operator-supplied",
    },
    metrics: {
      org_tenant_count: 1,
      cpu_core_count: 8,
      environment_count: 2,
      workflow_count: 3,
      average_actions_per_workflow: 2,
      average_nodes_per_workflow: 3,
      monthly_app_runs: 40,
      monthly_app_runs_by_month: [
        {
          month: "2026-06",
          monthly_app_runs: 20,
          source: "daily-statistics",
          per_org: [
            {
              org_id: "org-1",
              org_name: "Example <script> Org",
              monthly_app_runs: 20,
            },
          ],
        },
        {
          month: "2026-07",
          monthly_app_runs: 40,
          source: "current-month-counter",
          per_org: [
            {
              org_id: "org-1",
              org_name: "Example <script> Org",
              monthly_app_runs: 40,
            },
          ],
        },
      ],
      per_org: [
        {
          org_id: "org-1",
          org_name: "Example <script> Org",
          parent_org_id: null,
          parent_org_name: null,
          environment_count: 2,
          workflow_count: 3,
          action_count: 6,
          node_count: 9,
          average_actions_per_workflow: 2,
          average_nodes_per_workflow: 3,
          monthly_app_runs: 40,
        },
      ],
    },
  };
}

test("normalizes report and derives trigger count", () => {
  const report = dashboard.normalizeReport(reportFixture());

  assert.equal(report.metrics.per_org[0].org_name, "Example <script> Org");
  assert.equal(report.metrics.per_org[0].trigger_count, 3);
  assert.equal(report.metrics.monthly_app_runs, 40);
  assert.deepEqual(
    report.metrics.monthly_app_runs_by_month.map((entry) => entry.month),
    ["2026-06", "2026-07"],
  );
});

test("rejects reports without per-org metrics", () => {
  const report = reportFixture();
  delete report.metrics.per_org;

  assert.throws(
    () => dashboard.normalizeReport(report),
    /metrics\.per_org/,
  );
});

test("invalid and negative numbers cannot enter totals", () => {
  assert.equal(dashboard.asNumber("not-a-number"), 0);
  assert.equal(dashboard.asNumber(-5), 0);
  assert.equal(dashboard.asNullableNumber(null), null);
  assert.equal(dashboard.asNullableNumber("4"), 4);
});
