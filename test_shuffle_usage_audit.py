#!/usr/bin/env python3

import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import shuffle_usage_audit as audit


ROOT_ID = "11111111-1111-1111-1111-111111111111"
CHILD_ID = "22222222-2222-2222-2222-222222222222"
OTHER_ID = "33333333-3333-3333-3333-333333333333"


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def get_json(self, path, org_id=None):
        self.requests.append((path, org_id))
        key = (path, org_id)
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return response


class FakeOpenSearch:
    def __init__(self):
        self.page_size = 250
        self.request_count = 0
        self.retry_count = 0
        self.calls = []

    def iter_documents(self, base_index, query, source_fields):
        self.calls.append(("search", base_index, query, tuple(source_fields)))
        self.request_count += 1
        org_id = query["match"]["org_id"]
        if base_index == "workflow":
            count = 650 if org_id == ROOT_ID else 2
            for index in range(count):
                yield {
                    "id": f"{org_id}-workflow-{index}",
                    "org_id": org_id,
                    "name": "private workflow",
                    "actions": [{}, {}],
                    "triggers": [{}],
                }
        elif base_index == "environments":
            count = 620 if org_id == ROOT_ID else 2
            for index in range(count):
                yield {
                    "id": f"{org_id}-environment-{index}",
                    "org_id": org_id,
                    "archived": False,
                }

    def get_document(self, base_index, document_id):
        self.calls.append(("get", base_index, document_id))
        self.request_count += 1
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        return {
            "daily_statistics": [
                {
                    "date": (start + timedelta(days=index)).isoformat(),
                    "app_executions": 1,
                }
                for index in range(800)
            ]
        }


class ScriptedOpenSearch(audit.OpenSearchClient):
    def __init__(self, responses):
        super().__init__(
            "http://localhost:9200",
            page_size=2,
            request_delay_seconds=0,
        )
        self.responses = list(responses)
        self.calls = []

    def request_json(self, url, headers, method="GET", payload=None):
        self.calls.append((url, method, payload))
        if method == "DELETE":
            return {"succeeded": True}
        return self.responses.pop(0)


class FakeHttpResponse:
    def __init__(self, body):
        self.body = body
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        return False

    def read(self, _limit=-1):
        return self.body


def fake_responses():
    workflow_path = (
        f"/api/v1/workflows?top={audit.WORKFLOW_RESPONSE_LIMIT}&truncate=false"
    )
    return {
        ("/api/v1/orgs", None): [
            {
                "id": ROOT_ID,
                "name": "Secret Parent",
                "child_orgs": [
                    {
                        "id": CHILD_ID,
                        "name": "Secret Child",
                        "creator_org": ROOT_ID,
                    }
                ],
            },
            {"id": CHILD_ID, "name": "Secret Child", "creator_org": ROOT_ID},
            {"id": OTHER_ID, "name": "Unrelated"},
        ],
        (workflow_path, ROOT_ID): [
            {
                "id": "root-workflow",
                "org_id": ROOT_ID,
                "name": "Do not disclose",
                "actions": [{}, {}],
                "triggers": [{}],
            },
            {
                "id": "distributed-child-workflow",
                "org_id": CHILD_ID,
                "actions": [{}],
                "triggers": [],
            },
        ],
        (workflow_path, CHILD_ID): [
            {
                "id": "child-workflow",
                "org_id": CHILD_ID,
                "name": "Also private",
                "actions": [{}],
                "triggers": [{}, {}],
            }
        ],
        ("/api/v1/environments", ROOT_ID): [
            {"id": "env-root", "org_id": ROOT_ID, "archived": False},
            {"id": "env-old", "org_id": ROOT_ID, "archived": True},
        ],
        ("/api/v1/environments", CHILD_ID): [
            {"id": "env-child", "org_id": CHILD_ID, "archived": False},
            {"id": "foreign-env", "org_id": OTHER_ID, "archived": False},
        ],
        (f"/api/v1/orgs/{ROOT_ID}/stats", ROOT_ID): {
            "org_id": ROOT_ID,
            "org_name": "Secret Parent",
            "monthly_app_executions": 10,
            "monthly_child_app_executions": 7,
            "last_monthly_reset_month": 7,
            "daily_statistics": [
                {"date": "2026-01-02T00:00:00Z", "app_executions": 3},
                {"date": "2026-01-03T00:00:00Z", "app_executions": 4},
                {"date": "2026-06-15T00:00:00Z", "app_executions": 2},
                {"date": "2026-07-28T00:00:00Z", "app_executions": 6},
            ],
        },
        (f"/api/v1/orgs/{CHILD_ID}/stats", CHILD_ID): {
            "org_id": CHILD_ID,
            "org_name": "Secret Child",
            "monthly_app_executions": 7,
            "last_monthly_reset_month": 7,
            "daily_statistics": [
                {"date": "2026-01-12T00:00:00Z", "app_executions": 5},
                {"date": "2026-07-28T00:00:00Z", "app_executions": 4},
            ],
        },
    }


class OrgSelectionTests(unittest.TestCase):
    def test_selects_root_and_descendants_breadth_first(self):
        selected, parents = audit.select_org_ids(
            fake_responses()[("/api/v1/orgs", None)],
            ROOT_ID,
            False,
        )

        self.assertEqual(selected, [ROOT_ID, CHILD_ID])
        self.assertEqual(parents, {ROOT_ID: None, CHILD_ID: ROOT_ID})

    def test_rejects_unknown_root(self):
        with self.assertRaisesRegex(audit.AuditError, "not visible"):
            audit.select_org_ids(
                fake_responses()[("/api/v1/orgs", None)],
                "missing",
                False,
            )


class CollectionTests(unittest.TestCase):
    def test_aggregates_without_child_counter_double_counting(self):
        client = FakeClient(fake_responses())
        report = audit.collect_audit(
            client,
            root_org_id=ROOT_ID,
            all_visible_orgs=False,
            cpu_capacity={"cores": 8, "scope": "operator-supplied", "node_count": None},
            allow_partial=False,
            collected_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        )

        metrics = report["metrics"]
        self.assertEqual(report["reporting_month_utc"], "2026-07")
        self.assertEqual(metrics["org_tenant_count"], 2)
        self.assertEqual(metrics["cpu_core_count"], 8)
        self.assertEqual(metrics["environment_count"], 2)
        self.assertEqual(metrics["workflow_count"], 2)
        self.assertEqual(
            metrics["workflows_per_org"],
            [
                {
                    "org_id": ROOT_ID,
                    "org_name": "Secret Parent",
                    "workflow_count": 1,
                },
                {
                    "org_id": CHILD_ID,
                    "org_name": "Secret Child",
                    "workflow_count": 1,
                },
            ],
        )
        self.assertEqual(metrics["average_actions_per_workflow"], 1.5)
        self.assertEqual(metrics["average_nodes_per_workflow"], 3.0)
        self.assertEqual(metrics["monthly_app_runs"], 17)
        self.assertEqual(
            metrics["monthly_app_runs_per_org"],
            [
                {
                    "org_id": ROOT_ID,
                    "org_name": "Secret Parent",
                    "monthly_app_runs": 10,
                },
                {
                    "org_id": CHILD_ID,
                    "org_name": "Secret Child",
                    "monthly_app_runs": 7,
                },
            ],
        )
        self.assertEqual(
            metrics["monthly_app_runs_by_month"],
            [
                {
                    "month": "2026-01",
                    "monthly_app_runs": 12,
                    "source": "daily-statistics",
                    "per_org": [
                        {
                            "org_id": ROOT_ID,
                            "org_name": "Secret Parent",
                            "monthly_app_runs": 7,
                        },
                        {
                            "org_id": CHILD_ID,
                            "org_name": "Secret Child",
                            "monthly_app_runs": 5,
                        },
                    ],
                },
                {
                    "month": "2026-06",
                    "monthly_app_runs": 2,
                    "source": "daily-statistics",
                    "per_org": [
                        {
                            "org_id": ROOT_ID,
                            "org_name": "Secret Parent",
                            "monthly_app_runs": 2,
                        },
                        {
                            "org_id": CHILD_ID,
                            "org_name": "Secret Child",
                            "monthly_app_runs": 0,
                        },
                    ],
                },
                {
                    "month": "2026-07",
                    "monthly_app_runs": 17,
                    "source": "current-month-counter",
                    "per_org": [
                        {
                            "org_id": ROOT_ID,
                            "org_name": "Secret Parent",
                            "monthly_app_runs": 10,
                        },
                        {
                            "org_id": CHILD_ID,
                            "org_name": "Secret Child",
                            "monthly_app_runs": 7,
                        },
                    ],
                },
            ],
        )

        serialized = json.dumps(report)
        for expected_org_value in (
            ROOT_ID,
            CHILD_ID,
            "Secret Parent",
            "Secret Child",
        ):
            self.assertIn(expected_org_value, serialized)
        for private_object_value in (
            OTHER_ID,
            "root-workflow",
            "child-workflow",
            "Do not disclose",
        ):
            self.assertNotIn(private_object_value, serialized)

    def test_only_expected_get_surfaces_are_requested(self):
        client = FakeClient(fake_responses())
        audit.collect_audit(
            client,
            root_org_id=ROOT_ID,
            all_visible_orgs=False,
            cpu_capacity={"cores": 4, "scope": "operator-supplied", "node_count": None},
            allow_partial=False,
            collected_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        )

        self.assertEqual(client.requests[0], ("/api/v1/orgs", None))
        self.assertEqual(len(client.requests), 7)
        self.assertTrue(
            all(
                path == "/api/v1/orgs"
                or path == "/api/v1/environments"
                or path.endswith("/stats")
                or path.startswith("/api/v1/workflows?")
                for path, _ in client.requests
            )
        )

    def test_fails_on_workflow_response_ceiling(self):
        responses = fake_responses()
        workflow_path = (
            f"/api/v1/workflows?top={audit.WORKFLOW_RESPONSE_LIMIT}&truncate=false"
        )
        responses[(workflow_path, ROOT_ID)] = [
            {
                "id": f"workflow-{index}",
                "org_id": ROOT_ID,
                "actions": [],
                "triggers": [],
            }
            for index in range(audit.WORKFLOW_RESPONSE_LIMIT)
        ]

        with self.assertRaisesRegex(audit.AuditError, "backend ceiling"):
            audit.collect_audit(
                FakeClient(responses),
                root_org_id=ROOT_ID,
                all_visible_orgs=False,
                cpu_capacity={"cores": 4, "scope": "operator-supplied", "node_count": None},
                allow_partial=False,
                collected_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            )

    def test_fails_on_backend_history_ceiling(self):
        responses = fake_responses()
        responses[(f"/api/v1/orgs/{ROOT_ID}/stats", ROOT_ID)][
            "daily_statistics"
        ] = [
            {
                "date": (
                    datetime(2025, 7, 30, tzinfo=timezone.utc)
                    + timedelta(days=index)
                ).isoformat(),
                "app_executions": 1,
            }
            for index in range(365)
        ]

        with self.assertRaisesRegex(audit.AuditError, "maximum 365"):
            audit.collect_audit(
                FakeClient(responses),
                root_org_id=ROOT_ID,
                all_visible_orgs=False,
                cpu_capacity={"cores": 4, "scope": "operator-supplied", "node_count": None},
                allow_partial=False,
                collected_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            )

    def test_opensearch_mode_handles_uncapped_workflows_and_full_history(self):
        client = FakeClient(fake_responses())
        opensearch = FakeOpenSearch()
        report = audit.collect_audit(
            client,
            root_org_id=ROOT_ID,
            all_visible_orgs=False,
            cpu_capacity={"cores": 8, "scope": "operator-supplied", "node_count": None},
            allow_partial=False,
            collected_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            opensearch=opensearch,
        )

        self.assertTrue(report["audit_status"]["complete"])
        self.assertEqual(
            report["scope"]["collection_mode"],
            "shuffle-api-plus-opensearch",
        )
        self.assertEqual(report["metrics"]["workflow_count"], 652)
        self.assertEqual(report["metrics"]["environment_count"], 622)
        self.assertEqual(report["metrics"]["monthly_app_runs"], 17)
        history = report["metrics"]["monthly_app_runs_by_month"]
        self.assertEqual(history[0]["month"], "2024-01")
        self.assertGreater(len(history), 24)
        self.assertEqual(history[-1]["month"], "2026-07")
        self.assertEqual(
            client.requests,
            [
                ("/api/v1/orgs", None),
                (f"/api/v1/orgs/{ROOT_ID}/stats", ROOT_ID),
                (f"/api/v1/orgs/{CHILD_ID}/stats", CHILD_ID),
            ],
        )

    def test_partial_mode_marks_unknown_tenant_values(self):
        responses = fake_responses()
        responses[("/api/v1/environments", CHILD_ID)] = audit.AuditError("not allowed")
        report = audit.collect_audit(
            FakeClient(responses),
            root_org_id=ROOT_ID,
            all_visible_orgs=False,
            cpu_capacity={"cores": 4, "scope": "operator-supplied", "node_count": None},
            allow_partial=True,
            collected_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        )

        self.assertFalse(report["audit_status"]["complete"])
        self.assertIsNone(report["metrics"]["workflows_per_org"][1]["workflow_count"])
        self.assertIsNone(
            report["metrics"]["monthly_app_runs_per_org"][1]["monthly_app_runs"]
        )


class UtilityTests(unittest.TestCase):
    def test_opensearch_scroll_validates_and_yields_every_document(self):
        client = ScriptedOpenSearch(
            [
                {
                    "_scroll_id": "scroll-1",
                    "hits": {
                        "total": {"value": 3, "relation": "eq"},
                        "hits": [
                            {"_id": "one", "_source": {"id": "one"}},
                            {"_id": "two", "_source": {"id": "two"}},
                        ],
                    },
                },
                {
                    "_scroll_id": "scroll-2",
                    "hits": {
                        "total": {"value": 3, "relation": "eq"},
                        "hits": [{"_id": "three", "_source": {"id": "three"}}],
                    },
                },
                {
                    "_scroll_id": "scroll-3",
                    "hits": {
                        "total": {"value": 3, "relation": "eq"},
                        "hits": [],
                    },
                },
            ]
        )

        documents = list(
            client.iter_documents(
                "workflow",
                {"match": {"org_id": ROOT_ID}},
                ["id"],
            )
        )

        self.assertEqual(
            documents,
            [{"id": "one"}, {"id": "two"}, {"id": "three"}],
        )
        self.assertEqual(client.calls[0][1], "POST")
        self.assertEqual(client.calls[0][2]["size"], 2)
        self.assertEqual(client.calls[-1][1], "DELETE")

    def test_transport_retries_transient_http_failures(self):
        failure = urllib.error.HTTPError(
            "http://localhost:5001/api/v1/orgs",
            503,
            "unavailable",
            {"Retry-After": "0"},
            io.BytesIO(b"busy"),
        )
        client = audit.ShuffleApiClient(
            "test-api-key",
            retries=1,
            request_delay_seconds=0,
        )
        with mock.patch.object(
            audit.urllib.request,
            "urlopen",
            side_effect=[failure, FakeHttpResponse(b'{"ok":true}')],
        ), mock.patch.object(audit.time, "sleep"):
            response = client.get_json("/api/v1/orgs")

        self.assertEqual(response, {"ok": True})
        self.assertEqual(client.request_count, 2)
        self.assertEqual(client.retry_count, 1)

    def test_opensearch_scroll_rejects_incomplete_results(self):
        client = ScriptedOpenSearch(
            [
                {
                    "_scroll_id": "scroll-1",
                    "hits": {
                        "total": {"value": 2, "relation": "eq"},
                        "hits": [{"_id": "one", "_source": {"id": "one"}}],
                    },
                },
                {
                    "_scroll_id": "scroll-2",
                    "hits": {
                        "total": {"value": 2, "relation": "eq"},
                        "hits": [],
                    },
                },
            ]
        )

        with self.assertRaisesRegex(audit.AuditError, "pagination was incomplete"):
            list(
                client.iter_documents(
                    "workflow",
                    {"match": {"org_id": ROOT_ID}},
                    ["id"],
                )
            )

    def test_opensearch_comma_separated_endpoints_fail_over(self):
        client = audit.OpenSearchClient(
            "https://node-one:9200, https://node-two:9200/",
            retries=1,
            request_delay_seconds=0,
        )
        with mock.patch.object(
            audit.urllib.request,
            "urlopen",
            side_effect=[
                urllib.error.URLError("node one unavailable"),
                FakeHttpResponse(b'{"found":true,"_source":{"org_id":"org-1"}}'),
            ],
        ) as urlopen, mock.patch.object(audit.time, "sleep"):
            document = client.get_document("org_statistics", "org-1")

        self.assertEqual(document, {"org_id": "org-1"})
        self.assertEqual(
            client.base_urls,
            ("https://node-one:9200", "https://node-two:9200"),
        )
        requested_urls = [
            call.args[0].full_url for call in urlopen.call_args_list
        ]
        self.assertEqual(
            requested_urls,
            [
                "https://node-one:9200/org_statistics/_doc/org-1",
                "https://node-two:9200/org_statistics/_doc/org-1",
            ],
        )
        self.assertEqual(client.retry_count, 1)

    def test_backend_url_defaults_to_local_shuffle(self):
        self.assertEqual(
            audit.DEFAULT_SHUFFLE_BASE_URL,
            "http://localhost:5001",
        )
        self.assertEqual(
            audit.ShuffleApiClient("test-api-key").base_url,
            "http://localhost:5001",
        )

    def test_backend_url_can_be_changed(self):
        client = audit.ShuffleApiClient(
            "test-api-key",
            base_url="https://shuffle.example.com/backend/",
        )
        self.assertEqual(client.base_url, "https://shuffle.example.com/backend")

    def test_backend_url_rejects_non_http_urls(self):
        with self.assertRaisesRegex(audit.AuditError, "http:// or https://"):
            audit.ShuffleApiClient(
                "test-api-key",
                base_url="file:///tmp/report.json",
            )

    def test_kubernetes_cpu_quantity(self):
        self.assertEqual(audit.parse_cpu_quantity("2500m"), 2.5)
        self.assertEqual(audit.parse_cpu_quantity("8"), 8.0)

    def test_markdown_and_private_write(self):
        report = audit.collect_audit(
            FakeClient(fake_responses()),
            root_org_id=ROOT_ID,
            all_visible_orgs=False,
            cpu_capacity={"cores": 4, "scope": "operator-supplied", "node_count": None},
            allow_partial=False,
            collected_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        )
        markdown = audit.markdown_report(report)
        self.assertIn("| Monthly App Runs (Current Month) | 17 |", markdown)
        self.assertIn("| 2026-01 | Secret Parent", markdown)
        self.assertIn(ROOT_ID, markdown)
        self.assertIn("Secret Parent", markdown)
        self.assertNotIn("root-workflow", markdown)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.md"
            audit.write_private_text(output, markdown)
            self.assertEqual(output.read_text(encoding="utf-8"), markdown)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
