# Devin-Ops Guard: Implementation Checklist

Companion to `Devin_Ops_Guard_FastAPI_Architecture_v2.md`. Ordered for a 2–3 hour build. Ship a working end-to-end demo before polishing anything.

**Status: code complete and verified live end-to-end (webhook → real Devin session → monitor → issue comments → /metrics) against org `org-d2a9a68f5bab400597bcc8c7e6e387a1`, repo `e4c5/superset`, tunnelled via localtunnel. Solution repo pushed public: https://github.com/e4c5/devin-superset. Remaining: author real issues, run each through to a Devin PR, record the Loom.**

**Success definition (2026-08-30): a run is `remediated` as soon as GitHub shows a PR Devin opened for the issue (target repo, default branch, body references `#<issue>`, open or merged) — checked on every poll, so a still-running or `waiting_for_user` session counts once its PR is up. Merge is not required.**

Confirmed API facts (code matches):
- Create response: `session_id` (not `id`), `url`, `status` starts at `new`.
- `status` enum: `new | claimed | running | exit | error | suspended | resuming`.
- `status_detail` enum includes `working | waiting_for_user | waiting_for_approval | finished` + billing/limit reasons (`out_of_quota`, `out_of_credits`, `payment_declined`, `*_usage_limit_exceeded`, …). A paused session stays `status: running` with `status_detail: waiting_for_user`.
- List sessions by tag: `GET .../sessions?tags=<tag>` → `{ "items": [...], "end_cursor", "has_next_page" }`.
- `repos: ["owner/name"]` — repo path strings; repo must be connected to the org Git connection.
- Repo listing (`GET /v3beta1/organizations/{org}/repositories`) requires a human-user token, not a `cog_` service credential.
- Service-user permissions come from its **role** in its own org — there is no per-permission toggle; a wrong `DEVIN_ORG_ID` surfaces as `403 Missing required permission`.

Legend: `[x]` done · `[~]` partially done / needs live credentials · `[ ]` not started.

---

## Phase 0 — Accounts & Access ✅

- [x] `DEVIN_ORG_ID` = `org-d2a9a68f5bab400597bcc8c7e6e387a1` (from `GET /v3/self`).
- [x] Service-user credential (`cog_…`, name `bada`); its role in that org grants session create + view. `GET /v3/self` returns `principal_type: service_user`.
- [x] `POST /v3/organizations/{org}/sessions` with `prompt` + `repos:["e4c5/superset"]` → 200, `session_id` + `url`.
- [x] `GET /v3/organizations/{org}/sessions/{id}` → `status`, `status_detail`, `acus_consumed`, `pull_requests`, `structured_output` all present.
- [x] `GET /v3/organizations/{org}/sessions?tags=…` → `{items:[…]}`.
- [x] `e4c5/superset` fork (of `apache/superset`, public, default branch `master`) connected to the Devin org Git connection.
- [x] `GITHUB_TOKEN` = `gh auth token` (account `e4c5`) — comments + PR reads.
- [x] `BYPASS_APPROVAL=true`.

## Phase 1 — Issues in the Fork (manual, by hand)

- [ ] Author 3–4 issues covering the required categories: dependency/CVE upgrade, code-quality/lint finding, small reproducible bug, missing test coverage.
- [ ] Each issue body includes: expected behavior, acceptance criteria, exact focused test command.
- [ ] Do **not** add the `devin-autofix` label yet (labeling is the live demo trigger).
- [x] Create the `devin-autofix` label in the repo. (`e4c5/superset`, color `5319e7`.)

## Phase 2 — Project Skeleton ✅

- [x] Repo layout: `app/` (FastAPI), `scripts/`, `data/` (gitignored), `Dockerfile`, `docker-compose.yml`, `.env.example`, `README.md`.
- [x] `docker-compose.yml`: one service, port 8000, `./data:/app/data` volume mount, `env_file: .env`.
- [x] `.env.example` with every var: `DEVIN_SERVICE_USER_TOKEN`, `DEVIN_ORG_ID`, `DEVIN_REPOSITORY_ID`, `DEVIN_API_BASE`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_TOKEN`, `TARGET_REPOSITORY`, `MAX_ACU_LIMIT`, `DEVIN_AUTOFIX_LABEL`, `BYPASS_APPROVAL`, `DATABASE_PATH`.
- [x] Config loader (`app/config.py`) reads env, fails fast on missing values (`ConfigError` at startup).

## Phase 3 — Persistence & Idempotency ✅

- [x] SQLite opened with WAL mode; writes use `BEGIN IMMEDIATE` (`app/db.py` `_writer()` context manager, module-level write lock, `busy_timeout=30000`).
- [x] `deliveries(delivery_id PK, received_at, repository_id, event, action)`.
- [x] `jobs(id PK, repository_id, issue_number, issue_url, title, issue_body, state, attempt, correlation_tag UNIQUE, session_id UNIQUE, session_url, created_at, started_at, completed_at, last_polled_at, status, status_detail, acus_consumed, pr_url, structured_output_json, error)`. Plus `job_events(job_id, at, kind, detail)` for the audit trail.
- [x] Partial unique index `one_active_job_per_issue` on `(repository_id, issue_number)` where `state IN ('queued','creating','running','creation_unknown')`.
- [x] `reserve_job()` — single `BEGIN IMMEDIATE` txn: dedupes delivery, checks for an active job, computes `attempt = max(attempt)+1`, inserts job + event; returns `RESERVED` / `DUPLICATE_DELIVERY` / `ALREADY_ACTIVE`.
- [x] `claim_queued_job()` — atomically moves one `queued` → `creating`.
- [x] State machine: `queued → creating → running → {remediated | needs_review | failed}`, plus `creation_unknown → running (reconciled) | needs_review` (`app/states.py`).

## Phase 4 — Webhook Gateway ✅

- [x] `POST /webhook/github`: reads **raw** body (`await request.body()`) before parsing.
- [x] Verify `X-Hub-Signature-256` (HMAC-SHA256, raw body, `GITHUB_WEBHOOK_SECRET`); `hmac.compare_digest`; 401 on failure (`app/webhook.py::verify_signature`).
- [x] Reject if `repository.full_name != TARGET_REPOSITORY` → 403.
- [x] Accept only `issues` event with action `opened` (label already present) or `labeled` (added label == `DEVIN_AUTOFIX_LABEL`). Everything else → 200 `ignored`.
- [x] Persist delivery + reserve job in one transaction; return fast (202 accepted / 200 duplicate / 200 already_active).
- [x] Duplicate `X-GitHub-Delivery` → 200, no session.
- [x] Worker picks up `queued` rows by poll (3s); no session work in the request path.

## Phase 5 — Session Worker ✅

- [x] `queued → creating` (with `correlation_tag`, already persisted at reserve time) happens **before** the outbound POST (`db.claim_queued_job`).
- [x] Build create payload (`app/devin.py::build_create_payload`): `prompt` (issue #, title, body, fork URL, "smallest correct fix, add focused tests, run stated command, open PR vs default branch, don't broaden scope"), `repos: [DEVIN_REPOSITORY_ID]`, `title`, `tags` (`ops-guard`, `ops-guard:issue:<n>:attempt:<k>`), `max_acu_limit`, `resumable: false`, `bypass_approval` (when `BYPASS_APPROVAL=true`), `structured_output_required: true`, `structured_output_schema` (outcome/summary/tests_run/pr_url/blocker, `additionalProperties:false`).
- [x] On success: save `session_id`, `session_url`, set `running`.
- [x] Deterministic 4xx (non-429) → `failed` + issue comment (session was not created).
- [x] Timeout / 5xx / 429 / uncertain → `creation_unknown`; `_reconcile()` lists org sessions by `correlation_tag` — exactly 1 → attach + `running`; 0 → `needs_review`; >1 → `needs_review`. Never blind-retries the POST.
- [x] Post GitHub issue comment: "Devin session started" + session URL + ACU limit.

## Phase 6 — Session Monitor ✅

- [x] Background loop: for each `running` job with a `session_id`, `GET /v3/organizations/{org}/sessions/{id}`.
- [x] Backoff 15s → 60s (×1.5 when idle, reset when work done); honor `429` `Retry-After`; `db.record_poll` persists every observed `status`/`status_detail`/`acus_consumed`/`pr_url`/`structured_output`.
- [x] Completion mapping (`app/monitor.py::_evaluate`), checked every poll in this order:
  - [x] **GitHub confirms a PR for the issue** (URL/base repo == target, default branch, body references `#<issue>`, open/merged) → `remediated` — regardless of session `status` (covers a still-running or `waiting_for_user` session whose PR is already up).
  - [x] `error` / `error` detail (and no such PR) → `failed`.
  - [x] `suspended` / billing-or-usage-limit detail (and no such PR) → `failed`.
  - [x] `waiting_for_user` / `waiting_for_approval`, no PR, within `WAITING_GRACE` (30 min from when the session first paused) → keep polling (stays `running`) + one-time "needs input" comment (retried until GitHub accepts it); waiting past the window → `needs_review`.
  - [x] still working (`new`/`claimed`/`running`/`resuming`) and no PR → keep polling.
  - [x] `exit` + `blocked`/`not_reproducible` → `needs_review` (with blocker text).
  - [x] `exit` without a verifiable PR → `needs_review` (conservative).
- [x] On terminal state: post issue comment (PR URL + tests + ACUs, or reason + Devin URL); set `completed_at`.
- [~] `waiting_for_user`: hook present (`_maybe_nudge`) but intentionally a no-op. Now lower stakes — if Devin has already opened the PR the run is `remediated` before the wait matters. Wire `POST .../sessions/{id}/messages` only if a demo issue needs an answer before the PR exists.

## Phase 7 — Observable Outputs ✅

- [x] `GET /health` — process + DB reachable.
- [x] `GET /runs` — all jobs, summarized (issue, title, state, status, session_url, pr_url, acus, timestamps).
- [x] `GET /runs/{issue_number}` — full audit record incl. `job_events` timeline, session id/URL, status, ACUs, structured output, error, verified PR URL.
- [x] `GET /metrics` — `active_runs`, `remediated_total`, `needs_review_total`, `failed_total`, `reconciled_total` (from `job_events`, `kind='reconciled'`), `prs_opened_total`, `median_elapsed_seconds`, `acus_per_remediated_run`, `total_jobs`. Computed locally from job records. Elapsed + ACU figures are snapshotted at PR-creation (the completion point), so they are a lower bound if Devin keeps working after opening the PR.
- [x] Structured JSON log line on every state transition + webhook decision (`app/logging_utils.py::event`).
- [x] `scripts/dashboard.py` — terminal table over `/metrics` + `/runs` (optional; REST is the source of truth). Uses stdlib only, not `rich`.

## Phase 8 — Local Verification (no tunnel) ✅

- [x] `scripts/emit_sample_webhook.py` — sends a correctly signed `issues.labeled` (or `opened`) fixture; `--delivery` to replay, `--bad-signature` to force 401.
- [x] `scripts/selftest.py` (21 checks, offline, no Devin/GitHub calls): bad signature → 401; wrong repo → 403; non-trigger action / non-`issues` event → 200 ignored; malformed/null payloads → 200 ignored (not 500); first delivery → 202; same delivery id → 200 `duplicate_delivery`; second delivery same issue → 200 `already_active`; exactly one job row; correlation tag `ops-guard:issue:10:attempt:1`; claim → `creating`; new delivery after terminal → 202 `attempt:2`; `/health`, `/metrics`, `/runs/{n}`, `/runs/999`→404.
- [x] `scripts/e2e_test.py` (11 scenarios / 22 checks): worker create → `running`, then monitor:
  - happy path (PR GitHub-confirmed) → `remediated`;
  - `waiting_for_user` **with** a confirmed PR → `remediated`;
  - `waiting_for_user`, no PR, within grace → stays `running`; past grace → `needs_review`;
  - `exit` claiming remediated but PR **not** confirmable by GitHub → `needs_review`;
  - PR wrong repo / wrong branch / no `#issue` reference → `needs_review`;
  - `blocked` → `needs_review`; `error` / `suspended` (billing) → `failed`.
- [x] Docker: `docker build` succeeds; container boots, `/health` ok, signed webhook → 202, job persisted, `/runs` + `/runs/{n}` served (verified via `docker exec` — rootless podman here doesn't publish host ports; a normal Docker daemon does).

## Phase 9 — Live Integration

- [x] App running (uvicorn :8000; `docker compose` also works) with real `.env`.
- [x] Tunnel: `localtunnel --port 8000` → `https://twenty-lizards-travel.loca.lt` (URL changes on restart).
- [x] Webhook on `e4c5/superset` (hook `671982928`, events = Issues) → tunnel; ping delivers 200.
- [x] Pipeline proven on `e4c5/diary#3`: webhook → `job_reserved` → real `session_created` → "session started" comment → monitor → `waiting_for_user` → `needs_review` + terminal comment → `/metrics` updated. (Ended in `waiting_for_user` only because the test issue had nothing to fix.)
- [x] API shapes verified live; `app/devin.py` (`items` envelope) and `app/monitor.py` (status/status_detail model, `new` state) fixed accordingly. Tests: selftest 21/21, e2e 20/20.
- [~] First real run done (`e4c5/superset#1` → session `0485fcd…` → PR `e4c5/superset#2` open). Ended `waiting_for_user` → `needs_review` under the *old* rules; under the new "PR created = remediated" rule this run qualifies. Re-trigger (attempt 2) or reconcile the stale job to show `/runs/1` = `remediated`.
- [ ] Author the remaining real Superset issues, label each → run through to a Devin PR → `/runs/{n}` = `remediated`.
- [ ] Remediate a second issue so throughput > 1 is real.

## Phase 10 — Demo Prep

- [ ] Pre-run one full remediation so a finished PR + `structured_output` is on screen within 5 min.
- [ ] Have a fresh unlabeled issue ready to trigger live.
- [ ] Terminal panes ready: `docker compose logs -f`, `watch curl -s localhost:8000/metrics | jq`, `python scripts/dashboard.py`.
- [ ] Devin session URL bookmarked to show the investigate→edit→test loop.
- [x] README: setup steps, env vars, `emit_sample_webhook.py` usage, architecture diagram, testing. *(Add the remediated-issue + PR links after Phase 9.)*
- [ ] Loom recorded (≤5 min): What / How / Why Devin / When next. No unsourced stats.

## Phase 11 — Deliverables

- [x] Public solution repo (Docker, README) — https://github.com/e4c5/devin-superset (public), `origin/main` == local `HEAD`.
- [ ] Public Superset fork with the issues and the merged/open PRs linked.
- [ ] Loom link (not an .mp4).

---

## Cut-if-time-runs-out (in this order)

1. `scripts/dashboard.py` — REST endpoints suffice. *(built; free to ignore)*
2. `waiting_for_user` auto-message — let it sit in `needs_review`. *(already the behavior)*
3. Second remediated issue — one clean end-to-end run is the bar.
4. `/runs` list endpoint — `/runs/{n}` + `/metrics` cover the demo. *(built; free to ignore)*

## Do NOT cut — all implemented, keep them working

- [x] HMAC verification, repo allowlist.
- [x] Delivery dedup + one-active-job-per-issue.
- [x] `creating`/`correlation_tag`-before-POST + `creation_unknown` reconciliation.
- [x] `max_acu_limit` on every session.
- [x] Monitor treating `exit` as "ended," not "succeeded" — a PR is verified against GitHub (repo, default branch, `#issue` reference, open/merged) before `remediated`, and that check is the sole success gate.

---

## Build artifacts (what exists on disk)

```
app/config.py      env loader, fail-fast
app/states.py      state constants + groupings
app/db.py          SQLite WAL, schema, reserve_job/claim/record_poll/finalize
app/webhook.py     signature verify + event evaluation
app/devin.py       v3 client + build_create_payload
app/github.py      issue comments + PR-state reads
app/worker.py      create session, creation_unknown reconciliation
app/monitor.py     poll loop + verified completion mapping
app/metrics.py     local metrics from job records
app/main.py        FastAPI app, lifespan starts worker+monitor, all endpoints
app/logging_utils.py  JSON event lines
scripts/emit_sample_webhook.py   signed fixture sender
scripts/selftest.py              19 offline checks
scripts/e2e_test.py              10 mocked worker+monitor checks
scripts/dashboard.py             terminal view
Dockerfile, docker-compose.yml, .env.example, requirements.txt, README.md
```

Run the tests: `python scripts/selftest.py && python scripts/e2e_test.py` (needs `pip install -r requirements.txt`).
