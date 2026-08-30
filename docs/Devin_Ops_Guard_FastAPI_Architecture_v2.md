# Devin-Ops Guard: Event-Driven Autonomous Remediation Engine

## Executive Pitch and Implementation Architecture

Devin-Ops Guard converts a deliberately approved GitHub issue into a bounded, observable Devin remediation run. It is designed for maintenance work where a developer would otherwise need to inspect a large codebase, make a multi-file change, execute focused tests, and open a pull request.

The demo scope is intentionally narrow: a small set of reproducible code-quality or dependency-maintenance issues in a personal Apache Superset fork, authored by hand. Issues are always written by a human — the system never creates issues, only remediates ones a maintainer has approved by labeling. Each issue must include expected behavior, acceptance criteria, and a focused test command. A maintainer adds `devin-autofix`; Devin investigates and remediates it; the service reports the evidence and PR.

---

## 1. Problem and Outcome

The problem is not issue creation—it is turning a maintenance ticket into a reviewable engineering outcome without a developer coordinating each investigation and test loop.

Devin is the execution primitive. The FastAPI service governs when Devin may run, gives it repository and issue context, constrains its ACU spend, and independently verifies the resulting workflow state.

**Demo success definition**

1. A labeled issue triggers exactly one Devin session.
2. Devin opens a PR against the Superset fork that references the issue.
3. Devin returns a structured report naming its outcome and tests.
4. The service marks the run `remediated` as soon as GitHub confirms a PR that Devin opened for the issue (target repo, default branch, references the issue, open or merged) — the task is done when the PR exists, not when it merges.
5. A technical viewer can inspect the delivery, session, report, PR, duration, and ACUs via the API/dashboard.

---

## 2. Architecture

```text
GitHub fork: Issues webhook
  issues.opened (label already present) | issues.labeled (label added)
                 |
                 v
FastAPI /webhook/github
  - verify X-Hub-Signature-256 over raw body
  - allowlist owner/repository and event/action/label
  - SQLite transaction: dedupe delivery + reserve issue job
                 |
                 v
SQLite-backed job worker
  - create correlation tag and persist `creating`
  - POST Devin v3 organization session
  - save session_id and Devin session URL
                 |
                 v
Devin API and connected GitHub repository
  - inspect, edit, test, commit, and open PR
                 |
                 v
Session monitor (periodic GET per active session)
  - persist status, status_detail, ACUs, PR URLs, structured output
  - post GitHub issue updates on started / completed / needs-review
                 |
                 v
/metrics, /runs, and CLI dashboard
```

The FastAPI container and SQLite file run from one Docker Compose project. `./data` is mounted as the persistent volume. JSON is not used as a locking mechanism because it cannot safely coordinate concurrent workers or restarts.

---

## 3. GitHub Event Contract and Safety Boundary

The GitHub webhook accepts only the target fork and only the following issue events:

| GitHub event | Required action | Trigger condition |
|---|---|---|
| `issues` | `opened` | Issue already contains `devin-autofix` |
| `issues` | `labeled` | Added label is exactly `devin-autofix` |

Before parsing or dispatching, the handler verifies `X-Hub-Signature-256` with HMAC-SHA256 over the **raw** request body using `GITHUB_WEBHOOK_SECRET`. It also rejects events whose `repository.full_name` is not `TARGET_REPOSITORY`.

The handler writes the `X-GitHub-Delivery` identifier to SQLite. Re-delivery of the same ID returns HTTP 200 without launching another session. It then reserves a single active job for `(repository_id, issue_number)`; label edits and separate deliveries cannot create a second active run for that issue.

The webhook request returns quickly after durable reservation. A worker creates the Devin session outside the request path, so GitHub retries cannot be mistaken for a long-running remediation request.

---

## 4. Persistent State and Idempotency

Use SQLite with WAL mode and `BEGIN IMMEDIATE` transactions. At minimum, maintain these tables:

```text
deliveries(delivery_id PRIMARY KEY, received_at, repository_id, event, action)

jobs(id PRIMARY KEY, repository_id, issue_number, issue_url, title,
     state, correlation_tag UNIQUE, session_id UNIQUE, session_url,
     created_at, started_at, completed_at, last_polled_at,
     status, status_detail, acus_consumed, pr_url,
     structured_output_json, error)
```

Add a partial unique index equivalent to one active job per issue (`creating`, `queued`, `running`, `waiting_for_approval`, `waiting_for_user`).

State transitions:

```text
queued -> creating -> running -> remediated
                    |          -> needs_review
                    |          -> failed
                    -> creation_unknown -> reconciled | needs_review
```

Persist `creating` and the correlation tag before the outbound POST. If a POST times out after Devin accepted it, the job is `creation_unknown`; reconcile it by listing or searching the organization sessions for the correlation tag instead of submitting a duplicate. Retain the job for human review if reconciliation cannot prove the session identity.

---

## 5. Devin v3 Session Contract

The endpoint is:

```text
POST https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}/sessions
Authorization: Bearer <service-user credential>
Content-Type: application/json
```

The credential must be a Devin service-user credential and have organization-level `UseDevinSessions`. The monitor needs `ViewOrgSessions`. The organization ID uses the `org-` prefix.

Example create payload:

```json
{
  "title": "Ops Guard: Superset issue #123",
  "repos": ["<configured Devin repository identifier>"],
  "tags": ["ops-guard", "ops-guard:issue:123:attempt:1"],
  "max_acu_limit": 10,
  "resumable": false,
  "structured_output_required": true,
  "structured_output_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["outcome", "summary", "tests_run", "pr_url", "blocker"],
    "properties": {
      "outcome": {"type": "string", "enum": ["remediated", "blocked", "not_reproducible"]},
      "summary": {"type": "string"},
      "tests_run": {"type": "array", "items": {"type": "string"}},
      "pr_url": {"type": ["string", "null"]},
      "blocker": {"type": ["string", "null"]}
    }
  },
  "prompt": "You are remediating GitHub issue #123 in <fork URL>.\n\nIssue title: <title>\nIssue body: <body>\n\nWork only on this issue. Inspect the existing code and tests before editing. Implement the smallest correct fix, add or update focused tests, run the issue's stated test command, then open a PR against the fork's default branch. In your final structured output, report the PR URL, outcome, concise summary, tests run, and any blocker. Do not broaden the change or modify unrelated dependencies."
}
```

`prompt` is the only required create field, but `repos`, a descriptive title, a correlation tag, an ACU limit, and the structured-output schema make this automation governable. `repos` entries are repository path strings (e.g. `"e4c5/superset"`), not opaque ids; the repo must already be connected to the org's Git connection. The create response contains `session_id`, `url`, `status` (initially `new`), `pull_requests`, `acus_consumed`, and `structured_output`; save it immediately. List-by-tag responses are paginated as `{ "items": [...], "end_cursor", "has_next_page" }`.

`GITHUB_TOKEN` belongs to Guard for issue comments and PR verification. It does **not** grant Devin write access. Before the demo, connect the fork to Devin and confirm its configured repository connection has permission to create branches and PRs.

---

## 6. Session Management and Verified Completion

The monitor polls each non-terminal session:

```text
GET /v3/organizations/{DEVIN_ORG_ID}/sessions/{session_id}
```

Poll with bounded exponential backoff (15 seconds initially, up to 60 seconds), respect `429` with a retry delay, and persist every observed status. Useful API fields are `status`, `status_detail`, `acus_consumed`, `pull_requests`, `structured_output`, and `url`.

The session model has a coarse `status` and a finer `status_detail`:

- `status`: `new | claimed | running | exit | error | suspended | resuming`
- `status_detail`: `working | waiting_for_user | waiting_for_approval | finished | inactivity | user_request | usage_limit_exceeded | out_of_credits | out_of_quota | no_quota_allocation | payment_declined | org_usage_limit_exceeded | user_usage_limit_exceeded | total_session_limit_exceeded | error`

`status: exit` means the session has ended; it does not by itself mean a fix was delivered. A paused session keeps `status: running` with `status_detail: waiting_for_user`.

The success signal is a pull request Devin opened for the issue, checked on every
poll regardless of session `status`.

| Condition | Guard state |
|---|---|
| GitHub confirms a PR for the issue: URL/base repo == `TARGET_REPOSITORY`, targets the default branch, body references `#<issue>`, open or merged | `remediated` |
| `status == error` or `status_detail == error` (and no such PR) | `failed` |
| `status == suspended` or `status_detail` is a billing/usage-limit reason (and no such PR) | `failed` |
| `status_detail` in {`waiting_for_user`, `waiting_for_approval`}, no PR, within `WAITING_GRACE` (30 min since the session first paused) | keep polling (stays `running`), post a one-time "needs input" note |
| `status_detail` in {`waiting_for_user`, `waiting_for_approval`}, no PR, waiting longer than `WAITING_GRACE` | `needs_review` |
| `status == exit` + output `blocked` or `not_reproducible` | `needs_review` |
| `status == exit` without a verified PR | `needs_review` |

On `waiting_for_user`, a maintainer can intervene in Devin or Guard can send a narrowly scoped clarification through:

```text
POST /v3/organizations/{DEVIN_ORG_ID}/sessions/{session_id}/messages
```

Do not automatically answer broad technical questions; that hides failure modes and reduces reviewability.

---

## 7. Observable Outputs

Guard posts GitHub issue comments at three points: session started (including Devin URL), remediation completed (PR URL, tests, ACUs), and needs-review/failed (reason and Devin URL).

Expose:

```text
GET /health
GET /metrics
GET /runs
GET /runs/{issue_number}
```

`/metrics` returns, at minimum:

```json
{
  "active_runs": 1,
  "remediated_total": 4,
  "needs_review_total": 1,
  "failed_total": 1,
  "reconciled_total": 0,
  "prs_opened_total": 4,
  "median_elapsed_seconds": 842,
  "acus_per_remediated_run": 6.75,
  "total_jobs": 6
}
```

All values are computed locally from job records. Because a run is finalized
`remediated` the moment its PR is verified (not when the session exits),
`median_elapsed_seconds` is time-to-PR and `acus_per_remediated_run` is the ACU
count observed at that point — a lower bound on total session cost if Devin
keeps working after opening the PR.

`/runs/{issue_number}` is the audit record: GitHub delivery IDs, timestamps, job state, session URL and ID, current Devin status, ACU consumption, structured output, error details, and verified PR URL. The CLI dashboard is optional; the REST endpoints are the authoritative demo output.

---

## 8. Docker Configuration and Setup

Required environment variables:

```text
DEVIN_SERVICE_USER_TOKEN=<cog_...>
DEVIN_ORG_ID=<org-...>
DEVIN_REPOSITORY_ID=<configured repository identifier>
GITHUB_WEBHOOK_SECRET=<random secret>
GITHUB_TOKEN=<token able to comment on issues and read PR state>
TARGET_REPOSITORY=<owner/superset-fork>
MAX_ACU_LIMIT=10
```

```bash
docker compose up --build
ngrok http 8000
```

Configure the GitHub fork’s **Issues** webhook to `https://<ngrok-url>/webhook/github` and set its secret to `GITHUB_WEBHOOK_SECRET`. Create the selected issue, document its acceptance criteria, then add `devin-autofix`.

For a repeatable recording, include a `scripts/emit_sample_webhook.py` command that sends a correctly signed fixture to a local container. This validates the full dispatch and persistence path without requiring a public tunnel; the live GitHub label demo validates the real integration.

---

## 9. Five-Minute Loom Story

| Time | Message | Evidence to show |
|---|---|---|
| 0:00–0:45 | **What:** approved maintenance issues consume engineering coordination time. | The concrete Superset fork issue and its acceptance criteria. |
| 0:45–2:15 | **How:** a maintainer labels the issue; Guard verifies, deduplicates, and creates a governed session. | GitHub label, webhook log, SQLite/run record, submitted v3 request, Devin session URL. |
| 2:15–3:30 | **Proof:** the service monitors rather than assuming success. | Devin status/structured output, PR, issue comment, and `/runs/{issue}`. |
| 3:30–4:15 | **Why Devin:** it performs the investigate-edit-test-PR loop inside a large codebase, while Guard defines policy and evidence. | PR diff and focused test result. |
| 4:15–5:00 | **When next:** introduce scan events, human approval policies, per-team budgets, and production telemetry. | `/metrics` including throughput, success rate, latency, and ACUs. |

Avoid an unsupported blanket percentage for maintenance cost. The proof point for this demo is concrete: one approved issue becomes one verified PR with a measured elapsed time and ACU cost.

---

## 10. Production Extensions

After proving the issue-label workflow, add SonarQube/Snyk scan triggers, risk-tiered ACU caps, an approval gate before PR creation for high-risk paths, daily throughput and cost reporting, retry queues with operator alerts, and a curated Devin playbook/knowledge note for the repository’s test and contribution conventions.
