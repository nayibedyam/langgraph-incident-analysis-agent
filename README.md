# FL LangGraph Agent

A standalone, **IDE-free** multi-agent pipeline for IOS-XR defect (CDETS)
triage and test-coverage analysis. It is orchestrated with **LangGraph**, reasons
with an LLM (a gateway / Azure OpenAI **or** AWS Bedrock Claude), retrieves
historical duplicates with a local **TF-IDF RAG** index, is observable through
**Arize Phoenix** tracing + LLM-as-judge evals, and ships with a **FastAPI +
React** web console.

Given one or more CDETS IDs, the pipeline produces a defect schema, a quality
scorecard, a CaFy root-cause analysis, a generated test case, a coverage
comparison against existing automation, and (optionally) delivers the results to
MongoDB / TFTP / email / the CDETS record.

> This README is self-contained: copy the folder, follow the steps below, and the
> project runs end-to-end. See [src/docs/SPEC.md](src/docs/SPEC.md) for the full design spec.

---

## Table of Contents

1. [Architecture at a Glance](#1-architecture-at-a-glance)
2. [Prerequisites](#2-prerequisites)
3. [Install](#3-install)
4. [Configuration](#4-configuration)
5. [RAG — Historical Duplicate Retrieval](#5-rag--historical-duplicate-retrieval)
6. [Running the Pipeline (CLI)](#6-running-the-pipeline-cli)
7. [Phoenix — Tracing & LLM Evaluations](#7-phoenix--tracing--llm-evaluations)
8. [The Agents (Pipeline Stages)](#8-the-agents-pipeline-stages)
9. [Web Console — Backend & Frontend](#9-web-console--backend--frontend)
10. [Output Artifacts](#10-output-artifacts)
11. [Testing](#11-testing)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Architecture at a Glance

```
                                        ┌──────────────────────────────┐
   CLI (src/backend/cli)            ──► │                              │
   Web console (src/backend/api)    ──► │      LangGraph StateGraph     │ ──► artifacts in cdets_data/<ID>/
                                        │ (backend/pipeline/graph.py)   │ ──► MongoDB / TFTP / email
                                        └──────────────────────────────┘
                                          │        │
                            ┌─────────────┘        └─────────────┐
                            ▼                                     ▼
            LLM factory (backend/pipeline/llm.py)   RAG index (backend/pipeline/rag/)
            Gateway/Azure OR Bedrock Claude         local TF-IDF over bug-list CSV

   Observability: Arize Phoenix tracing + evals (src/eval/)
```

```
common_infra → prescan → rag_fetch_related_cdets
                              │  (high-confidence duplicate? → short-circuit → delivery)
                              ▼
                       cdets_tz_analyzer → cdets_scoring
                                                │
              ┌──── low score + HITL enabled ───┴──── normal ────┐
              ▼                                                   ▼
     missing_info_request                                 cafy_rca_analyzer
              │                                                   │
       human_review (interrupt)             ┌──── testcase_generator ──┐ (LLM, fan-out)
              │                              │                          ▼
     merge_human_input ─► cdets_tz_analyzer  └─ existing_test_scanner ─► merge_coverage
                                                  (Python, fan-out)        │
                                                                           ▼
                                                                 coverage_comparison
                                                                           │
                                                                           ▼
                                                              email_report_generator → delivery → END
```

**Tech stack:** LangGraph · LangChain · Azure OpenAI (gateway) / AWS Bedrock
· scikit-learn (TF-IDF RAG) · Arize Phoenix · FastAPI · Vite + React + TypeScript
· MongoDB (pymongo).

---

## 2. Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.11+** (3.12/3.13 recommended) | The system `python3` on some hosts is 3.6 and will **not** work. Always use a virtualenv. |
| **Node.js 18+** & npm | Only needed for the web console frontend. |
| **An LLM credential** | Either an LLM gateway / Azure OpenAI access token **or** AWS Bedrock credentials (see [Configuration](#4-configuration)). |
| Internal tools (optional) | `dumpcr`, `cbugval`, CaFy AP repo, blueprints — needed for *live* CDETS fetch and CaFy coverage. The pipeline degrades gracefully (dry-run / `--dry-run`) when these are absent. |
| MongoDB (optional) | Only for delivery / dashboard. Skipped in `--dry-run`. |

The repo ships with `cdets_data/` containing pre-computed sample defects so you
can browse the console and inspect artifacts without any backend access.

---

## 3. Install

### 3.1 Python environment

```bash
cd fl-langgraph-agent

# Create an isolated environment (use python3.12/3.13, NOT system 3.6)
python3.12 -m venv .venv
source .venv/bin/activate

# Install all Python dependencies (pipeline + RAG + Phoenix + web backend)
pip install -r requirements.txt
```

`requirements.txt` installs everything: LangGraph/LangChain, the Azure + Bedrock
providers, scikit-learn/scipy/joblib (RAG), arize-phoenix +
openinference-instrumentation-langchain (observability), FastAPI/uvicorn (web
backend), and pytest (tst).

> **uv users:** if `.venv` was created by `uv` and has no `pip`, install with
> `VIRTUAL_ENV=$PWD/.venv uv pip install -r requirements.txt` and run python via
> `./.venv/bin/python`.

### 3.2 Frontend (web console) dependencies

```bash
cd src/frontend
npm install
```

This installs React, Vite, and TypeScript tooling (see
[src/frontend/package.json](src/frontend/package.json)).

---

## 4. Configuration

Configuration comes from two layers:

1. **`.env`** — secrets and environment-specific values (never commit it).
2. **[src/backend/config/config.yaml](src/backend/config/config.yaml)** — pipeline behavior. Values use
   `${VAR:-default}` placeholders that read from the environment, so most users
   only edit `.env`.

### 4.1 Create your `.env`

```bash
cp .env.example .env
# edit .env
```

Pick **one** LLM provider:

**Option A — LLM gateway / Azure OpenAI (default)**

```bash
AZURE_OPENAI_ENDPOINT=https://your-llm-gateway.example.com
AZURE_OPENAI_API_KEY=your-access-token   # OR use OAuth client creds below
AZURE_OPENAI_DEPLOYMENT=gpt-5-nano
GATEWAY_APP_KEY=your-app-key
GATEWAY_USER_ID=your-gateway-user-id
```

The LLM factory ([src/backend/pipeline/llm.py](src/backend/pipeline/llm.py)) can also mint a gateway token
automatically via OAuth client-credentials if you set `OAUTH_CLIENT_ID`,
`OAUTH_CLIENT_SECRET`, and `OAUTH_TOKEN_URL` — this avoids recurring
`401 TokenExpired` errors from a stale static key.

**Option B — AWS Bedrock (Claude)**

```bash
FL_LLM_PROVIDER=bedrock
AWS_REGION=us-west-2
# plus standard AWS credentials (env vars, profile, or instance role)
```

Bedrock defaults to Claude Haiku 4.5 (fast); per-stage overrides and `sonnet` /
`opus` aliases live under `llm.bedrock` in [src/backend/config/config.yaml](src/backend/config/config.yaml).

### 4.2 Key `src/backend/config/config.yaml` sections

| Section | What it controls |
|---|---|
| `llm` / `azure_openai` | Provider selection, per-stage model routing, temperature, timeouts. |
| `rag` | Corpus CSV path, index dir, `top_k`, and `high_match_threshold` (duplicate short-circuit). |
| `cafy` / `blueprints` | Paths to the CaFy AP repo and blueprint corpus. |
| `mongodb` / `tftp` / `email` | Delivery targets (skipped in `--dry-run`). |
| `human_in_loop` | Low-score HITL detour (console-only). |
| `paths` | Artifact base dir, AP CSV / index, schema template. |

---

## 5. RAG — Historical Duplicate Retrieval

### 5.1 What it does

Right after `prescan`, the `rag_fetch_related_cdets` agent retrieves the **top-k
most similar historical CDETS** for the bug under analysis. If the best match
similarity is `>= rag.high_match_threshold` (default **0.45**), the bug is treated
as a likely duplicate and the graph **short-circuits straight to delivery**,
emitting the related CDETS and skipping the expensive schema/scoring/RCA/testcase
stages. Otherwise the normal pipeline continues. The matches are always written to
`cdets_data/<ID>/<ID>_related_cdets.json`.

### 5.2 Why TF-IDF (not embeddings)

The gateway appkey used here is scoped to `gpt-5-nano` **chat** only — the
embedding deployments return `401`. So retrieval uses a **local TF-IDF vector
space** (scikit-learn) instead of a hosted embedding model. No external service is
required to build or query the index.

### 5.3 Data source

The corpus is a historical bug-list CSV. The default path (overridable via
`FL_RAG_CORPUS_CSV` or `rag.corpus_csv` in [src/backend/config/config.yaml](src/backend/config/config.yaml)) is:

```
/path/to/bug_list.csv
```

- **ID column:** `Identifier` (`rag.id_column`)
- **Text columns** indexed: `description`, `eng_notes`, `scrub_notes`,
  `release_notes`, `cfd_analysis`, `regression_analysis` (`rag.text_columns`)

To use your own corpus, point `--csv` (or `FL_RAG_CORPUS_CSV`) at any CSV that has
an identifier column plus one or more text columns, and adjust `rag.text_columns`.

### 5.4 Build the index

```bash
# Uses paths from src/backend/config/config.yaml by default
python src/backend/cli/build_rag_index.py build

# Or with explicit paths
python src/backend/cli/build_rag_index.py build \
    --csv /path/to/1_bug_list_updated.csv \
    --index-dir cdets_data/rag_index
```

This writes the TF-IDF matrix + vectorizer to `cdets_data/rag_index/`
(`matrix.npz`, vectorizer, and id map). A pre-built sample index is already
present under `cdets_data/rag_index/`.

### 5.5 Query the index manually

```bash
# By CDETS id (auto-fetches fields via dumpcr if the id is not in the corpus)
python src/backend/cli/build_rag_index.py query --cdets-id CSCwr73685

# By free text
python src/backend/cli/build_rag_index.py query --text "GRE IPsec decap traffic drop on NCS5700"

# JSON output, top-3
python src/backend/cli/build_rag_index.py query --cdets-id CSCwr73685 -k 3 --json
```

The retrieval code lives in [src/backend/pipeline/rag/](src/backend/pipeline/rag): `corpus.py`
(CSV → documents), `index.py` (build/persist), `retriever.py` (load + top-k
query).

---

## 6. Running the Pipeline (CLI)

```bash
source .venv/bin/activate

# Single defect
python src/backend/cli/run_fl_pipeline.py CSCwk35275

# Multiple defects, 3 at a time
python src/backend/cli/run_fl_pipeline.py CSCwk35275 CSCwk35276 --parallel 3

# From a file (one CDETS ID per line; # comments allowed)
python src/backend/cli/run_fl_pipeline.py --bugs-file bugs.txt --parallel 5

# Dry-run: skip MongoDB / TFTP / email side effects (still writes local artifacts)
python src/backend/cli/run_fl_pipeline.py CSCwk35275 --dry-run

# With Phoenix tracing
python src/backend/cli/run_fl_pipeline.py CSCwk35275 --trace
```

| Flag | Purpose |
|---|---|
| `cdets_ids ...` | One or more CDETS IDs (`CSCxx99999`). |
| `--bugs-file FILE` | Read IDs from a file. |
| `--parallel N` | Max concurrent pipeline instances (default 1). |
| `--dry-run` | Skip all external delivery side effects. |
| `--config PATH` | Use a non-default `src/backend/config/config.yaml`. |
| `--output-summary PATH` | Write the batch summary JSON to a file. |
| `--trace` | Enable Phoenix tracing (or set `PHOENIX_TRACING=1`). |
| `-v / -vv` | Increase logging verbosity. |

Entry flow: `src/backend/cli/run_fl_pipeline.py` → `backend.pipeline.batch.run_batch` → `build_graph()`
([src/backend/pipeline/graph.py](src/backend/pipeline/graph.py)).

---

## 7. Phoenix — Tracing & LLM Evaluations

[Arize Phoenix](https://github.com/Arize-ai/phoenix) gives full observability:
**tracing** records *what happened* (every LangGraph node + LLM call as an
OpenTelemetry span); **evals** score *how good it was* (LLM-as-judge verdicts
written back as span annotations).

Phoenix is already in `requirements.txt` — no separate account or install needed.

### 7.1 Tracing

```bash
# 1. Start a local Phoenix server (UI on http://localhost:6006, OTLP gRPC :4317)
./.venv/bin/phoenix serve

# 2. Run the pipeline with tracing on
python src/backend/cli/run_fl_pipeline.py CSCwk35275 --trace
#    ...or set it once in .env so every CLI/console run traces:
#    PHOENIX_TRACING=1
#    PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006
```

[src/eval/tracing.py](src/eval/tracing.py) (`setup_phoenix_tracing()`) instruments
LangChain via `openinference-instrumentation-langchain`. The web console backend
auto-enables tracing on startup when `PHOENIX_TRACING` is set. Setting
`PHOENIX_COLLECTOR_ENDPOINT` exports spans to that collector instead of launching
an embedded UI.

> **NFS warning:** Phoenix stores its data in a local SQLite DB. SQLite **corrupts
> on NFS** (`file is not a database`). Keep Phoenix data on local disk via
> `PHOENIX_WORKING_DIR=/local/disk/.phoenix` in `.env`.

### 7.2 LLM-as-judge evaluations

Tracing alone shows **zero annotations** — that is expected. Annotations are
produced by an eval pass that pulls LLM spans, judges them with the project's own
LLM factory (reusing your gateway/Bedrock auth — no extra credentials), and logs
the verdict back to each span.

```bash
# Requires: Phoenix running + at least one traced pipeline run
FL_LLM_PROVIDER=bedrock ./.venv/bin/python src/eval/run_phoenix_evals.py --eval quality

# Other dimensions
python src/eval/run_phoenix_evals.py --eval coherence --hours 6 --limit 50
python src/eval/run_phoenix_evals.py --eval hallucination
```

| Flag | Purpose |
|---|---|
| `--eval {quality,coherence,hallucination}` | Which judge dimension to score. |
| `--project` | Phoenix project name (default `fl-langgraph-agent`). |
| `--hours N` | Only evaluate spans from the last N hours. |
| `--limit N` | Max spans to pull. |
| `--judge-stage` | Which configured model stage acts as judge (default `scoring`). |
| `--endpoint` | Phoenix base URL (default `PHOENIX_COLLECTOR_ENDPOINT`). |

Eval logic and judge prompts live in [pipeline/evals.py](pipeline/evals.py). View
results in the Phoenix UI under each span's **Annotations**.

---

## 8. The Agents (Pipeline Stages)

Each stage is a LangGraph node in [src/backend/pipeline/nodes/](src/backend/pipeline/nodes). LLM agents run
a tool-calling ReAct loop ([src/backend/pipeline/agent_loop.py](src/backend/pipeline/agent_loop.py)); pure
Python nodes do deterministic work. All nodes read the shared `FLAgentState`
([pipeline/state.py](pipeline/state.py)) and return only the fields they own.

| # | Node | Type | Role |
|---|---|---|---|
| 0 | `common_infra` | Python | Validate the `CSC[a-z]{2}\d{5}` ID, create `cdets_data/<ID>/`, write a trace index. Invalid ID → abort. |
| 1 | `prescan` | Python | `dumpcr` fetch + parse CDETS fields; Component→AP/SubAP CSV lookup; resolve blueprint dir, topology, version, severity; run CaFy coverage prescan. |
| 2 | `rag_fetch_related_cdets` | Python (TF-IDF) | Retrieve top-k similar historical CDETS. High-confidence match → short-circuit to `delivery`. Writes `<ID>_related_cdets.json`. |
| 3 | `cdets_tz_analyzer` | LLM agent | Map CDETS (+ optional TechZone) fields into the defect schema. Produces `<ID>_Cdets_Schema_Template.json` (and optional TZ/Union schemas). Lookup failure → abort. |
| 4 | `cdets_scoring` | LLM agent | Apply the v3 quality formula → `<ID>-Scorecard.md`, `cdet_ai_score`, `ai_confidence`, `automation_readiness`, quality blockers. |
| 5 | `cafy_rca_analyzer` | LLM agent | Resolve AP/SubAP, analyze CaFy coverage, produce root-cause analysis (`<ID>_cafy_rca.json`, `AI-FL-<ID>_cafy_rca.md`), coverage gap classification. |
| 6a | `testcase_generator` | LLM agent (fan-out) | Generate a structured test case (`AI-FL-<ID>_TestCase.md`) from schema + RCA + blueprints. Most token-heavy stage. |
| 6b | `existing_test_scanner` | Python (fan-out) | Scan CaFy `*_ap.py` sources; regex-extract existing `test_*`, `verify_*`, `helper_*` methods (excludes xfail/flaky/deprecated). |
| 6c | `merge_coverage` | Python (join) | Combine the generated test case + existing-test inventory into one comparison-ready dict. Waits for both fan-out branches. |
| 7 | `coverage_comparison` | LLM agent | Score 6 weighted coverage dimensions (Observable 30%, Trigger 20%, Topology 15%, Platform 15%, Config/Scale 10%, Framework 10%) → confidence + grade. |
| 8 | `email_report_generator` | Python + light LLM | Render Jinja2 email templates ([pipeline/templates/](pipeline/templates)); assemble payload, subject, attachments. |
| 9 | `delivery` | Python (terminal) | MongoDB upsert, TFTP copy, send email, attach to CDETS, post performance metrics. All non-fatal; skipped under `--dry-run`. |
| — | `missing_info_request` / `human_review` / `merge_human_input` | Python (HITL) | Console-only detour when `cdet_ai_score < human_in_loop.score_threshold`: requests missing info, pauses the run for a reviewer, merges the human input, then re-analyzes. |
| — | `abort` | Python | Terminal error node for invalid ID / failed CDETS lookup / missing schema. |

**Fan-out parallelism:** after `cafy_rca_analyzer`, `testcase_generator` (LLM) and
`existing_test_scanner` (Python) run concurrently and rejoin at `merge_coverage` —
they write disjoint state fields, so there is no conflict.

**LLM provider & per-stage routing:** all agents resolve their model through
[src/backend/pipeline/llm.py](src/backend/pipeline/llm.py) using the `llm` / `azure_openai` config, so you
can route individual stages to different models (e.g. Sonnet for `testcase`, Haiku
elsewhere) without touching node code.

System prompts for each LLM agent live in [src/backend/pipeline/prompts/](src/backend/pipeline/prompts);
their tools live in [src/backend/pipeline/tools/](src/backend/pipeline/tools).

---

## 9. Web Console — Backend & Frontend

The console lets you browse analyzed defects, inspect every artifact, trigger new
analyses, watch each agent run in real time, do human-in-the-loop review, and
triage by priority. The UI lives in [src/frontend/](src/frontend) and the API in [src/backend/api/](src/backend/api).

### 9.1 Backend (FastAPI)

[src/backend/api/server.py](src/backend/api/server.py) reads per-CDETS
artifacts from `cdets_data/<ID>/` directly off disk and drives the pipeline for
new runs. Start it from the **repo root** with `--app-dir src` (so `backend` and
`eval` import):

```bash
source .venv/bin/activate
cd fl-langgraph-agent
.venv/bin/python -m uvicorn backend.api.server:app --app-dir src --port 8800 --reload
```

Set `FL_ARTIFACTS_DIR` to point at a different artifact root (defaults to
`cdets_data/`). If `PHOENIX_TRACING` is set, the backend auto-enables tracing on
startup.

Key endpoints (all under `/api`):

| Method & path | Purpose |
|---|---|
| `GET /api/health` | Liveness probe. |
| `GET /api/stats` | Aggregate overview metrics. |
| `GET /api/artifacts` | List analyzed defects (summaries + priority). |
| `GET /api/artifacts/{id}` | One defect: summary + file list. |
| `GET /api/artifacts/{id}/file?name=` | Raw content of one artifact file. |
| `GET /api/artifacts/{id}/similar` | RAG-related CDETS for a defect. |
| `GET /api/artifacts/{id}/summary` | Parsed run summary. |
| `POST /api/artifacts/analyze` | Run the deterministic prescan for a CDETS ID. |
| `POST /api/jobs` | Start a full pipeline run (returns a `job_id`). |
| `GET /api/jobs/{id}/events` | Server-Sent Events stream of per-node progress. |
| `GET /api/jobs/{id}/review` / `POST /api/jobs/{id}/resume` | HITL review fetch / resume. |
| `POST /api/artifacts/{id}/chat` | Chat over a defect's artifacts. |
| `POST /api/triage/log` | Log a triage decision. |

Job orchestration and the live event stream are in
[src/backend/api/jobs.py](src/backend/api/jobs.py) and
[src/backend/api/runner.py](src/backend/api/runner.py).

### 9.2 Frontend (Vite + React + TypeScript)

[src/frontend/](src/frontend) is a Vite SPA. Dev server runs on port **3100**
and proxies `/api` to the backend.

```bash
cd src/frontend
npm install            # first time only

# Backend on default 127.0.0.1:8800
npm run dev

# Backend on a different port/host
VITE_API_TARGET=http://localhost:8800 npm run dev
```

Then open **http://localhost:3100**.

| Build script | Purpose |
|---|---|
| `npm run dev` | Start the dev server (HMR) on :3100. |
| `npm run build` | Type-check + production build to `dist/`. |
| `npm run preview` | Serve the production build on :3100. |
| `npm run typecheck` | TypeScript-only check. |

Main views (in [src/frontend/src/components/](src/frontend/src/components)):
`OverviewView` (metrics), `DefectsView` (browse + file viewer), `TriageView`
(priority triage), `AnalyzeProgress` / `AgentPanels` / `RunSummaryPanel` (live
per-agent run streaming), and `HumanReviewForm` / `ReviewLanding` (HITL). The API
client is [src/frontend/src/api.ts](src/frontend/src/api.ts); shared types are
in [src/frontend/src/types.ts](src/frontend/src/types.ts).

### 9.3 Full local stack (typical workflow)

```bash
# Terminal 1 — Phoenix (optional, for tracing)
./.venv/bin/phoenix serve

# Terminal 2 — backend
source .venv/bin/activate
.venv/bin/python -m uvicorn backend.api.server:app --app-dir src --port 8800 --reload

# Terminal 3 — frontend
cd src/frontend && VITE_API_TARGET=http://localhost:8800 npm run dev
# open http://localhost:3100
```

---

## 10. Output Artifacts

For each `CSCxxNNNNN` defect the pipeline writes to `cdets_data/<ID>/`:

| File | Produced by |
|---|---|
| `<ID>_Cdets_Schema_Template.json` | `cdets_tz_analyzer` |
| `<ID>_TZ_Schema_Template.json` (optional) | `cdets_tz_analyzer` |
| `<ID>_Union_Schema_Template.json` (optional) | `cdets_tz_analyzer` |
| `<ID>-Scorecard.md` | `cdets_scoring` |
| `<ID>_cafy_rca.json` | `cafy_rca_analyzer` |
| `AI-FL-<ID>_cafy_rca.md` | `cafy_rca_analyzer` |
| `AI-FL-<ID>_TestCase.md` | `testcase_generator` |
| `<ID>_related_cdets.json` | `rag_fetch_related_cdets` |
| `<ID>_trace_index.json` (debug) | `common_infra` |

These share the same artifact contract as the IDE-mode FL Agent — drop-in
compatible with the existing dashboard, MongoDB schema, and email templates.

---

## 11. Testing

```bash
source .venv/bin/activate
pytest tst/                       # all tests
pytest tst/test_graph_routing.py -v
```

Tests live in [tst/](tst) and cover graph routing, prescan, the RAG node,
blueprints, and utilities. Config is in [tst/pytest.ini](tst/pytest.ini)
(`asyncio_mode = auto`).

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| `python: command not found` / langgraph import errors | You are on system Python 3.6. Activate `.venv` (Python 3.11+). |
| `401 TokenExpired` from the LLM | Use OAuth client-credentials (`OAUTH_CLIENT_ID/SECRET/TOKEN_URL`) instead of a static `AZURE_OPENAI_API_KEY`; the factory auto-refreshes the token. |
| Phoenix: `file is not a database` / GraphQL project errors | Phoenix SQLite on NFS is corrupting — set `PHOENIX_WORKING_DIR` to local disk. |
| `src/eval/run_phoenix_evals.py` reports zero annotations | Run the pipeline with tracing first, and make sure Phoenix is running at `PHOENIX_COLLECTOR_ENDPOINT`. |
| `existing_test_scanner` skipped / empty | `FL_CAFY_AP_ROOT` (CaFy repo) is not mounted — benign; coverage uses partial data. |
| RAG returns nothing / index error | Build the index first: `python src/backend/cli/build_rag_index.py build`. |
| `npm install` fails with quota/`errno -122` | Point npm cache to a non-quota disk: `npm config set cache /local/disk/.npm`. |
| Frontend can't reach the API | Start the dev server with `VITE_API_TARGET=http://localhost:<backend-port>`. |

---

For the complete design rationale, shared-state contract, tool definitions, and
routing logic, see [src/docs/SPEC.md](src/docs/SPEC.md).
