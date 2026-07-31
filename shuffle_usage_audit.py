#!/usr/bin/env python3
"""Collect a Shuffle usage audit through read-only API requests."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
WORKFLOW_RESPONSE_LIMIT = 600
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_RETRIES = 3
DEFAULT_REQUEST_DELAY_SECONDS = 0.2
DEFAULT_OPENSEARCH_PAGE_SIZE = 250
MAX_RETRY_DELAY_SECONDS = 60.0
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
DEFAULT_SHUFFLE_BASE_URL = "http://localhost:5001"
RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}


class AuditError(RuntimeError):
    """Raised when an audit cannot produce a complete, trustworthy report."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise AuditError(f"{field_name} must be a non-negative integer")

    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AuditError(f"{field_name} must be a non-negative integer") from exc

    if parsed < 0:
        raise AuditError(f"{field_name} must be a non-negative integer")
    return parsed


def require_list(value: Any, endpoint: str) -> List[Any]:
    if not isinstance(value, list):
        raise AuditError(f"{endpoint} returned {type(value).__name__}; expected a JSON list")
    return value


class ResilientJsonClient:
    """Bounded, sequential JSON transport with pacing and transient retries."""

    def __init__(
        self,
        timeout_seconds: float,
        retries: int,
        request_delay_seconds: float,
        insecure: bool = False,
    ) -> None:
        if timeout_seconds <= 0:
            raise AuditError("--timeout must be greater than zero")
        if retries < 0 or retries > 10:
            raise AuditError("--retries must be between 0 and 10")
        if request_delay_seconds < 0 or request_delay_seconds > 60:
            raise AuditError("--request-delay must be between 0 and 60 seconds")

        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.request_delay_seconds = request_delay_seconds
        self.ssl_context = (
            ssl._create_unverified_context() if insecure else ssl.create_default_context()
        )
        self.request_count = 0
        self.retry_count = 0
        self._last_request_started: Optional[float] = None

    def _pace(self) -> None:
        now = time.monotonic()
        if self._last_request_started is not None:
            remaining = self.request_delay_seconds - (
                now - self._last_request_started
            )
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_started = time.monotonic()

    @staticmethod
    def _retry_delay(attempt: int, headers: Any = None) -> float:
        retry_after = headers.get("Retry-After") if headers is not None else None
        if retry_after:
            try:
                return min(MAX_RETRY_DELAY_SECONDS, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(MAX_RETRY_DELAY_SECONDS, float(2**attempt))

    def request_json(
        self,
        url: str,
        headers: Mapping[str, str],
        method: str = "GET",
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        return self.request_json_candidates(
            (url,),
            headers=headers,
            method=method,
            payload=payload,
        )

    def request_json_candidates(
        self,
        urls: Sequence[str],
        headers: Mapping[str, str],
        method: str = "GET",
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Try equivalent endpoint URLs in rotation without merging their data."""

        if not urls:
            raise AuditError("internal error: no request URL was provided")
        body_data = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        request_headers = dict(headers)
        if body_data is not None:
            request_headers["Content-Type"] = "application/json"

        last_error: Optional[BaseException] = None
        total_attempts = max(self.retries + 1, len(urls))
        for attempt in range(total_attempts):
            url = urls[attempt % len(urls)]
            self._pace()
            self.request_count += 1
            request = urllib.request.Request(
                url,
                data=body_data,
                headers=request_headers,
                method=method,
            )

            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout_seconds,
                    context=self.ssl_context,
                ) as response:
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > MAX_RESPONSE_BYTES:
                                raise AuditError(
                                    f"{method} response exceeded the "
                                    f"{MAX_RESPONSE_BYTES}-byte safety limit"
                                )
                        except ValueError:
                            pass

                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise AuditError(
                            f"{method} response exceeded the "
                            f"{MAX_RESPONSE_BYTES}-byte safety limit"
                        )
            except urllib.error.HTTPError as exc:
                last_error = exc
                if (
                    exc.code in RETRYABLE_HTTP_STATUSES
                    and attempt < total_attempts - 1
                ):
                    self.retry_count += 1
                    retry_delay = self._retry_delay(attempt, exc.headers)
                    exc.close()
                    time.sleep(retry_delay)
                    continue

                detail = exc.read(1024).decode("utf-8", errors="replace").strip()
                exc.close()
                suffix = f": {detail}" if detail else ""
                raise AuditError(
                    f"{method} {urllib.parse.urlsplit(url).path} failed with "
                    f"HTTP {exc.code}{suffix}"
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt < total_attempts - 1:
                    self.retry_count += 1
                    time.sleep(self._retry_delay(attempt))
                    continue
                reason = getattr(exc, "reason", exc)
                raise AuditError(
                    f"{method} {urllib.parse.urlsplit(url).path} failed: {reason}"
                ) from exc

            try:
                return json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AuditError(
                    f"{method} {urllib.parse.urlsplit(url).path} "
                    "did not return valid JSON"
                ) from exc

        raise AuditError(f"request failed after endpoint attempts: {last_error}")


class ShuffleApiClient(ResilientJsonClient):
    """Small Shuffle API client deliberately exposing GET only."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_SHUFFLE_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AuditError("--base-url must be a full http:// or https:// URL")
        if parsed.query or parsed.fragment:
            raise AuditError("--base-url cannot contain a query string or fragment")

        self.base_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self.api_key = api_key.strip()

        if not self.api_key:
            raise AuditError("an API key is required")
        super().__init__(
            timeout_seconds=timeout_seconds,
            retries=retries,
            request_delay_seconds=request_delay_seconds,
        )

    def get_json(self, path: str, org_id: Optional[str] = None) -> Any:
        if not path.startswith("/"):
            raise AuditError(f"internal error: API path must start with '/': {path}")

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "shuffle-usage-audit/1",
        }
        if org_id:
            headers["Org-Id"] = org_id

        return self.request_json(
            f"{self.base_url}{path}",
            headers=headers,
            method="GET",
        )


class OpenSearchClient(ResilientJsonClient):
    """Read-only OpenSearch client for uncapped audit history and pagination."""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        api_key: str = "",
        index_prefix: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
        insecure: bool = False,
        page_size: int = DEFAULT_OPENSEARCH_PAGE_SIZE,
    ) -> None:
        parsed_urls: List[str] = []
        for raw_url in base_url.split(","):
            parsed = urllib.parse.urlsplit(raw_url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise AuditError(
                    "--opensearch-url must contain full http:// or https:// URLs"
                )
            if parsed.query or parsed.fragment:
                raise AuditError(
                    "--opensearch-url entries cannot contain query strings or fragments"
                )
            normalized = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
            )
            if normalized not in parsed_urls:
                parsed_urls.append(normalized)
        if page_size < 1 or page_size > 1000:
            raise AuditError("--opensearch-page-size must be between 1 and 1000")

        self.base_urls = tuple(parsed_urls)
        self.base_url = self.base_urls[0]
        # Match Shuffle's GetESIndexPrefix behavior: preserve the configured
        # prefix, add one underscore, then lowercase the complete index name.
        self.index_prefix = index_prefix.strip()
        self.page_size = page_size
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "shuffle-usage-audit/1",
        }
        if api_key.strip():
            self.headers["Authorization"] = f"ApiKey {api_key.strip()}"
        elif username:
            credentials = base64.b64encode(
                f"{username}:{password}".encode("utf-8")
            ).decode("ascii")
            self.headers["Authorization"] = f"Basic {credentials}"

        super().__init__(
            timeout_seconds=timeout_seconds,
            retries=retries,
            request_delay_seconds=request_delay_seconds,
            insecure=insecure,
        )

    def index_name(self, base_name: str) -> str:
        index = (
            f"{self.index_prefix}_{base_name}"
            if self.index_prefix
            else base_name
        )
        return index.lower()

    def request_path(
        self,
        path: str,
        method: str = "GET",
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        urls = tuple(f"{base_url}{path}" for base_url in self.base_urls)
        if len(urls) == 1:
            # Keep this route overridable for in-memory test clients.
            return self.request_json(
                urls[0],
                headers=self.headers,
                method=method,
                payload=payload,
            )
        return self.request_json_candidates(
            urls,
            headers=self.headers,
            method=method,
            payload=payload,
        )

    def get_document(self, base_index: str, document_id: str) -> Mapping[str, Any]:
        index = urllib.parse.quote(self.index_name(base_index), safe="-_.")
        document = urllib.parse.quote(document_id, safe="")
        response = self.request_path(
            f"/{index}/_doc/{document}",
            method="GET",
        )
        if not isinstance(response, Mapping) or response.get("found") is False:
            raise AuditError(
                f"OpenSearch document {base_index}/{document_id} was not found"
            )
        source = response.get("_source")
        if not isinstance(source, Mapping):
            raise AuditError(
                f"OpenSearch document {base_index}/{document_id} had no _source"
            )
        return source

    def iter_documents(
        self,
        base_index: str,
        query: Mapping[str, Any],
        source_fields: Sequence[str],
    ) -> Iterable[Mapping[str, Any]]:
        """Yield an exact scroll result one page at a time and validate total hits."""

        index = urllib.parse.quote(self.index_name(base_index), safe="-_.")
        response = self.request_path(
            f"/{index}/_search?scroll=15m",
            method="POST",
            payload={
                "size": self.page_size,
                "track_total_hits": True,
                "query": query,
                "_source": list(source_fields),
                "sort": ["_doc"],
            },
        )

        scroll_id = ""
        expected_total: Optional[int] = None
        received_total = 0
        try:
            while True:
                if not isinstance(response, Mapping):
                    raise AuditError("OpenSearch search returned an invalid response")

                scroll_id = str(response.get("_scroll_id") or scroll_id)
                hits_wrapper = response.get("hits")
                if not isinstance(hits_wrapper, Mapping):
                    raise AuditError("OpenSearch search response had no hits object")

                if expected_total is None:
                    total = hits_wrapper.get("total", 0)
                    if isinstance(total, Mapping):
                        expected_total = require_non_negative_int(
                            total.get("value", 0),
                            "OpenSearch hits.total.value",
                        )
                        if total.get("relation", "eq") != "eq":
                            raise AuditError(
                                "OpenSearch did not return an exact total hit count"
                            )
                    else:
                        expected_total = require_non_negative_int(
                            total,
                            "OpenSearch hits.total",
                        )

                hits = hits_wrapper.get("hits")
                if not isinstance(hits, list):
                    raise AuditError("OpenSearch search response had no hits list")
                if not hits:
                    break

                for hit in hits:
                    if not isinstance(hit, Mapping):
                        raise AuditError(
                            "OpenSearch search returned a non-object document"
                        )
                    hit_id = str(hit.get("_id") or "")
                    if not hit_id:
                        raise AuditError(
                            "OpenSearch search returned a document without an ID"
                        )
                    source = hit.get("_source")
                    if not isinstance(source, Mapping):
                        raise AuditError(
                            f"OpenSearch document {hit_id} had no _source"
                        )
                    received_total += 1
                    if (
                        expected_total is not None
                        and received_total > expected_total
                    ):
                        raise AuditError(
                            "OpenSearch pagination returned more documents than "
                            f"its exact total of {expected_total}"
                        )
                    yield source

                if not scroll_id:
                    raise AuditError(
                        "OpenSearch did not return a scroll ID for complete pagination"
                    )
                response = self.request_path(
                    "/_search/scroll",
                    method="POST",
                    payload={"scroll": "15m", "scroll_id": scroll_id},
                )

            if expected_total is not None and received_total != expected_total:
                raise AuditError(
                    "OpenSearch pagination was incomplete: "
                    f"expected {expected_total} documents, received {received_total}"
                )
        finally:
            if scroll_id:
                try:
                    self.request_path(
                        "/_search/scroll",
                        method="DELETE",
                        payload={"scroll_id": [scroll_id]},
                    )
                except AuditError:
                    # The context expires automatically after two minutes.
                    pass


def flatten_orgs(raw_orgs: Sequence[Any]) -> Dict[str, Dict[str, Any]]:
    """Flatten top-level and nested child_orgs into an organization inventory."""

    flattened: Dict[str, Dict[str, Any]] = {}

    def visit(value: Any, explicit_parent: str = "") -> None:
        if not isinstance(value, Mapping):
            return

        org_id = str(value.get("id") or "").strip()
        if not org_id:
            return

        org_name = str(value.get("name") or "").strip()
        creator_org = str(value.get("creator_org") or explicit_parent or "").strip()
        current = flattened.setdefault(
            org_id,
            {
                "id": org_id,
                "name": org_name,
                "creator_org": creator_org,
                "child_ids": set(),
            },
        )
        if org_name:
            current["name"] = org_name
        if creator_org:
            current["creator_org"] = creator_org

        child_orgs = value.get("child_orgs")
        if not isinstance(child_orgs, list):
            return

        for child in child_orgs:
            if not isinstance(child, Mapping):
                continue
            child_id = str(child.get("id") or "").strip()
            if not child_id:
                continue
            current["child_ids"].add(child_id)
            visit(child, org_id)

    for raw_org in raw_orgs:
        visit(raw_org)

    return flattened


def select_org_ids(
    raw_orgs: Sequence[Any],
    root_org_id: Optional[str],
    all_visible_orgs: bool,
) -> Tuple[List[str], Dict[str, Optional[str]]]:
    orgs = flatten_orgs(raw_orgs)
    if not orgs:
        raise AuditError("GET /api/v1/orgs returned no visible organizations")

    if all_visible_orgs:
        selected = sorted(orgs)
        parents = {
            org_id: (orgs[org_id]["creator_org"] or None)
            for org_id in selected
        }
        return selected, parents

    root = (root_org_id or "").strip()
    if not root:
        raise AuditError(
            "--root-org-id is required unless --all-visible-orgs is explicitly selected"
        )
    if root not in orgs:
        raise AuditError(
            "the requested root org is not visible to this API key; "
            "use a key that belongs to the parent and child organizations"
        )

    adjacency: Dict[str, set[str]] = {org_id: set() for org_id in orgs}
    for org_id, org in orgs.items():
        creator_org = org["creator_org"]
        if creator_org in adjacency:
            adjacency[creator_org].add(org_id)
        for child_id in org["child_ids"]:
            if child_id in orgs:
                adjacency[org_id].add(child_id)

    selected: List[str] = []
    parents: Dict[str, Optional[str]] = {root: None}
    queue: deque[str] = deque([root])
    seen = {root}
    while queue:
        org_id = queue.popleft()
        selected.append(org_id)
        for child_id in sorted(adjacency.get(org_id, ())):
            if child_id in seen:
                continue
            seen.add(child_id)
            parents[child_id] = org_id
            queue.append(child_id)

    return selected, parents


def parse_cpu_quantity(value: Any) -> float:
    text = str(value).strip()
    if not text:
        raise ValueError("empty CPU quantity")
    if text.endswith("m"):
        return float(text[:-1]) / 1000.0
    return float(text)


def normalize_number(value: float) -> Any:
    rounded = round(value, 3)
    if rounded.is_integer():
        return int(rounded)
    return rounded


def run_inventory_command(args: Sequence[str], timeout_seconds: float = 10.0) -> str:
    completed = subprocess.run(
        list(args),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    return completed.stdout.strip()


def detect_docker_swarm_cpu() -> Optional[Dict[str, Any]]:
    if not shutil.which("docker"):
        return None

    try:
        control_available = run_inventory_command(
            ["docker", "info", "--format", "{{.Swarm.ControlAvailable}}"]
        ).lower()
        if control_available != "true":
            return None

        node_ids = run_inventory_command(
            ["docker", "node", "ls", "--filter", "status=ready", "--quiet"]
        ).splitlines()
        if not node_ids:
            return None

        # Inspect every node rather than relying on local-host capacity.
        output = run_inventory_command(
            [
                "docker",
                "node",
                "inspect",
                "--format",
                "{{.Status.State}}|{{.Spec.Availability}}|{{.Description.Resources.NanoCPUs}}",
                *node_ids,
            ]
        )

        total_nano_cpus = 0
        node_count = 0
        for line in output.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 3:
                continue
            state, availability, nano_cpus = parts
            if state.lower() != "ready" or availability.lower() != "active":
                continue
            total_nano_cpus += int(nano_cpus)
            node_count += 1

        if total_nano_cpus <= 0:
            return None
        return {
            "cores": normalize_number(total_nano_cpus / 1_000_000_000.0),
            "scope": "docker-swarm-ready-active-nodes",
            "node_count": node_count,
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def kubernetes_node_is_active(node: Mapping[str, Any]) -> bool:
    spec = node.get("spec")
    if isinstance(spec, Mapping) and spec.get("unschedulable") is True:
        return False

    status = node.get("status")
    if not isinstance(status, Mapping):
        return False
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return False

    return any(
        isinstance(condition, Mapping)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    )


def detect_kubernetes_cpu() -> Optional[Dict[str, Any]]:
    if not shutil.which("kubectl"):
        return None

    try:
        output = run_inventory_command(
            [
                "kubectl",
                "get",
                "nodes",
                "--output=json",
                "--request-timeout=10s",
            ],
            timeout_seconds=15.0,
        )
        payload = json.loads(output)
        nodes = payload.get("items")
        if not isinstance(nodes, list):
            return None

        total_cores = 0.0
        node_count = 0
        for node in nodes:
            if not isinstance(node, Mapping) or not kubernetes_node_is_active(node):
                continue
            status = node.get("status")
            capacity = status.get("capacity") if isinstance(status, Mapping) else None
            if not isinstance(capacity, Mapping):
                continue
            total_cores += parse_cpu_quantity(capacity.get("cpu"))
            node_count += 1

        if total_cores <= 0:
            return None
        return {
            "cores": normalize_number(total_cores),
            "scope": "kubernetes-ready-schedulable-nodes",
            "node_count": node_count,
        }
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return None


def detect_cpu_capacity(override: Optional[float]) -> Dict[str, Any]:
    if override is not None:
        if override <= 0:
            raise AuditError("--cpu-cores must be greater than zero")
        return {
            "cores": normalize_number(override),
            "scope": "operator-supplied",
            "node_count": None,
        }

    swarm = detect_docker_swarm_cpu()
    if swarm:
        return swarm

    kubernetes = detect_kubernetes_cpu()
    if kubernetes:
        return kubernetes

    local_cores = os.cpu_count()
    if not local_cores:
        raise AuditError(
            "CPU capacity could not be detected; provide --cpu-cores explicitly"
        )
    return {
        "cores": local_cores,
        "scope": "local-host-logical-cpus",
        "node_count": 1,
    }


def extract_owned_workflows(raw: Any, org_id: str) -> List[Mapping[str, Any]]:
    workflows = require_list(raw, "/api/v1/workflows")
    unique: Dict[str, Mapping[str, Any]] = {}
    anonymous_index = 0

    for workflow in workflows:
        if not isinstance(workflow, Mapping):
            continue
        if str(workflow.get("org_id") or "") != org_id:
            continue

        workflow_id = str(workflow.get("id") or "").strip()
        if not workflow_id:
            anonymous_index += 1
            workflow_id = f"__missing_id_{anonymous_index}"
        unique[workflow_id] = workflow

    return list(unique.values())


def count_active_environments(raw: Any, org_id: str) -> int:
    environments = require_list(raw, "/api/v1/environments")
    return count_active_environment_entries(environments, org_id)


def count_active_environment_entries(
    environments: Iterable[Any],
    org_id: str,
) -> int:
    seen: set[str] = set()
    anonymous_index = 0

    for environment in environments:
        if not isinstance(environment, Mapping):
            continue
        if environment.get("archived") is True:
            continue

        response_org_id = str(environment.get("org_id") or "").strip()
        if response_org_id and response_org_id != org_id:
            continue

        environment_id = str(environment.get("id") or "").strip()
        if not environment_id:
            anonymous_index += 1
            environment_id = f"__missing_id_{anonymous_index}"
        seen.add(environment_id)

    return len(seen)


def current_monthly_app_runs(stats: Any, current_month: int) -> Tuple[int, Optional[str]]:
    if not isinstance(stats, Mapping):
        raise AuditError("/api/v1/orgs/{id}/stats did not return a JSON object")

    runs = require_non_negative_int(
        stats.get("monthly_app_executions", 0),
        "monthly_app_executions",
    )
    reset_value = stats.get("last_monthly_reset_month")
    if reset_value is None:
        return runs, (
            "The statistics response did not include last_monthly_reset_month; "
            "the monthly counter was accepted as returned."
        )

    reset_month = require_non_negative_int(
        reset_value,
        "last_monthly_reset_month",
    )
    if reset_month not in range(1, 13):
        return runs, (
            "The statistics response had an invalid last_monthly_reset_month; "
            "the monthly counter was accepted as returned."
        )
    if reset_month != current_month:
        return runs, (
            "The statistics counter's last reset month differs from the collection "
            "month; monthly app runs may be stale."
        )
    return runs, None


def available_monthly_app_runs(stats: Any) -> Tuple[Dict[str, int], List[str]]:
    """Aggregate direct app executions from every valid daily statistic available."""

    if not isinstance(stats, Mapping):
        raise AuditError("/api/v1/orgs/{id}/stats did not return a JSON object")

    daily_statistics = stats.get("daily_statistics")
    if daily_statistics is None:
        return {}, []
    if not isinstance(daily_statistics, list):
        return {}, ["daily_statistics was not a list and could not be aggregated."]

    warnings: List[str] = []
    runs_by_day: Dict[str, int] = {}
    for index, entry in enumerate(daily_statistics):
        if not isinstance(entry, Mapping):
            warnings.append(f"daily_statistics[{index}] was not an object and was skipped.")
            continue

        raw_date = str(entry.get("date") or "").strip()
        match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:T|$)", raw_date)
        if not match:
            warnings.append(
                f"daily_statistics[{index}] had an invalid date and was skipped."
            )
            continue

        year, month, day = (int(part) for part in match.groups())
        try:
            datetime(year, month, day)
        except ValueError:
            warnings.append(
                f"daily_statistics[{index}] had an invalid calendar date and was skipped."
            )
            continue

        runs = require_non_negative_int(
            entry.get("app_executions", 0),
            f"daily_statistics[{index}].app_executions",
        )
        day_key = f"{year:04d}-{month:02d}-{day:02d}"
        if day_key in runs_by_day:
            warnings.append(
                f"Multiple daily statistics entries existed for {day_key}; "
                "the largest direct app-run value was used."
            )
            runs_by_day[day_key] = max(runs_by_day[day_key], runs)
        else:
            runs_by_day[day_key] = runs

    runs_by_month: Dict[str, int] = {}
    for day_key, runs in runs_by_day.items():
        month_key = day_key[:7]
        runs_by_month[month_key] = runs_by_month.get(month_key, 0) + runs
    return dict(sorted(runs_by_month.items())), warnings


def collect_org_metrics(
    client: Any,
    org_id: str,
    current_month_key: str,
    opensearch: Optional[OpenSearchClient] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    if opensearch:
        workflows: Iterable[Mapping[str, Any]] = opensearch.iter_documents(
            "workflow",
            {"match": {"org_id": org_id}},
            ["id", "org_id", "owner", "name", "hidden", "actions", "triggers"],
        )
    else:
        workflow_response = client.get_json(
            f"/api/v1/workflows?top={WORKFLOW_RESPONSE_LIMIT}&truncate=false",
            org_id=org_id,
        )
        response_workflows = require_list(workflow_response, "/api/v1/workflows")
        if len(response_workflows) >= WORKFLOW_RESPONSE_LIMIT:
            raise AuditError(
                f"workflow response reached the backend ceiling of "
                f"{WORKFLOW_RESPONSE_LIMIT}; configure --opensearch-url for "
                "complete paginated collection"
            )
        workflows = extract_owned_workflows(response_workflows, org_id)

    action_count = 0
    node_count = 0
    workflow_count = 0
    for workflow in workflows:
        if str(workflow.get("org_id") or "") != org_id:
            continue
        if workflow.get("hidden") is True:
            continue
        actions = workflow.get("actions")
        triggers = workflow.get("triggers")
        actions = actions if isinstance(actions, list) else []
        triggers = triggers if isinstance(triggers, list) else []
        if not str(workflow.get("name") or "") and len(actions) <= 1:
            continue
        if not str(workflow.get("org_id") or "") and not str(
            workflow.get("owner") or ""
        ):
            continue
        workflow_count += 1
        action_count += len(actions)
        node_count += len(actions) + len(triggers)

    if opensearch:
        environment_count = count_active_environment_entries(
            opensearch.iter_documents(
                "environments",
                {"match": {"org_id": org_id}},
                ["id", "org_id", "archived"],
            ),
            org_id,
        )
    else:
        environment_response = client.get_json("/api/v1/environments", org_id=org_id)
        environment_count = count_active_environments(environment_response, org_id)

    escaped_org_id = urllib.parse.quote(org_id, safe="")
    live_stats = client.get_json(
        f"/api/v1/orgs/{escaped_org_id}/stats",
        org_id=org_id,
    )
    monthly_app_runs, stats_warning = current_monthly_app_runs(
        live_stats,
        int(current_month_key[5:7]),
    )
    if stats_warning:
        warnings.append(stats_warning)

    history_stats = (
        opensearch.get_document("org_statistics", org_id)
        if opensearch
        else live_stats
    )
    daily_statistics = history_stats.get("daily_statistics")
    if (
        not opensearch
        and isinstance(daily_statistics, list)
        and len(daily_statistics) >= 365
    ):
        raise AuditError(
            "backend returned its maximum 365 daily statistics entries; "
            "configure --opensearch-url for complete history"
        )

    monthly_app_runs_by_month, history_warnings = available_monthly_app_runs(
        history_stats
    )
    warnings.extend(history_warnings)
    # The current monthly counter includes today's cache-backed increments, while
    # daily_statistics normally contains completed days only.
    monthly_app_runs_by_month[current_month_key] = monthly_app_runs
    monthly_app_runs_by_month = dict(sorted(monthly_app_runs_by_month.items()))

    return (
        {
            "workflow_count": workflow_count,
            "action_count": action_count,
            "node_count": node_count,
            "environment_count": environment_count,
            "monthly_app_runs": monthly_app_runs,
            "monthly_app_runs_by_month": monthly_app_runs_by_month,
            "stats_org_name": str(live_stats.get("org_name") or "").strip(),
        },
        warnings,
    )


def organization_inventory(
    selected_org_ids: Sequence[str],
    parent_by_org_id: Mapping[str, Optional[str]],
    orgs: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    organizations: List[Dict[str, Any]] = []
    for org_id in selected_org_ids:
        parent_id = parent_by_org_id.get(org_id)
        parent = orgs.get(parent_id, {}) if parent_id else {}
        organizations.append(
            {
                "org_id": org_id,
                "org_name": str(orgs.get(org_id, {}).get("name") or ""),
                "parent_org_id": parent_id,
                "parent_org_name": str(parent.get("name") or "") if parent_id else None,
            }
        )
    return organizations


def collect_audit(
    client: Any,
    root_org_id: Optional[str],
    all_visible_orgs: bool,
    cpu_capacity: Mapping[str, Any],
    allow_partial: bool,
    collected_at: Optional[datetime] = None,
    opensearch: Optional[OpenSearchClient] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    timestamp = collected_at or utc_now()
    raw_orgs = require_list(client.get_json("/api/v1/orgs"), "/api/v1/orgs")
    orgs = flatten_orgs(raw_orgs)
    selected_org_ids, parent_by_org_id = select_org_ids(
        raw_orgs,
        root_org_id,
        all_visible_orgs,
    )

    warnings: List[str] = []
    errors: List[str] = []
    metrics_by_org: Dict[str, Dict[str, Any]] = {}
    for org_index, org_id in enumerate(selected_org_ids, start=1):
        org_name = str(orgs.get(org_id, {}).get("name") or org_id)
        if progress:
            progress(
                f"Collecting organization {org_index}/{len(selected_org_ids)}: "
                f"{org_name} ({org_id})"
            )
        try:
            org_metrics, org_warnings = collect_org_metrics(
                client,
                org_id,
                timestamp.strftime("%Y-%m"),
                opensearch=opensearch,
            )
            metrics_by_org[org_id] = org_metrics
            if not orgs.get(org_id, {}).get("name") and org_metrics["stats_org_name"]:
                orgs[org_id]["name"] = org_metrics["stats_org_name"]
            warnings.extend(f"{org_name}: {warning}" for warning in org_warnings)
        except AuditError as exc:
            error = f"{org_name} ({org_id}): {exc}"
            if not allow_partial:
                raise AuditError(error) from exc
            errors.append(error)

    complete = not errors
    organizations = organization_inventory(selected_org_ids, parent_by_org_id, orgs)
    known_metrics = list(metrics_by_org.values())
    workflow_count = sum(metric["workflow_count"] for metric in known_metrics)
    action_count = sum(metric["action_count"] for metric in known_metrics)
    node_count = sum(metric["node_count"] for metric in known_metrics)
    environment_count = sum(metric["environment_count"] for metric in known_metrics)
    monthly_app_runs = sum(metric["monthly_app_runs"] for metric in known_metrics)
    available_months = sorted(
        {
            month
            for metric in known_metrics
            for month in metric["monthly_app_runs_by_month"]
        }
    )

    workflows_per_org: List[Dict[str, Any]] = []
    monthly_runs_per_org: List[Dict[str, Any]] = []
    per_org: List[Dict[str, Any]] = []
    for org_id in selected_org_ids:
        org = orgs.get(org_id, {})
        org_name = str(org.get("name") or "")
        parent_id = parent_by_org_id.get(org_id)
        parent_name = str(orgs.get(parent_id, {}).get("name") or "") if parent_id else None
        org_metrics = metrics_by_org.get(org_id)
        workflow_value = org_metrics["workflow_count"] if org_metrics else None
        monthly_value = org_metrics["monthly_app_runs"] if org_metrics else None
        workflows_per_org.append(
            {
                "org_id": org_id,
                "org_name": org_name,
                "workflow_count": workflow_value,
            }
        )
        monthly_runs_per_org.append(
            {
                "org_id": org_id,
                "org_name": org_name,
                "monthly_app_runs": monthly_value,
            }
        )
        per_org.append(
            {
                "org_id": org_id,
                "org_name": org_name,
                "parent_org_id": parent_id,
                "parent_org_name": parent_name,
                "environment_count": (
                    org_metrics["environment_count"] if org_metrics else None
                ),
                "workflow_count": workflow_value,
                "action_count": org_metrics["action_count"] if org_metrics else None,
                "node_count": org_metrics["node_count"] if org_metrics else None,
                "average_actions_per_workflow": (
                    round(org_metrics["action_count"] / workflow_value, 2)
                    if org_metrics and workflow_value
                    else 0.0 if org_metrics else None
                ),
                "average_nodes_per_workflow": (
                    round(org_metrics["node_count"] / workflow_value, 2)
                    if org_metrics and workflow_value
                    else 0.0 if org_metrics else None
                ),
                "monthly_app_runs": monthly_value,
                "monthly_app_runs_by_month": (
                    [
                        {
                            "month": month,
                            "monthly_app_runs": org_metrics[
                                "monthly_app_runs_by_month"
                            ].get(month, 0),
                        }
                        for month in available_months
                    ]
                    if org_metrics
                    else []
                ),
            }
        )

    monthly_app_runs_by_month: List[Dict[str, Any]] = []
    for month in available_months:
        per_org_month: List[Dict[str, Any]] = []
        month_total = 0
        for org_id in selected_org_ids:
            org_metrics = metrics_by_org.get(org_id)
            value = (
                org_metrics["monthly_app_runs_by_month"].get(month, 0)
                if org_metrics
                else None
            )
            if value is not None:
                month_total += value
            per_org_month.append(
                {
                    "org_id": org_id,
                    "org_name": str(orgs.get(org_id, {}).get("name") or ""),
                    "monthly_app_runs": value,
                }
            )
        monthly_app_runs_by_month.append(
            {
                "month": month,
                "monthly_app_runs": month_total,
                "source": (
                    "current-month-counter"
                    if month == timestamp.strftime("%Y-%m")
                    else "daily-statistics"
                ),
                "per_org": per_org_month,
            }
        )

    if cpu_capacity.get("scope") == "local-host-logical-cpus":
        warnings.append(
            "CPU capacity covers this host only; use --cpu-cores if the Shuffle "
            "deployment spans additional non-Swarm/non-Kubernetes hosts."
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": timestamp.isoformat().replace("+00:00", "Z"),
        "reporting_month_utc": timestamp.strftime("%Y-%m"),
        "audit_status": {
            "complete": complete,
            "errors": errors,
            "warnings": warnings,
        },
        "scope": {
            "selection": "all-visible-orgs" if all_visible_orgs else "root-and-descendants",
            "organizations": organizations,
            "cpu_scope": cpu_capacity.get("scope"),
            "cpu_node_count": cpu_capacity.get("node_count"),
            "collection_mode": (
                "shuffle-api-plus-opensearch" if opensearch else "shuffle-api"
            ),
        },
        "metrics": {
            "org_tenant_count": len(selected_org_ids),
            "cpu_core_count": cpu_capacity["cores"],
            "environment_count": environment_count,
            "workflow_count": workflow_count,
            "workflows_per_org": workflows_per_org,
            "average_actions_per_workflow": (
                round(action_count / workflow_count, 2) if workflow_count else 0.0
            ),
            "average_nodes_per_workflow": (
                round(node_count / workflow_count, 2) if workflow_count else 0.0
            ),
            "monthly_app_runs": monthly_app_runs,
            "monthly_app_runs_per_org": monthly_runs_per_org,
            "monthly_app_runs_by_month": monthly_app_runs_by_month,
            "per_org": per_org,
        },
        "methodology": {
            "api_operations": (
                "authenticated Shuffle HTTP GET requests plus read-only paginated "
                "OpenSearch searches"
                if opensearch
                else "authenticated Shuffle HTTP GET requests only"
            ),
            "completeness": (
                "OpenSearch scroll pagination is validated against exact total hits; "
                "the full stored org_statistics document supplies historical daily "
                "statistics"
                if opensearch
                else "collection fails at known Shuffle backend response ceilings "
                "instead of silently reporting capped results"
            ),
            "transport": {
                "sequential_requests": True,
                "shuffle_request_count": getattr(client, "request_count", None),
                "shuffle_retry_count": getattr(client, "retry_count", None),
                "opensearch_request_count": (
                    getattr(opensearch, "request_count", None)
                    if opensearch
                    else None
                ),
                "opensearch_retry_count": (
                    getattr(opensearch, "retry_count", None)
                    if opensearch
                    else None
                ),
                "opensearch_page_size": (
                    getattr(opensearch, "page_size", None) if opensearch else None
                ),
                "opensearch_endpoint_count": (
                    len(getattr(opensearch, "base_urls", ())) if opensearch else None
                ),
            },
            "environment_definition": "non-archived environments returned for each tenant",
            "workflow_definition": "unique workflows owned by each tenant",
            "action_definition": "entries in workflow.actions",
            "node_definition": "workflow actions plus triggers",
            "monthly_app_runs_definition": (
                "current month uses each tenant's direct monthly_app_executions "
                "counter; prior available months aggregate daily_statistics.app_executions; "
                "parent child counters are excluded to prevent double-counting"
            ),
            "data_handling": (
                "org names/IDs and aggregate usage metrics are written; workflow "
                "names/IDs/content, environment names/IDs, API credentials, and raw "
                "API responses are not written"
            ),
        },
    }
    return report


def markdown_report(report: Mapping[str, Any]) -> str:
    status = report["audit_status"]
    metrics = report["metrics"]
    scope = report["scope"]

    summary_rows = [
        ("Org / Tenant Count", metrics["org_tenant_count"]),
        ("CPU Core Count", metrics["cpu_core_count"]),
        ("Environment Count", metrics["environment_count"]),
        ("Workflow Count", metrics["workflow_count"]),
        ("Average Actions per Workflow", metrics["average_actions_per_workflow"]),
        ("Average Nodes per Workflow", metrics["average_nodes_per_workflow"]),
        ("Monthly App Runs (Current Month)", metrics["monthly_app_runs"]),
    ]

    lines = [
        "# Shuffle Usage Audit",
        "",
        f"- Generated (UTC): `{report['generated_at_utc']}`",
        f"- Reporting month (UTC): `{report['reporting_month_utc']}`",
        f"- Complete: `{'yes' if status['complete'] else 'no'}`",
        f"- CPU scope: `{scope['cpu_scope']}`",
        f"- Collection mode: `{scope.get('collection_mode', 'shuffle-api')}`",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {name} | {value} |" for name, value in summary_rows)

    lines.extend(
        [
            "",
        "## Per-org Metrics",
        "",
        "| Org Name | Org ID | Parent Org | Environments | Workflows | Actions | Nodes | Avg Actions / Workflow | Avg Nodes / Workflow | Monthly App Runs |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for org in metrics["per_org"]:
        parent = org["parent_org_name"] or "—"
        if org["parent_org_id"]:
            parent = f"{parent} (`{org['parent_org_id']}`)"
        lines.append(
            f"| {org['org_name']} | `{org['org_id']}` | {parent} | "
            f"{org['environment_count'] if org['environment_count'] is not None else 'unknown'} | "
            f"{org['workflow_count'] if org['workflow_count'] is not None else 'unknown'} | "
            f"{org['action_count'] if org['action_count'] is not None else 'unknown'} | "
            f"{org['node_count'] if org['node_count'] is not None else 'unknown'} | "
            f"{org['average_actions_per_workflow'] if org['average_actions_per_workflow'] is not None else 'unknown'} | "
            f"{org['average_nodes_per_workflow'] if org['average_nodes_per_workflow'] is not None else 'unknown'} | "
            f"{org['monthly_app_runs'] if org['monthly_app_runs'] is not None else 'unknown'} |"
        )

    lines.extend(
        [
            "",
            "## Monthly App Runs — All Available Months",
            "",
            "| Month | Organization | App Runs | Source |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for month_entry in metrics["monthly_app_runs_by_month"]:
        for org in month_entry["per_org"]:
            value = org["monthly_app_runs"]
            lines.append(
                f"| {month_entry['month']} | {org['org_name']} "
                f"(`{org['org_id']}`) | "
                f"{value if value is not None else 'unknown'} | "
                f"{month_entry['source']} |"
            )

    lines.extend(
        [
            "",
            "## Methodology",
            "",
            "- Organizations are listed by their Shuffle names and IDs.",
            "- Environments are non-archived environments returned for each tenant.",
            "- Actions are workflow action entries; nodes are actions plus triggers.",
            "- Current-month App Runs use each tenant's direct monthly counter. Prior "
            "available months are aggregated from daily statistics. Parent child-org "
            "counters are excluded to prevent double-counting.",
            (
                "- Complete-data mode uses authenticated Shuffle GET requests and "
                "read-only, paginated OpenSearch searches whose exact total is "
                "validated."
                if scope.get("collection_mode") == "shuffle-api-plus-opensearch"
                else "- API-only mode uses authenticated Shuffle GET requests and "
                "fails at known backend response ceilings."
            ),
            "- The collector writes org identifiers and aggregate metrics, but not "
            "raw API responses, workflow details, environment identifiers, or "
            "credentials.",
        ]
    )

    if status["warnings"]:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in status["warnings"])
    if status["errors"]:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in status["errors"])

    return "\n".join(lines) + "\n"


def write_private_text(path: Path, content: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def read_api_key(api_key_file: Optional[str]) -> str:
    return read_secret(
        api_key_file,
        "--api-key-file",
        ("SHUFFLE_AUDIT_API_KEY",),
    )


def read_secret(
    secret_file: Optional[str],
    option_name: str,
    environment_names: Sequence[str],
) -> str:
    if secret_file:
        try:
            return Path(secret_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AuditError(f"failed to read {option_name}: {exc}") from exc

    for environment_name in environment_names:
        value = os.environ.get(environment_name, "").strip()
        if value:
            return value
    return ""


def environment_float(name: str) -> Optional[float]:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise AuditError(f"{name} must be a number") from exc


def environment_int(name: str) -> Optional[int]:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise AuditError(f"{name} must be an integer") from exc


def environment_bool(*names: str) -> bool:
    for name in names:
        value = os.environ.get(name)
        if value is None or not value.strip():
            continue
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise AuditError(
            f"{name} must be one of true/false, yes/no, on/off, or 1/0"
        )
    return False


def first_environment_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def environment_or_default(value: Any, default: Any) -> Any:
    return default if value is None else value


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect Shuffle org, workflow, environment, CPU, and monthly app-run "
            "metrics. Configure OpenSearch for uncapped complete-data collection."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "SHUFFLE_AUDIT_BASE_URL",
            DEFAULT_SHUFFLE_BASE_URL,
        ),
        help=(
            "Shuffle backend URL "
            f"(default: {DEFAULT_SHUFFLE_BASE_URL}; or SHUFFLE_AUDIT_BASE_URL)"
        ),
    )
    parser.add_argument(
        "--root-org-id",
        default=os.environ.get("SHUFFLE_AUDIT_ROOT_ORG_ID"),
        help=(
            "Parent org to audit together with its descendants "
            "(or SHUFFLE_AUDIT_ROOT_ORG_ID)"
        ),
    )
    parser.add_argument(
        "--all-visible-orgs",
        action="store_true",
        help="Audit every org visible to the API key instead of a root hierarchy",
    )
    parser.add_argument(
        "--api-key-file",
        help="Read the API key from a file instead of SHUFFLE_AUDIT_API_KEY",
    )
    parser.add_argument(
        "--output",
        default="shuffle-usage-audit.json",
        help="JSON report path (default: shuffle-usage-audit.json)",
    )
    parser.add_argument(
        "--markdown-output",
        help="Markdown report path (default: JSON output path with .md suffix)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=environment_or_default(
            environment_float("SHUFFLE_AUDIT_TIMEOUT"),
            DEFAULT_TIMEOUT_SECONDS,
        ),
        help=(
            f"Per-request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g}; "
            "or SHUFFLE_AUDIT_TIMEOUT)"
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=environment_or_default(
            environment_int("SHUFFLE_AUDIT_RETRIES"),
            DEFAULT_RETRIES,
        ),
        help=(
            f"Retries for timeouts, HTTP 429, and HTTP 5xx "
            f"(default: {DEFAULT_RETRIES}; or SHUFFLE_AUDIT_RETRIES)"
        ),
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=environment_or_default(
            environment_float("SHUFFLE_AUDIT_REQUEST_DELAY"),
            DEFAULT_REQUEST_DELAY_SECONDS,
        ),
        help=(
            "Minimum seconds between sequential requests "
            f"(default: {DEFAULT_REQUEST_DELAY_SECONDS:g}; "
            "or SHUFFLE_AUDIT_REQUEST_DELAY)"
        ),
    )
    parser.add_argument(
        "--opensearch-url",
        default=first_environment_value(
            "SHUFFLE_AUDIT_OPENSEARCH_URL",
            "SHUFFLE_OPENSEARCH_URL",
        ),
        help=(
            "Comma-separated OpenSearch endpoints for one logical cluster "
            "(or SHUFFLE_AUDIT_OPENSEARCH_URL / SHUFFLE_OPENSEARCH_URL)"
        ),
    )
    parser.add_argument(
        "--opensearch-username",
        default=first_environment_value(
            "SHUFFLE_AUDIT_OPENSEARCH_USERNAME",
            "SHUFFLE_OPENSEARCH_USERNAME",
        ),
        help="OpenSearch basic-auth username (environment variables are also supported)",
    )
    parser.add_argument(
        "--opensearch-password-file",
        help=(
            "Read the OpenSearch password from a protected file instead of "
            "SHUFFLE_AUDIT_OPENSEARCH_PASSWORD"
        ),
    )
    parser.add_argument(
        "--opensearch-api-key-file",
        help=(
            "Read the OpenSearch API key from a protected file instead of "
            "SHUFFLE_AUDIT_OPENSEARCH_API_KEY"
        ),
    )
    parser.add_argument(
        "--opensearch-index-prefix",
        default=first_environment_value(
            "SHUFFLE_AUDIT_OPENSEARCH_INDEX_PREFIX",
            "SHUFFLE_OPENSEARCH_INDEX_PREFIX",
        ),
        help="OpenSearch index prefix used by Shuffle, if configured",
    )
    parser.add_argument(
        "--opensearch-page-size",
        type=int,
        default=environment_or_default(
            environment_int("SHUFFLE_AUDIT_OPENSEARCH_PAGE_SIZE"),
            DEFAULT_OPENSEARCH_PAGE_SIZE,
        ),
        help=(
            "Documents held in memory per OpenSearch page "
            f"(default: {DEFAULT_OPENSEARCH_PAGE_SIZE}; "
            "or SHUFFLE_AUDIT_OPENSEARCH_PAGE_SIZE)"
        ),
    )
    parser.add_argument(
        "--opensearch-insecure",
        action="store_true",
        default=environment_bool(
            "SHUFFLE_AUDIT_OPENSEARCH_INSECURE",
            "SHUFFLE_OPENSEARCH_SKIPSSL_VERIFY",
        ),
        help="Disable OpenSearch TLS certificate verification",
    )
    parser.add_argument(
        "--cpu-cores",
        type=float,
        default=environment_float("SHUFFLE_AUDIT_CPU_CORES"),
        help=(
            "Override auto-detected logical CPU capacity "
            "(or SHUFFLE_AUDIT_CPU_CORES)"
        ),
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Write an explicitly incomplete report instead of failing on a tenant error",
    )
    args = parser.parse_args(argv)

    if args.all_visible_orgs and args.root_org_id:
        parser.error("--all-visible-orgs and --root-org-id cannot be used together")
    if not args.all_visible_orgs and not args.root_org_id:
        parser.error("--root-org-id is required unless --all-visible-orgs is used")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = parse_args(argv)
        api_key = read_api_key(args.api_key_file)
        if not api_key:
            raise AuditError(
                "set SHUFFLE_AUDIT_API_KEY or provide a protected --api-key-file"
            )

        client = ShuffleApiClient(
            api_key,
            base_url=args.base_url,
            timeout_seconds=args.timeout,
            retries=args.retries,
            request_delay_seconds=args.request_delay,
        )
        opensearch = None
        if args.opensearch_url:
            opensearch = OpenSearchClient(
                args.opensearch_url,
                username=args.opensearch_username,
                password=read_secret(
                    args.opensearch_password_file,
                    "--opensearch-password-file",
                    (
                        "SHUFFLE_AUDIT_OPENSEARCH_PASSWORD",
                        "SHUFFLE_OPENSEARCH_PASSWORD",
                    ),
                ),
                api_key=read_secret(
                    args.opensearch_api_key_file,
                    "--opensearch-api-key-file",
                    (
                        "SHUFFLE_AUDIT_OPENSEARCH_API_KEY",
                        "SHUFFLE_OPENSEARCH_APIKEY",
                    ),
                ),
                index_prefix=args.opensearch_index_prefix,
                timeout_seconds=args.timeout,
                retries=args.retries,
                request_delay_seconds=args.request_delay,
                insecure=args.opensearch_insecure,
                page_size=args.opensearch_page_size,
            )
        cpu_capacity = detect_cpu_capacity(args.cpu_cores)
        report = collect_audit(
            client,
            root_org_id=args.root_org_id,
            all_visible_orgs=args.all_visible_orgs,
            cpu_capacity=cpu_capacity,
            allow_partial=args.allow_partial,
            opensearch=opensearch,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )

        json_path = Path(args.output)
        markdown_path = (
            Path(args.markdown_output)
            if args.markdown_output
            else json_path.with_suffix(".md")
        )
        write_private_text(
            json_path,
            json.dumps(report, indent=2, sort_keys=True) + "\n",
        )
        write_private_text(markdown_path, markdown_report(report))

        print(f"JSON report: {json_path.expanduser().resolve()}")
        print(f"Markdown report: {markdown_path.expanduser().resolve()}")
        print(
            "Status: "
            + ("complete" if report["audit_status"]["complete"] else "incomplete")
        )
        return 0 if report["audit_status"]["complete"] else 2
    except (AuditError, OSError) as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
