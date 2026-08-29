# Devin-Ops Guard

Event-driven autonomous remediation engine. A maintainer labels a GitHub issue
`devin-autofix`; Ops Guard verifies the event, opens exactly one governed Devin
session, and **independently verifies** the result before declaring success.

> Ops Guard never creates issues. Humans author and approve issues; Ops Guard
> only remediates ones a maintainer has explicitly labeled.

- **Solution repo:** this repository
- **Superset fork + remediated issues:** https://github.com/e4c5/superset/issues?q=label%3Adevin-autofix

---

## The problem

Approved maintenance work — CVE bumps, dependency upgrades, lint/code-quality
findings, small reproducible bugs — is cheap to decide and expensive to execute.
A human still has to clone a large unfamiliar codebase (Superset is ~500k LOC),
locate the code, make a multi-file change, write and run focused tests, and open
a PR. That investigate → edit → test → PR loop is what consumes senior-engineer
time, not the decision to fix.

**Ops Guard turns "a maintainer labelled this issue" into "here is a reviewed PR
with test evidence and a measured ACU cost."** The service owns *policy and
proof*: which events may spend budget, how much, and what counts as done. Devin
owns *execution*: the actual code investigation and change inside the repo.

### Why Devin specifically

The remediation step is an open-ended agentic loop in a codebase the automation
has never indexed — find the offending call site, understand surrounding tests,
make the smallest correct change, run the stated test command, iterate on
failures, open the PR. That is not scriptable with templates or codemods. Devin
is the primitive that makes an *event → merged PR* pipeline practical; Ops Guard
is the governor that makes it safe to run unattended.

---

## Quick start — simulate the full pipeline (no Devin, no GitHub)

```bash
pip install -r requirements.txt
python scripts/selftest.py     # 19 checks: webhook auth, dedup, one-active-job, state machine, endpoints
python scripts/e2e_test.py     # worker + monitor against mocked Devin/GitHub HTTP, 6 scenarios
```

This exercises webhook verification → job reservation → session create → monitor
verification end to end, including the safety case where a session claims
`remediated` but GitHub cannot confirm the PR (→ `needs_review`, not
`remediated`). See [Live setup](#setup) below to run it against the real Devin API.

---

## How it works

```
GitHub fork  ──issues.labeled / issues.opened──▶  POST /webhook/github
                                                    │  verify X-Hub-Signature-256 (HMAC-SHA256, raw body)
                                                    │  allowlist repository.full_name == TARGET_REPOSITORY
                                                    │  SQLite txn: dedupe X-GitHub-Delivery + reserve ONE active job/issue
                                                    ▼
                                              worker (in-process)
                                                    │  persist `creating` + correlation tag BEFORE the POST
                                                    │  POST /v3/organizations/{org}/sessions  (max_acu_limit, structured_output_schema)
                                                    │  uncertain POST → `creation_unknown` → reconcile by correlation tag, never blind-retry
                                                    ▼
                                              monitor (in-process)
                                                    │  GET /v3/organizations/{org}/sessions/{id}  (backoff 15→60s, honor 429)
                                                    │  `exit` means ended, NOT succeeded
                                                    │  remediated ⟺ exit + structured_output.outcome==remediated + PR URL + GitHub says PR open/merged
                                                    ▼
                                    issue comments  +  GET /health /metrics /runs /runs/{issue}
```

Single process, single Docker Compose service, SQLite (WAL) on a mounted volume.

## State machine

```
queued → creating → running → remediated | needs_review | failed
                 ↘ creation_unknown → running (reconciled) | needs_review
```

A partial unique index enforces **one active job per `(repository, issue)`**.
`X-GitHub-Delivery` is stored and deduped. Re-labels and webhook retries cannot
launch a second session.

---

## Setup

### 1. Prerequisites

- A Devin **organization** and a **service-user credential** with permission to
  create sessions (`UseDevinSessions`) and view org sessions (`ViewOrgSessions`).
- Your Superset fork **connected to Devin** (org-level Git connection) with
  permission to create branches and open PRs. Devin opens the PR itself.
- A GitHub token that can comment on issues and read PR state in the fork. This
  token is **not** used for code changes.

Resolve the repository identifier Devin expects:

```bash
curl -s https://api.devin.ai/v3/organizations/$DEVIN_ORG_ID/repositories \
  -H "Authorization: Bearer $DEVIN_SERVICE_USER_TOKEN"
```

Use the returned identifier as `DEVIN_REPOSITORY_ID`.

### 2. Configure

```bash
cp .env.example .env
# fill in every value
```

| Variable | Purpose |
|---|---|
| `DEVIN_SERVICE_USER_TOKEN` | Devin service-user credential (Bearer) |
| `DEVIN_ORG_ID` | `org-…` organization id |
| `DEVIN_REPOSITORY_ID` | identifier from `GET /v3/organizations/{org}/repositories` |
| `DEVIN_API_BASE` | default `https://api.devin.ai` |
| `GITHUB_WEBHOOK_SECRET` | shared secret; also set on the fork webhook |
| `GITHUB_TOKEN` | issue comments + PR state reads only |
| `TARGET_REPOSITORY` | `owner/superset-fork` — the only accepted repo |
| `MAX_ACU_LIMIT` | per-session ACU cap (default 10) |
| `DEVIN_AUTOFIX_LABEL` | trigger label (default `devin-autofix`) |
| `BYPASS_APPROVAL` | `true` so unattended sessions don't stall at approval |

### 3. Run

```bash
docker compose up --build           # FastAPI + SQLite, one service, port 8000
npx -y localtunnel@2 --port 8000    # or: ngrok http 8000 — any public HTTPS tunnel
```

Add a webhook to the fork → **Settings ▸ Webhooks**:
- Payload URL: `https://<tunnel-host>/webhook/github`
- Content type: `application/json`
- Secret: same as `GITHUB_WEBHOOK_SECRET`
- Events: **Issues** only

The tunnel host changes on restart; re-point the hook with
`gh api repos/<owner>/<fork>/hooks/<id> -X PATCH -f "config[url]=https://<new-host>/webhook/github"`.

### 4. Trigger a run

Open one of the authored issues in the fork, then add the `devin-autofix` label.
Ops Guard comments **"Devin session started"** with the session URL within
seconds; watch progress at `GET /runs/{issue_number}` or `python scripts/dashboard.py`.

---

## Observability

| Endpoint | Content |
|---|---|
| `GET /health` | process + DB reachable |
| `GET /metrics` | `active_runs`, `remediated_total`, `needs_review_total`, `failed_total`, `prs_opened_total`, `median_elapsed_seconds`, `acus_per_remediated_run` |
| `GET /runs` | every job, summarized |
| `GET /runs/{issue_number}` | full audit record: delivery, timestamps, state, session id/URL, Devin status, ACUs, structured output, verified PR URL, event log |

Every state transition emits one JSON log line. Optional terminal view:

```bash
python scripts/dashboard.py            # polls /metrics + /runs
```

Ops Guard also comments on the issue at three points: session started,
remediation completed (PR + tests + ACUs), and needs-review/failed (reason + Devin URL).

---

## Testing

No Devin/GitHub network calls; both are offline.

```bash
python scripts/selftest.py    # webhook auth, dedup, one-active-job, state machine, endpoints
python scripts/e2e_test.py    # worker + monitor with mocked Devin/GitHub HTTP
```

`e2e_test.py` covers the key safety property: a session that `exit`s claiming
`remediated` but whose PR GitHub cannot confirm becomes `needs_review`, not
`remediated`.

Replay a signed webhook against a running container:

```bash
GITHUB_WEBHOOK_SECRET=... TARGET_REPOSITORY=owner/fork \
  python scripts/emit_sample_webhook.py --repo owner/fork --issue 42 --action labeled

# prove dedup — same delivery id twice:
... --delivery 11111111-1111-1111-1111-111111111111
# prove auth — bad signature → 401:
... --bad-signature
```

---

## Authoring issues in the fork (manual)

Create a small set (3–4) covering: dependency/CVE upgrade, code-quality/lint
finding, a small reproducible bug, missing test coverage. Each issue body must
state **expected behavior**, **acceptance criteria**, and an **exact focused
test command**. Create the `devin-autofix` label but do not apply it until you
want the run to start.

---

## Deliverables

| Deliverable | Where |
|---|---|
| Public solution repo (Docker + README) | this repository |
| Superset fork with the selected/remediated issues | https://github.com/e4c5/superset |
| The issues Ops Guard remediates | fork issues labelled `devin-autofix` |
| Resulting PRs | opened by Devin against the fork's default branch, linked from each `remediated` issue comment and `GET /runs/{issue}` |
| Loom walkthrough (≤5 min) | What (problem) / How (live demo + architecture) / Why Devin / When next |

### How an engineering leader knows it is working

`GET /metrics` → `remediated_total`, `needs_review_total`, `failed_total`,
`prs_opened_total`, `median_elapsed_seconds`, `acus_per_remediated_run`. One
approved issue becomes one PR with a measured wall-clock time and ACU cost;
success rate, throughput, and spend are the dashboard.
