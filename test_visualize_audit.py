#!/usr/bin/env python3

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import visualize_audit as viewer


def sample_report():
    return {
        "schema_version": 1,
        "generated_at_utc": "2026-07-29T16:28:36Z",
        "reporting_month_utc": "2026-07",
        "audit_status": {"complete": True, "errors": [], "warnings": []},
        "metrics": {
            "org_tenant_count": 1,
            "cpu_core_count": 8,
            "environment_count": 2,
            "workflow_count": 3,
            "average_actions_per_workflow": 2.0,
            "average_nodes_per_workflow": 3.0,
            "monthly_app_runs": 40,
            "monthly_app_runs_by_month": [
                {
                    "month": "2026-06",
                    "monthly_app_runs": 20,
                    "source": "daily-statistics",
                    "per_org": [
                        {
                            "org_id": "org-1",
                            "org_name": "Example Org",
                            "monthly_app_runs": 20,
                        }
                    ],
                },
                {
                    "month": "2026-07",
                    "monthly_app_runs": 40,
                    "source": "current-month-counter",
                    "per_org": [
                        {
                            "org_id": "org-1",
                            "org_name": "Example Org",
                            "monthly_app_runs": 40,
                        }
                    ],
                },
            ],
            "per_org": [
                {
                    "org_id": "org-1",
                    "org_name": "Example Org",
                    "parent_org_id": None,
                    "parent_org_name": None,
                    "environment_count": 2,
                    "workflow_count": 3,
                    "action_count": 6,
                    "node_count": 9,
                    "average_actions_per_workflow": 2.0,
                    "average_nodes_per_workflow": 3.0,
                    "monthly_app_runs": 40,
                }
            ],
        },
    }


class ReportLoadingTests(unittest.TestCase):
    def test_loads_current_report_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(sample_report()), encoding="utf-8")

            report_bytes, report = viewer.load_report(path)

        self.assertEqual(json.loads(report_bytes), sample_report())
        self.assertEqual(report["metrics"]["per_org"][0]["org_name"], "Example Org")

    def test_rejects_report_without_per_org_data(self):
        report = sample_report()
        del report["metrics"]["per_org"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")

            with self.assertRaisesRegex(viewer.ViewerError, "metrics.per_org"):
                viewer.load_report(path)


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.report_bytes = json.dumps(sample_report()).encode("utf-8")
        self.server = viewer.create_server(self.report_bytes, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://{viewer.LOOPBACK_HOST}:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_serves_dashboard_and_report_with_security_headers(self):
        with urllib.request.urlopen(f"{self.base_url}/", timeout=2) as response:
            html = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Shuffle Usage Audit", html)
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")

        with urllib.request.urlopen(
            f"{self.base_url}/report.json",
            timeout=2,
        ) as response:
            self.assertEqual(response.read(), self.report_bytes)
            self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")

    def test_does_not_expose_other_local_files(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(
                f"{self.base_url}/../README.md",
                timeout=2,
            )
        self.assertEqual(context.exception.code, 404)
        context.exception.close()


if __name__ == "__main__":
    unittest.main()
