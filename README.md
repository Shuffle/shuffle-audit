# Shuffle Usage Audit Collector

This dependency-free Python script collects a licensing and usage report from a
customer-managed Shuffle deployment. It does not install packages, change
Shuffle configuration, or read/write workflow execution payloads.

## Metrics

- Org / tenant count
- Logical CPU core count
- Active environment count
- Workflow count
- Workflows per org
- Average actions per workflow
- Average nodes per workflow
- Monthly app runs for every month available
- Monthly app runs per org for every month available

The report includes each org's name, ID, parent relationship, and aggregate
metrics. It does not write workflow names/IDs/content, environment names/IDs,
API credentials, or raw API responses.

## Requirements

- Python 3.9 or newer
- A Shuffle API key whose user can access the parent org and its child orgs
- Network access to the Shuffle backend URL
- Read-only network access to OpenSearch for complete, uncapped workflow,
  environment, and historical statistics collection
- Permission to run `docker` or `kubectl` if cluster-wide CPU detection is
  expected

Create a dedicated, temporary audit account/API key where possible. The account
only needs read access to the audited org hierarchy. Revoke the key after the
report has been accepted.

## Run

Keep the API key out of shell history:

```bash
read -rsp "Shuffle audit API key: " SHUFFLE_AUDIT_API_KEY
export SHUFFLE_AUDIT_API_KEY
echo
read -rsp "OpenSearch password: " SHUFFLE_AUDIT_OPENSEARCH_PASSWORD
export SHUFFLE_AUDIT_OPENSEARCH_PASSWORD
echo

python3 shuffle_usage_audit.py \
  --base-url "http://localhost:5001" \
  --root-org-id "00000000-0000-0000-0000-000000000000" \
  --opensearch-url "https://localhost:9200" \
  --opensearch-username "admin" \
  --opensearch-insecure \
  --output "./shuffle-usage-audit.json"

unset SHUFFLE_AUDIT_API_KEY SHUFFLE_AUDIT_OPENSEARCH_PASSWORD
```

This complete-data mode uses Shuffle's backend for authorized org discovery and
the live current-month counter. It reads workflows, environments, and the full
stored `org_statistics` document directly from OpenSearch, avoiding the
backend's 600-workflow and 365-day response ceilings.

For a multi-node OpenSearch cluster, use Shuffle's existing comma-separated
format:

```bash
export SHUFFLE_AUDIT_OPENSEARCH_URL="https://search-1:9200,https://search-2:9200,https://search-3:9200"
```

These URLs must be interchangeable endpoints for the same logical cluster. The
collector rotates through them on connection errors, timeouts, HTTP 408/429, or
transient 5xx responses. It reads the dataset once; it does not add results from
each endpoint, which would double-count replicated shards.

This creates:

- `shuffle-usage-audit.json` for machine-readable review
- `shuffle-usage-audit.md` for a human-readable report

Both files are written with owner-only (`0600`) permissions because they include
customer org names and IDs. Review either file before sending it to Shuffle.

### Protect and transfer reports

The collector and visualizer source files do not need encryption. The generated
JSON and Markdown reports contain customer-identifying names, IDs, and usage
aggregates, so encrypt them before sending them off the audited machine or
retaining them outside an encrypted customer-controlled disk.

Do not add either plaintext report to Git. This repository ignores the default
report filenames.

Prefer recipient public-key encryption instead of sharing a report password.
For example, with [`age`](https://age-encryption.org/):

```bash
tar -czf - shuffle-usage-audit.json shuffle-usage-audit.md \
  | age --recipient "AGE_RECIPIENT_PUBLIC_KEY" \
      --output shuffle-usage-audit.tar.gz.age
```

The recipient can decrypt and extract it with:

```bash
age --decrypt --identity /secure/path/to/age-key.txt \
  shuffle-usage-audit.tar.gz.age | tar -xzf -
```

Keep the plaintext reports only as long as the customer and Shuffle require
them, following the customer's retention and secure-deletion policy. Encryption
does not replace using a temporary read-only audit account, revoking its API key
afterward, or protecting any credential files with `0600` permissions.

## Visualize the report

Start the private local dashboard after collecting the report:

```bash
cd shuffle-audit
python3 visualize_audit.py shuffle-usage-audit.json
```

The command opens `http://127.0.0.1:8765/` and keeps the server bound to the
local machine only. Press `Ctrl+C` when finished. Use `--no-browser` when
running over SSH, or `--port 0` to select an available local port.

The dashboard provides:

- Overall audit metric cards
- App-run history across every month available in Shuffle statistics
- A month selector for per-org app-run comparisons and table values
- Per-org bar charts for app runs, workflows, environments, and nodes
- Action/trigger composition
- Operational highlights
- Searchable and sortable per-org details
- Incomplete-report warnings
- Print / Save PDF support

The viewer has no external dependencies, CDN assets, analytics, or upload
endpoint. It serves only the selected report and bundled dashboard files over
loopback, with browser security headers and caching disabled.

The corresponding environment variables are:

```text
SHUFFLE_AUDIT_BASE_URL
SHUFFLE_AUDIT_ROOT_ORG_ID
SHUFFLE_AUDIT_API_KEY
SHUFFLE_AUDIT_CPU_CORES
SHUFFLE_AUDIT_TIMEOUT
SHUFFLE_AUDIT_RETRIES
SHUFFLE_AUDIT_REQUEST_DELAY
SHUFFLE_AUDIT_OPENSEARCH_URL
SHUFFLE_AUDIT_OPENSEARCH_USERNAME
SHUFFLE_AUDIT_OPENSEARCH_PASSWORD
SHUFFLE_AUDIT_OPENSEARCH_API_KEY
SHUFFLE_AUDIT_OPENSEARCH_INDEX_PREFIX
SHUFFLE_AUDIT_OPENSEARCH_PAGE_SIZE
SHUFFLE_AUDIT_OPENSEARCH_INSECURE
```

The collector also recognizes Shuffle's existing
`SHUFFLE_OPENSEARCH_URL`, `SHUFFLE_OPENSEARCH_USERNAME`,
`SHUFFLE_OPENSEARCH_PASSWORD`, `SHUFFLE_OPENSEARCH_APIKEY`,
`SHUFFLE_OPENSEARCH_INDEX_PREFIX`, and
`SHUFFLE_OPENSEARCH_SKIPSSL_VERIFY` variables. Audit-specific variables take
precedence.

Index prefixes follow Shuffle's naming behavior exactly. For example,
`SHUFFLE_OPENSEARCH_INDEX_PREFIX=customer_one` reads
`customer_one_workflow`, `customer_one_environments`, and
`customer_one_org_statistics`. You can also override it for the audit only:

```bash
export SHUFFLE_AUDIT_OPENSEARCH_INDEX_PREFIX="customer_one"
```

Use a protected key file instead of an environment variable if required:

```bash
chmod 600 /secure/path/shuffle-audit.key
python3 shuffle_usage_audit.py \
  --base-url "https://shuffle.customer.example" \
  --root-org-id "00000000-0000-0000-0000-000000000000" \
  --api-key-file /secure/path/shuffle-audit.key \
  --opensearch-url "https://localhost:9200" \
  --opensearch-username "admin" \
  --opensearch-password-file /secure/path/opensearch-audit-password
```

Use `--all-visible-orgs` only when every org visible to the key is intentionally
in audit scope. The safer default requires `--root-org-id` and selects only that
org and its descendants.

## Metric definitions

- **Workflow**: a unique workflow owned by the audited org. A workflow merely
  distributed into another org is not counted twice.
- **Action**: an entry in `workflow.actions`.
- **Node**: an action or trigger. Branch edges and comments are not nodes.
- **Environment**: a non-archived environment returned for the org.
- **Monthly App Runs**: the current month uses the org's direct
  `monthly_app_executions` counter, including live cached increments. Every
  earlier month available in `daily_statistics` is aggregated from direct
  `app_executions`. The parent `monthly_child_app_executions` and
  `child_app_executions` fields are deliberately excluded because each child is
  collected separately.
- **CPU Core Count**: logical CPU capacity. The script tries ready/active Docker
  Swarm nodes, then ready/schedulable Kubernetes nodes, then the local host.
  For another topology, pass the deployment-wide total using `--cpu-cores`.

Averages are weighted across all workflows, not averaged from per-org averages.

## Complete collection and large-data safety

The Shuffle API side uses only these authenticated HTTP GET operations:

```text
GET /api/v1/orgs
GET /api/v1/orgs/{org_id}/stats               (once per org)
```

Complete mode uses read-only OpenSearch document searches:

```text
POST /{workflow-index}/_search?scroll=15m      (once per org)
POST /{environment-index}/_search?scroll=15m   (once per org)
POST /_search/scroll                           (once per result page)
GET  /{org-statistics-index}/_doc/{org_id}     (once per org)
DELETE /_search/scroll                         (clears temporary scroll context)
```

The OpenSearch `POST` requests are searches, not writes. The `DELETE` removes
only the temporary search cursor; it does not delete an index or customer
document.

Large result sets are handled sequentially:

- Workflows and environments are read in pages of 250 by default.
- Only the current page and aggregate counters are retained in memory.
- Exact OpenSearch total hits are required and checked against the number of
  documents received. Incomplete pagination fails the audit.
- Requests are paced with a 0.2-second minimum interval.
- Each request has a 120-second timeout and up to three retries for timeouts,
  HTTP 408/429, and transient 5xx responses.
- Retry delays use exponential backoff and honor `Retry-After`.
- Comma-separated endpoints are attempted in rotation, matching Shuffle's
  `SHUFFLE_OPENSEARCH_URL` format.
- A single JSON response is limited to 128 MiB to avoid runaway memory use.
- Progress is printed one organization at a time, which makes a roughly
  50-tenant audit easy to monitor.

Tune these safely with `--opensearch-page-size`, `--request-delay`, `--timeout`,
and `--retries`. A smaller page size lowers peak memory; a larger delay lowers
load. Collection remains sequential regardless of these values.

The Shuffle backend URL defaults to `http://localhost:5001`. Use `--base-url`
or `SHUFFLE_AUDIT_BASE_URL` to target any other authorized HTTP or HTTPS Shuffle
deployment:

```bash
python3 shuffle_usage_audit.py \
  --base-url "https://shuffle.customer.example" \
  --root-org-id "00000000-0000-0000-0000-000000000000"
```

CPU discovery runs fixed read-only inventory commands (`docker info`,
`docker node ls/inspect`, or `kubectl get nodes`). The only local writes are the
two requested report files.

Shuffle's current backend may internally update short-lived authentication/API
usage caches while serving authenticated GET requests. If an org statistics
document is missing, the statistics GET handler may initialize it. These are
server-side bookkeeping effects of the existing GET handlers; the collector
does not change customer configuration, workflow data, execution data, or
statistics.

Without `--opensearch-url`, API-only fallback mode uses
`GET /api/v1/workflows?top=600&truncate=false` and
`GET /api/v1/environments` for each org. It refuses to call a report complete
when the workflow response reaches 600 or statistics reaches 365 daily entries.
This prevents silent under-reporting, but API-only mode cannot guarantee
multi-year completeness after those ceilings are reached.

The collector also fails if any selected tenant cannot be read.
`--allow-partial` is available for troubleshooting, but its report is
explicitly marked incomplete and should not be used as a final audit.

Comma-separated endpoints are for nodes or gateways serving the same
OpenSearch cluster. If the URLs point to independent clusters containing
different datasets, they cannot be safely merged without an explicit ownership
map because replicated organizations would otherwise be double-counted.

## Test

The tests use only in-memory fake API responses and do not contact a deployment:

```bash
cd shuffle-audit
python3 -m unittest -v
node --test viewer/test_app.js
```
