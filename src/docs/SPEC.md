# FL LangGraph Multi-Agent Pipeline — Specification

> **Version:** 0.1.0-draft  
> **Status:** SPEC PHASE — No implementation until spec is reviewed and approved.  
> **LLM Provider:** Azure OpenAI via `langchain_openai.AzureChatOpenAI`  
> **Orchestrator:** LangGraph (`langgraph`)  
> **Purpose:** Replace IDE-dependent FL Agent with a standalone multi-agent pipeline.

---

## 1. Problem Statement

The current IOS-XR Feedback Loop Agent requires Cursor or VS Code to orchestrate
defect analysis. The IDE loads `AGENTS.md` + stage `SKILL.md` files into a chat
context, and the LLM reasons through the pipeline interactively.

**This project removes the IDE dependency entirely.** The same 9-stage pipeline
runs as a standalone Python program orchestrated by LangGraph, with each stage
implemented as a specialist agent node backed by Azure OpenAI.

---

## 2. Goals

| #   | Goal                                                                              |
| --- | --------------------------------------------------------------------------------- |
| G1  | Given one or more CDETS IDs, produce identical artifacts to the IDE-mode FL Agent |
| G2  | No dependency on Cursor, VS Code, Codex CLI, or any IDE                           |
| G3  | Single-defect and batch-parallel execution from CLI                               |
| G4  | Same artifact contract (filenames, content structure, delivery targets)           |
| G5  | Modular — each agent node independently testable                                  |
| G6  | Observable — per-stage traces, token usage, timing                                |

---

## 3. Non-Goals (v0.1)

- Autonomous polling mode (poller → queue → worker). Deferred to v0.2.
- UI/dashboard changes. Existing dashboard consumes MongoDB docs unchanged.
- Replacing the scoring formula or testcase format. Lossless port only.
- Supporting multiple LLM providers simultaneously. Azure OpenAI only.

---

## 4. Architecture Overview

### 4.1 Technology Stack

| Layer         | Technology                                       |
| ------------- | ------------------------------------------------ |
| Orchestration | `langgraph` (StateGraph)                         |
| LLM           | `langchain_openai.AzureChatOpenAI`               |
| Messages      | `langchain.schema.HumanMessage`, `SystemMessage` |
| State         | Python `TypedDict` (LangGraph shared state)      |
| Tools         | LangChain `@tool` decorator + `ToolMessage`      |
| Async         | `asyncio` for batch parallelism                  |
| Storage       | `pymongo` (MongoDB), filesystem (TFTP)           |
| Email         | `sendmail` via subprocess / SMTP                 |
| Config        | YAML (`src/backend/config/config.yaml`) + environment variables     |

### 4.2 Graph Topology

```
┌───────────────────────────────────────────────────────────────────────────┐
│                              LANGGRAPH                                     │
│                                                                             │
│  [START]                                                                    │
│     │                                                                       │
│     ▼                                                                       │
│  ┌──────────────┐    ┌─────────┐                                           │
│  │ common_infra │───▶│ prescan │                                           │
│  └──────────────┘    └────┬────┘                                           │
│         │ invalid         │                                                 │
│         ▼                 ▼                                                 │
│     [ABORT]     ┌────────────────────────┐                                 │
│                 │ rag_fetch_related_cdets │──── high match ──┐             │
│                 └───────────┬────────────┘   (skip pipeline)  │             │
│                             │ no/low match                    │             │
│                             ▼                                  │             │
│                 ┌──────────────────┐                          │             │
│                 │ cdets_tz_analyzer │──── FAIL ───▶ [ABORT]    │             │
│                 └────────┬─────────┘                          │             │
│                          ▼                                    │             │
│                 ┌────────────────┐                            │             │
│                 │ cdets_scoring  │                            │             │
│                 └────────┬───────┘                            │             │
│                          ▼                                    │             │
│                 ┌──────────────────┐                          │             │
│                 │ cafy_rca_analyzer│                          │             │
│                 └────────┬─────────┘                          │             │
│                          │                                    │             │
│                    ┌─────┴──────┐    ◄── FAN-OUT (parallel)   │             │
│                    ▼            ▼                             │             │
│         ┌───────────────┐  ┌──────────────────────┐          │             │
│         │testcase_gen   │  │existing_test_scanner  │          │             │
│         │  (LLM agent)  │  │  (pure Python scan)   │          │             │
│         └───────┬───────┘  └──────────┬───────────┘          │             │
│                 │                      │                      │             │
│                 └──────┬───────────────┘    ◄── JOIN          │             │
│                        ▼                                      │             │
│               ┌──────────────────┐                           │             │
│               │ merge_coverage   │   (pure Python — merge)    │             │
│               └────────┬─────────┘                           │             │
│                        ▼                                      │             │
│               ┌──────────────────────────┐                   │             │
│               │ coverage_comparison      │   (LLM)            │             │
│               └────────┬─────────────────┘                   │             │
│                        ▼                                      │             │
│               ┌──────────────────────┐                       │             │
│               │ email_report_generator│                       │             │
│               └────────┬─────────────┘                       │             │
│                        ▼                                      │             │
│               ┌──────────┐  ◄───── high-match short-circuit ──┘             │
│               │ delivery │                                                 │
│               └─────┬────┘                                                 │
│                     │                                                       │
│                     ▼                                                       │
│                   [END]                                                     │
│                                                                             │
└───────────────────────────────────────────────────────────────────────────┘
```

### 4.2.1 RAG Duplicate Short-Circuit

`rag_fetch_related_cdets` runs immediately after `prescan`. It queries a local
TF-IDF index built from the historical bug-list corpus
(`1_bug_list_updated.csv`) and retrieves the top-k (default 5) most similar
CDETS. The matches are always attached to the state (`related_cdets`) and
written to `<artifact_dir>/<ID>_related_cdets.json`.

If the **best** match similarity is `>= rag.high_match_threshold` (default
0.45), the input bug is treated as a likely duplicate of an already-analysed
defect: the node sets `rag_short_circuit=True` and the graph routes **directly
to `delivery`**, emitting the top-k related CDETS as the result and skipping
the expensive schema / scoring / RCA / testcase / coverage stages. Otherwise
the normal analysis pipeline continues.

The gateway appkey used by this project is scoped to `gpt-5-nano` chat
only — the embedding deployments return 401 — so retrieval uses a local
TF-IDF vector space rather than a hosted embedding model. The index is built
offline via `python src/backend/cli/build_rag_index.py build`.

### 4.4 Fan-Out Pattern Detail

After `cafy_rca_analyzer` completes, two independent branches execute **in parallel**:

| Branch | Type | Time | Reads from state | Writes to state |
|---|---|---|---|---|
| `testcase_gen` | LLM agent (~30s) | Dominant | `cdets_schema_path`, `cafy_rca_json_path`, `blueprint_dir`, `topology` | `testcase_path`, `test_scenarios` |
| `existing_test_scanner` | Pure Python (~5s) | Fast | `primary_ap`, `primary_subap`, `blueprint_dir` | `existing_tests`, `existing_verifiers`, `existing_helpers`, `test_file_map` |

**No state conflicts** — each branch writes to different fields. LangGraph automatically waits for both branches to complete before running `merge_coverage`.

**Why fan-out here:** The testcase generator doesn't need the list of existing test methods to generate new test scenarios. The existing test scanner doesn't need the generated testcase. Both only need the upstream CaFy RCA output. Running them sequentially wastes ~5s per defect; in batch mode with 100 defects, that's 8+ minutes saved.

**Adding more parallel branches later** requires only `add_edge` calls:
```python
# Future: add allergen/platform validation in parallel
g.add_edge("cafy_rca", "platform_validator")
g.add_edge("platform_validator", "merge_coverage")
```

### 4.3 Batch Parallelism

```
CLI: python src/backend/cli/run_fl_pipeline.py CSCxx001 CSCxx002 CSCxx003 --parallel 5

                    ┌─── Graph Instance 1 (CSCxx001) ───┐
                    │                                     │
asyncio.gather ─────┼─── Graph Instance 2 (CSCxx002) ───┼──▶ Summary
                    │                                     │
                    └─── Graph Instance 3 (CSCxx003) ───┘

Each instance: independent state, independent artifacts, rate-limited by semaphore.
```

---

## 5. Shared State Contract

```python
from typing import TypedDict, Optional, Annotated
import operator

class FLAgentState(TypedDict):
    # ═══ Input (set once at invocation) ═══
    cdets_id: str                          # CSCxx99999
    invocation_mode: str                   # LANGGRAPH | LANGGRAPH_BATCH
    config: dict                           # Loaded from src/backend/config/config.yaml

    # ═══ Stage 00: common_infra ═══
    artifact_dir: str                      # Absolute path to cdets_data/<ID>/
    init_valid: bool
    error: Optional[str]

    # ═══ Prescan (pre-graph enrichment) ═══
    cdets_fields: dict                     # Raw dumpcr structured fields
    component: str
    primary_ap: str
    primary_subap: str
    blueprint_dir: Optional[str]
    topology: str                          # PP | F3 | F6
    version: str
    severity: str
    dtpt_manager: str
    prescan_coverage: Optional[dict]       # analyze_cafy_coverage.py output

    # ═══ RAG: related-CDETS retrieval (runs next to prescan) ═══
    related_cdets: list                    # Top-k {identifier, score, snippet}
    related_cdets_path: Optional[str]      # <ID>_related_cdets.json artifact
    rag_top_score: float                   # Best match similarity (0..1)
    rag_short_circuit: bool                # True → skip pipeline, deliver matches

    # ═══ Stage 01: cdets_tz_analyzer ═══
    cdets_schema_path: Optional[str]
    tz_schema_path: Optional[str]
    union_schema_path: Optional[str]
    cdets_lookup_ok: bool
    has_techzone: bool
    schema_data: dict                      # Parsed schema content

    # ═══ Stage 02: scoring ═══
    scorecard_path: Optional[str]
    cdet_ai_score: float
    ai_confidence: float
    automation_readiness: str              # HIGH | MEDIUM | LOW
    quality_blockers: list

    # ═══ Stage 03: cafy_rca ═══
    cafy_rca_json_path: Optional[str]
    cafy_rca_md_path: Optional[str]
    automation_mapping: Optional[dict]
    genc_handoff: Optional[dict]
    coverage_gap: str                      # FULL_COVERAGE_EXISTS | ... | NEW_TEST_REQUIRED
    gap_classification: str
    cafy_coverage_verdict: str

    # ═══ Stage 04a: testcase_generator (fan-out branch 1) ═══
    testcase_path: Optional[str]
    test_scenarios: list

    # ═══ Stage 04b: existing_test_scanner (fan-out branch 2) ═══
    existing_tests: list                   # All test method names from CaFy AP sources
    existing_verifiers: list               # All verify_* methods
    existing_helpers: list                 # All helper_* methods
    test_file_map: dict                    # {filename: {class, methods, verifiers, helpers}}

    # ═══ Stage 04c: merge_coverage (join point) ═══
    merged_coverage_input: dict            # Combined testcase + existing test data for comparison

    # ═══ Stage 05: coverage_comparison ═══
    test_coverage_confidence: float
    test_coverage_grade: str               # Fully Covered | Covered-Needs Review | Partial | No Coverage
    coverage_classification: str

    # ═══ Stage 06: email_report ═══
    email_payload: Optional[dict]
    email_subject: str
    attachment_paths: list

    # ═══ Stage 07: delivery ═══
    mongo_pushed: bool
    tftp_delivered: bool
    email_sent: bool
    cdets_attached: bool
    delivery_status: str

    # ═══ Pipeline metadata ═══
    stage_traces: dict                     # {stage_id: {start, end, tokens, status}}
    messages: Annotated[list, operator.add] # LangGraph message list accumulator
```

### State Mutation Rules

1. Each node receives the **full state** and returns a **partial dict** (only fields it sets).
2. LangGraph merges the partial dict into the state before the next node.
3. A node must **never** overwrite fields owned by another stage.
4. Large artifacts live on disk — state carries **paths**, not content.

---

## 6. Node Specifications

### 6.0 `common_infra` — Validate & Initialize

| Property     | Value                                                                            |
| ------------ | -------------------------------------------------------------------------------- |
| **Type**     | Pure Python (no LLM)                                                             |
| **Input**    | `cdets_id`                                                                       |
| **Output**   | `artifact_dir`, `init_valid`, `error`                                            |
| **Behavior** | Regex-validate `CSC[a-z]{2}\d{5}`. Create `cdets_data/<ID>/`. Write trace index. |
| **Failure**  | Invalid ID → `init_valid=False` → route to ABORT                                 |

### 6.1 `prescan` — Pre-LLM Enrichment

| Property     | Value                                                                                                                                              |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Type**     | Pure Python (no LLM)                                                                                                                               |
| **Input**    | `cdets_id`, `config`                                                                                                                               |
| **Output**   | `cdets_fields`, `component`, `primary_ap`, `primary_subap`, `blueprint_dir`, `topology`, `version`, `severity`, `dtpt_manager`, `prescan_coverage` |
| **Behavior** | Run `dumpcr -d` → parse fields. CSV lookup → AP. Run `analyze_cafy_coverage.py` → coverage dict.                                                   |
| **Failure**  | `dumpcr` timeout after retries → `cdets_fields={}` (agent will retry via MCP). CaFy failure → `prescan_coverage=None`. Neither is fatal.           |

### 6.1b `rag_fetch_related_cdets` — Related-CDETS Retrieval (RAG)

| Property     | Value                                                                                                                                              |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Type**     | Pure Python (no LLM) — local TF-IDF retrieval                                                                                                      |
| **Input**    | `cdets_id`, `cdets_fields`, `artifact_dir`, `config.rag`                                                                                           |
| **Output**   | `related_cdets`, `related_cdets_path`, `rag_top_score`, `rag_short_circuit`                                                                        |
| **Behavior** | Query the TF-IDF index (built from `1_bug_list_updated.csv`) for the top-k similar CDETS. If `cdets_id` is in the corpus use its own row vector; otherwise build a query from `cdets_fields` (Headline/Summary/Component/RCA/…). Write matches to `<ID>_related_cdets.json`. |
| **Short-circuit** | If `rag_top_score >= rag.high_match_threshold` (and `rag.enabled`), set `rag_short_circuit=True` → route straight to `delivery`, skipping the analysis stages. |
| **Failure**  | Missing index / empty corpus / retrieval error → `related_cdets=[]`, `rag_short_circuit=False`. Non-fatal; the normal pipeline continues.          |

### 6.2 `cdets_tz_analyzer` — Schema Generation (LLM Agent)

| Property             | Value                                                                                                          |
| -------------------- | -------------------------------------------------------------------------------------------------------------- |
| **Type**             | Azure OpenAI Agent                                                                                             |
| **System Prompt**    | `prompts/cdets_analyzer.md`                                                                                    |
| **Tools**            | `cdets_lookup`, `techzone_lookup`, `read_schema_template`, `write_artifact`                                    |
| **Input from state** | `cdets_id`, `cdets_fields`, `artifact_dir`                                                                     |
| **Output**           | `cdets_schema_path`, `tz_schema_path`, `union_schema_path`, `cdets_lookup_ok`, `has_techzone`, `schema_data`   |
| **Failure**          | CDETS lookup fail → `cdets_lookup_ok=False` → route to ABORT                                                   |
| **LLM Interaction**  | Multi-turn agent loop. LLM decides which tools to call, maps fields to schema, handles TechZone optional path. |

### 6.3 `cdets_scoring` — Quality Scorecard (LLM Agent)

| Property             | Value                                                                                          |
| -------------------- | ---------------------------------------------------------------------------------------------- |
| **Type**             | Azure OpenAI Agent                                                                             |
| **System Prompt**    | `prompts/scoring.md`                                                                           |
| **Tools**            | `read_artifact`, `write_artifact`                                                              |
| **Input from state** | `cdets_schema_path`, `artifact_dir`                                                            |
| **Output**           | `scorecard_path`, `cdet_ai_score`, `ai_confidence`, `automation_readiness`, `quality_blockers` |
| **Failure**          | Missing schema → fatal. Scoring always produces a result.                                      |
| **LLM Interaction**  | Single-turn: reads schema, applies v3 formula, writes scorecard. May use 1-2 tool calls.       |

### 6.4 `cafy_rca_analyzer` — Coverage RCA (LLM Agent)

| Property             | Value                                                                                                                                         |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| **Type**             | Azure OpenAI Agent                                                                                                                            |
| **System Prompt**    | `prompts/cafy_rca.md`                                                                                                                         |
| **Tools**            | `read_artifact`, `read_blueprint`, `read_ap_index`, `read_csv`, `run_cafy_analysis`, `write_artifact`                                         |
| **Input from state** | `cdets_schema_path`, `scorecard_path`, `primary_ap`, `primary_subap`, `blueprint_dir`, `prescan_coverage`, `artifact_dir`                     |
| **Output**           | `cafy_rca_json_path`, `cafy_rca_md_path`, `automation_mapping`, `genc_handoff`, `coverage_gap`, `gap_classification`, `cafy_coverage_verdict` |
| **Failure**          | CaFy not accessible → non-blocking warning, proceed with partial RCA.                                                                         |
| **LLM Interaction**  | Multi-turn: resolves AP/SubAP confidence chain, analyzes coverage, produces RCA artifacts.                                                    |

### 6.5a `testcase_generator` — Test Case Generation (LLM Agent) [Fan-out Branch 1]

| Property             | Value                                                                                                                                     |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| **Type**             | Azure OpenAI Agent                                                                                                                        |
| **System Prompt**    | `prompts/testcase_gen.md`                                                                                                                 |
| **Tools**            | `read_artifact`, `read_blueprint`, `write_artifact`                                                                                       |
| **Input from state** | `cdets_schema_path`, `cafy_rca_json_path`, `cafy_rca_md_path`, `primary_ap`, `primary_subap`, `blueprint_dir`, `topology`, `artifact_dir` |
| **Output**           | `testcase_path`, `test_scenarios`                                                                                                         |
| **Failure**          | Missing schema/RCA → fatal.                                                                                                               |
| **Parallel**         | Runs simultaneously with `existing_test_scanner`. No shared writes.                                                                       |
| **LLM Interaction**  | Multi-turn: reads evidence, consults blueprints, generates structured testcase markdown. Most token-heavy stage.                          |

### 6.5b `existing_test_scanner` — CaFy Source Scanner (Pure Python) [Fan-out Branch 2]

| Property             | Value                                                                                                 |
| -------------------- | ----------------------------------------------------------------------------------------------------- |
| **Type**             | Pure Python (no LLM)                                                                                  |
| **Input from state** | `primary_ap`, `primary_subap`, `blueprint_dir`, `cafy_rca_json_path`                                  |
| **Output**           | `existing_tests`, `existing_verifiers`, `existing_helpers`, `test_file_map`                            |
| **Parallel**         | Runs simultaneously with `testcase_generator`. No shared writes.                                      |
| **Behavior**         | Scans CaFy AP test directory for `*_ap.py` files. Regex-extracts test classes, `test_*` methods, `verify_*` verifiers, `helper_*` functions. Excludes xfail/flaky/deprecated/disabled tests. Returns structured inventory of existing automation. |
| **Failure**          | CaFy repo not accessible → `existing_tests=[]`. Non-fatal.                                            |

### 6.5c `merge_coverage` — Join Point (Pure Python)

| Property             | Value                                                                                                 |
| -------------------- | ----------------------------------------------------------------------------------------------------- |
| **Type**             | Pure Python (no LLM)                                                                                  |
| **Input from state** | `testcase_path`, `test_scenarios`, `existing_tests`, `existing_verifiers`, `existing_helpers`, `test_file_map` |
| **Output**           | `merged_coverage_input`                                                                               |
| **Behavior**         | Combines testcase scenarios + existing test inventory into a single comparison-ready dict. This gives `coverage_comparison` everything it needs in one read. |
| **Failure**          | Never fails — missing fields default to empty lists.                                                   |

### 6.6 `coverage_comparison` — Coverage Confidence (LLM Agent)

| Property             | Value                                                                                                 |
| -------------------- | ----------------------------------------------------------------------------------------------------- |
| **Type**             | Azure OpenAI Agent                                                                                    |
| **System Prompt**    | `prompts/coverage.md`                                                                                 |
| **Tools**            | `read_artifact`, `read_cafy_source`, `write_artifact`                                                 |
| **Input from state** | `testcase_path`, `merged_coverage_input`, `cafy_rca_json_path`, `primary_ap`, `primary_subap`, `artifact_dir` |
| **Output**           | `test_coverage_confidence`, `test_coverage_grade`, `coverage_classification`                          |
| **Failure**          | CaFy data empty → `test_coverage_confidence=0`, grade="N/A". Non-fatal.                               |
| **LLM Interaction**  | Single-to-multi-turn: compares generated testcase against existing test inventory (from `merged_coverage_input`), scores 6 weighted dimensions (Observable 30%, Trigger 20%, Topology 15%, Platform 15%, Config/Scale 10%, Framework 10%). |

### 6.7 `email_report_generator` — Report Assembly

| Property             | Value                                                                           |
| -------------------- | ------------------------------------------------------------------------------- |
| **Type**             | Python + minimal LLM (template rendering)                                       |
| **Tools**            | `read_artifact`, `generate_feedback_token`, `render_template`, `write_artifact` |
| **Input from state** | All artifact paths, scores, AP/SubAP, `config`                                  |
| **Output**           | `email_payload`, `email_subject`, `attachment_paths`                            |
| **Failure**          | Missing required artifacts → fatal.                                             |
| **LLM Interaction**  | Minimal — mostly Jinja2 template rendering. LLM may summarize for email body.   |

### 6.8 `delivery` — External Side Effects (Terminal Node)

| Property             | Value                                                                               |
| -------------------- | ----------------------------------------------------------------------------------- |
| **Type**             | Pure Python (no LLM)                                                                |
| **Tools**            | `mongo_upsert`, `tftp_copy`, `send_email`, `cdets_attach`, `post_performance`       |
| **Input from state** | All artifact paths, `email_payload`, `config`                                       |
| **Output**           | `mongo_pushed`, `tftp_delivered`, `email_sent`, `cdets_attached`, `delivery_status` |
| **Failure**          | All delivery failures are non-fatal. Logged and continued.                          |
| **Dry-run**          | `--dry-run` flag skips all external side effects.                                   |
| **Next**             | `END` — pipeline terminates after delivery.                                         |

---

## 7. Routing Logic

```python
def route_after_init(state: FLAgentState) -> str:
    """Gate: valid CDETS ID?"""
    return "prescan" if state["init_valid"] else "abort"

def route_after_cdets(state: FLAgentState) -> str:
    """Gate: CDETS lookup succeeded?"""
    return "cdets_scoring" if state["cdets_lookup_ok"] else "abort"

def route_after_prescan(state: FLAgentState) -> str:
    """Gate: CDETS fetched? If so, run RAG retrieval next."""
    return "rag_fetch_related_cdets" if state["cdets_lookup_ok"] else "abort"

def route_after_rag(state: FLAgentState) -> str:
    """Short-circuit to delivery on a high-confidence duplicate match."""
    return "delivery" if state["rag_short_circuit"] else "cdets_tz_analyzer"

```

Delivery connects directly to `END` — no conditional routing needed after it.

### Fan-out Edges (no routing function needed)

LangGraph runs both branches automatically when two `add_edge` calls share the same source:

```python
# cafy_rca feeds BOTH branches in parallel
g.add_edge("cafy_rca", "testcase_gen")            # branch 1 (LLM)
g.add_edge("cafy_rca", "existing_test_scanner")    # branch 2 (Python)

# Both branches feed into the join node — LangGraph waits for both
g.add_edge("testcase_gen", "merge_coverage")
g.add_edge("existing_test_scanner", "merge_coverage")

# After merge, continue sequentially
g.add_edge("merge_coverage", "coverage_comparison")
g.add_edge("coverage_comparison", "email_report")
g.add_edge("email_report", "delivery")
g.add_edge("delivery", END)
```

---

## 8. LLM Configuration

```python
from langchain_openai import AzureChatOpenAI
from langchain.schema import HumanMessage, SystemMessage

llm = AzureChatOpenAI(
    azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT"),      # e.g. "gpt-4o"
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),          # e.g. "https://xxx.openai.azure.com/"
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
    temperature=0.1,        # Low temperature for deterministic analysis
    max_tokens=4096,        # Per-response cap
)
```

### Per-Stage Model Routing (Optional)

| Stage                    | Complexity | Recommended Deployment |
| ------------------------ | ---------- | ---------------------- |
| `cdets_tz_analyzer`      | Medium     | gpt-4o                 |
| `cdets_scoring`          | Low-Medium | gpt-4o-mini            |
| `cafy_rca_analyzer`      | High       | gpt-4o                 |
| `testcase_generator`     | High       | gpt-4o                 |
| `test_coverage_analyzer` | Medium     | gpt-4o                 |
| `email_report_generator` | Low        | gpt-4o-mini            |

---

## 9. Tool Definitions

Each tool is a LangChain `@tool`-decorated function. Agents bind only the tools they need.

```python
from langchain_core.tools import tool

@tool
def cdets_lookup(cdets_id: str) -> dict:
    """Fetch structured CDETS fields via dumpcr CLI. Returns parsed field dict."""
    ...

@tool
def techzone_lookup(tz_thread_id: str) -> dict:
    """Fetch TechZone thread content. Returns thread data or empty on failure."""
    ...

@tool
def read_artifact(artifact_path: str) -> str:
    """Read an artifact file from the defect data directory."""
    ...

@tool
def write_artifact(filename: str, content: str) -> str:
    """Write an artifact file to the current defect data directory. Returns path."""
    ...

@tool
def read_blueprint(ap_name: str, filename: str) -> str:
    """Read an AP/SubAP blueprint file. Returns content or empty."""
    ...

@tool
def read_ap_index() -> dict:
    """Read ap_subap_index.json. Returns AP→SubAP mapping."""
    ...

@tool
def read_csv_mapping(component: str) -> dict:
    """Look up Component→AP from comp_ap_dtpt_mgr_mapping.csv."""
    ...

@tool
def run_cafy_analysis(cdets_id: str, ap_name: str) -> dict:
    """Run analyze_cafy_coverage.py and return coverage verdict JSON."""
    ...

@tool
def read_cafy_source(ap_name: str, test_file: str) -> str:
    """Read a CaFy AP test source file (*_ap.py). Returns content."""
    ...

@tool
def mongo_upsert(document: dict) -> str:
    """Upsert defect record to MongoDB cdetDB.orders. Returns status."""
    ...

@tool
def tftp_copy(source_path: str, dest_subdir: str) -> str:
    """Copy artifact to TFTP server path. Returns destination."""
    ...

@tool
def send_email(payload: dict) -> str:
    """Send analysis email via sendmail. Returns delivery status."""
    ...

@tool
def cdets_attach(cdets_id: str, file_path: str, title: str) -> str:
    """Attach file to CDETS defect via addfile CLI. Returns status."""
    ...

@tool
def generate_feedback_token(cdets_id: str) -> str:
    """Generate HMAC feedback token for dashboard rating link."""
    ...

```

---

## 10. Agent Loop Pattern (per LLM node)

Each LLM-backed node runs this inner loop:

```python
from langchain_openai import AzureChatOpenAI
from langchain.schema import HumanMessage, SystemMessage, AIMessage
from langchain_core.messages import ToolMessage

async def run_agent_node(
    llm: AzureChatOpenAI,
    system_prompt: str,
    user_message: str,
    tools: list,
    max_iterations: int = 10,
) -> tuple[str, list]:
    """
    Generic agent loop for a single LangGraph node.
    Returns (final_text, tool_call_log).
    """
    llm_with_tools = llm.bind_tools(tools)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
    ]
    tool_log = []

    for _ in range(max_iterations):
        response = await llm_with_tools.ainvoke(messages)
        messages.append(response)

        # If no tool calls → final answer
        if not response.tool_calls:
            return response.content, tool_log

        # Execute tool calls
        for tool_call in response.tool_calls:
            tool_fn = _resolve_tool(tool_call["name"], tools)
            result = await tool_fn.ainvoke(tool_call["args"])
            tool_log.append({"tool": tool_call["name"], "args": tool_call["args"], "result": result})
            messages.append(ToolMessage(
                content=str(result),
                tool_call_id=tool_call["id"],
            ))

    # Max iterations reached
    return messages[-1].content, tool_log
```

---

## 11. CLI Interface

```
usage: run_fl_pipeline.py [-h] [--parallel N] [--from-file FILE]
                          [--dry-run] [--config PATH] [--verbose]
                          [cdets_ids ...]

FL LangGraph Agent Pipeline

positional arguments:
  cdets_ids             One or more CDETS IDs (CSCxx99999)

options:
  --parallel N          Max concurrent pipeline instances (default: 3)
  --from-file FILE      Read CDETS IDs from file (one per line)
  --dry-run             Skip all external side effects (MongoDB, TFTP, email, CDETS)
  --config PATH         Path to config.yaml (default: src/backend/config/config.yaml)
  --stages STAGES       Run only specific stages (comma-separated)
  --verbose             Enable debug logging

```

---

## 12. Configuration (`src/backend/config/config.yaml`)

```yaml
azure_openai:
  endpoint: ${AZURE_OPENAI_ENDPOINT}
  api_version: "2024-12-01-preview"
  deployments:
    default: "gpt-4o"
    scoring: "gpt-4o-mini"
    email: "gpt-4o-mini"
  temperature: 0.1
  max_tokens: 4096

mongodb:
  uri: ${MONGO_URI:-mongodb://<user>:<password>@<host>:27017/?authSource=admin}
  database: "cdetDB"
  collection: "orders"

tftp:
  root: "/path/to/cdets_feedback"

email:
  enabled: true
  cc: []
  include_ap_owner: true
  template_dir: "pipeline/templates"

cdets:
  dumpcr_bin: "/path/to/dumpcr"
  cbugval_bin: "/path/to/cbugval"
  timeout: 20
  retries: 2

cafy:
  ap_root: ${FL_CAFY_AP_ROOT:-/path/to/cafy/work-dir}
  analyze_script: "scripts/analyze_cafy_coverage.py"

blueprints:
  root: ${FL_BLUEPRINT_ROOT:-/path/to/blueprints}

rag:
  corpus_csv: ${FL_RAG_CORPUS_CSV:-/path/to/bug_list.csv}
  index_dir: ${FL_RAG_INDEX_DIR:-cdets_data/rag_index}
  id_column: Identifier
  top_k: 5
  enabled: true              # toggle the duplicate short-circuit
  high_match_threshold: 0.45 # best-match similarity that triggers short-circuit
  text_columns: [description, eng_notes, scrub_notes, release_notes, cfd_analysis, regression_analysis]

paths:
  artifact_base: "cdets_data"
  config_dir: "config"
  ap_csv: "config/comp_ap_dtpt_mgr_mapping.csv"
  ap_index: "config/ap_subap_index.json"

batch:
  max_parallel: 5
  rate_limit_rpm: 60 # Azure OpenAI requests per minute
```

---

## 13. File Structure

```
fl-langgraph-agent/
├── README.md
├── requirements.txt
├── .env.example                    # LLM gateway + Mongo secrets (copy to .env)
│
├── src/
│   ├── backend/
│   │   ├── pipeline/               # LangGraph pipeline core (package: backend.pipeline)
│   │   │   ├── state.py            # FLAgentState TypedDict
│   │   │   ├── graph.py            # StateGraph definition + compile
│   │   │   ├── batch.py            # Parallel batch runner (asyncio)
│   │   │   ├── llm.py              # LLM factory + per-stage routing
│   │   │   ├── agent_loop.py       # Generic tool-calling agent loop
│   │   │   ├── prescan.py          # dumpcr + CSV + CaFy prescan
│   │   │   ├── utils.py            # repo_root() / backend_root() / load_config()
│   │   │   ├── nodes/              # one module per pipeline stage
│   │   │   ├── tools/              # cdets / cafy / blueprints / mongo / tftp / email / filesystem
│   │   │   ├── rag/                # local TF-IDF related-CDETS retrieval
│   │   │   ├── prompts/            # per-stage system prompts (.md)
│   │   │   ├── templates/          # Jinja2 email templates
│   │   │   └── schemas/            # Defect_Schema_Template_v1.0.json
│   │   ├── api/                    # FastAPI web-console backend (package: backend.api)
│   │   │   ├── server.py           # endpoints + SSE event stream
│   │   │   ├── runner.py           # drives the graph for console runs
│   │   │   ├── jobs.py             # job registry / live events
│   │   │   └── agents.py, generate.py, llm.py
│   │   ├── cli/                    # command-line entry points (package: backend.cli)
│   │   │   ├── run_fl_pipeline.py  # run the pipeline for one or more CDETS IDs
│   │   │   ├── build_rag_index.py  # build / query the RAG TF-IDF index
│   │   │   └── check_bedrock_token.py
│   │   └── config/
│   │       ├── config.yaml
│   │       ├── comp_ap_dtpt_mgr_mapping.csv
│   │       └── ap_subap_index.json
│   │
│   ├── frontend/                   # Vite + React + TypeScript SPA
│   │   ├── index.html, package.json, vite.config.ts
│   │   └── src/                    # components/, api.ts, types.ts, ...
│   │
│   ├── eval/                       # Phoenix tracing + LLM-as-judge (package: eval)
│   │   ├── tracing.py              # setup_phoenix_tracing()
│   │   ├── evals.py                # judge prompts + run_phoenix_evals()
│   │   └── run_phoenix_evals.py    # CLI entry point
│   │
│   └── docs/
│       └── SPEC.md                 # ← This file
│
├── cdets_data/                     # per-CDETS artifacts + RAG index (created at runtime)
│
└── tst/                            # pytest suite
    ├── conftest.py                 # adds src/ to sys.path
    ├── pytest.ini
    ├── test_blueprints.py, test_graph_routing.py, test_prescan.py
    └── test_rag_node.py, test_utils.py
```

---

## 14. Environment Variables

```bash
# Azure OpenAI (required)
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=sk-...
AZURE_OPENAI_DEPLOYMENT=gpt-4o
AZURE_OPENAI_API_VERSION=2024-12-01-preview

# MongoDB
MONGO_URI=mongodb://<user>:<password>@<host>:27017/?authSource=admin

# CDETS tools
FL_CAFY_AP_ROOT=/path/to/cafy/work-dir
FL_BLUEPRINT_ROOT=/path/to/blueprints

# Optional
FL_COVERAGE_ORACLE_CMD=/path/to/run_coverage_oracle_enrichment.sh
FL_GENC_CMD=/path/to/run_genc_testcase_from_plan.sh
DASHBOARD_URL=http://your-dashboard-host:3005
```

---

## 15. Testing Strategy

| Level           | Scope                                          | How                                                                         |
| --------------- | ---------------------------------------------- | --------------------------------------------------------------------------- |
| **Unit**        | Individual nodes with mocked LLM/tools         | `pytest tst/test_*.py`                                                    |
| **Integration** | Full graph with mock LLM, real filesystem      | `pytest tst/ -m integration`                                              |
| **E2E**         | Full graph with real Azure OpenAI + real CDETS | `python src/backend/cli/run_fl_pipeline.py CSCxx12345 --verbose`                            |
| **Batch**       | Multiple IDs, verify isolation                 | `python src/backend/cli/run_fl_pipeline.py --from-file test_ids.txt --parallel 3 --dry-run` |

### Mock LLM for Tests

```python
from langchain_core.messages import AIMessage

class MockAzureLLM:
    """Returns canned responses for deterministic testing."""
    def __init__(self, responses: list[str]):
        self._responses = iter(responses)

    async def ainvoke(self, messages):
        return AIMessage(content=next(self._responses))
```

---

## 16. Observability

Each node writes a trace entry to `state["stage_traces"]`:

```python
{
    "cdets_tz_analyzer": {
        "start_time": "2026-06-18T10:00:00Z",
        "end_time": "2026-06-18T10:00:12Z",
        "duration_seconds": 12.3,
        "llm_calls": 3,
        "tool_calls": [
            {"tool": "cdets_lookup", "duration": 2.1},
            {"tool": "write_artifact", "duration": 0.05}
        ],
        "input_tokens": 2400,
        "output_tokens": 1800,
        "status": "SUCCESS"
    }
}
```

Final summary printed to stdout and optionally POSTed to dashboard `/api/agent-performance`.

---

## 17. Implementation Order

| Phase   | What                                                                                                   | Depends On |
| ------- | ------------------------------------------------------------------------------------------------------ | ---------- |
| **P0**  | `state.py`, `graph.py` (skeleton with fan-out edges), `common_infra.py`, `abort.py`                    | Nothing    |
| **P1**  | `prescan.py`, `tools/cdets.py`, `tools/filesystem.py`                                                  | P0         |
| **P2**  | `llm.py`, `agent_loop.py`, `prompts/cdets_analyzer.md`, `nodes/cdets_analyzer.py`                      | P1         |
| **P3**  | `prompts/scoring.md`, `nodes/scoring.py`                                                               | P2         |
| **P4**  | `prompts/cafy_rca.md`, `nodes/cafy_rca.py`, `tools/blueprints.py`, `tools/cafy.py`                     | P3         |
| **P5**  | `prompts/testcase_gen.md`, `nodes/testcase_gen.py` (fan-out branch 1)                                  | P4         |
| **P5b** | `nodes/existing_test_scanner.py` (fan-out branch 2)                                                    | P4         |
| **P5c** | `nodes/merge_coverage.py` (join node)                                                                  | P5 + P5b   |
| **P6**  | `prompts/coverage.md`, `nodes/coverage_comparison.py`                                                  | P5c        |
| **P7**  | `nodes/email_report.py`, `nodes/delivery.py`, `tools/mongo.py`, `tools/tftp.py`, `tools/email_tool.py` | P6         |
| **P8**  | `batch.py`, `src/backend/cli/run_fl_pipeline.py` (CLI)                                                                 | P7         |
| **P9** | Tests, observability, documentation                                                                    | P8         |

---

## 18. Open Questions

| #   | Question                                                                           | Decision Needed                                         |
| --- | ---------------------------------------------------------------------------------- | ------------------------------------------------------- |
| Q1  | Should prescan failures (dumpcr timeout) abort or let the LLM agent retry via MCP? | Recommend: prescan best-effort, agent has fallback tool |
| Q2  | Token budget per stage? Hard cap or advisory?                                      | Recommend: advisory with warning at 80%                 |
| Q3  | Should state be persisted (LangGraph checkpointing) for resume on failure?         | Recommend: yes for batch, no for single                 |
| Q4  | Azure OpenAI rate limit handling — retry with backoff or queue?                    | Recommend: exponential backoff in `llm.py`              |
| Q5  | Schema template — copy from FL_agent or symlink?                                   | Recommend: copy (no cross-repo dependency)              |

---

## 19. Success Criteria

- [ ] `python src/backend/cli/run_fl_pipeline.py CSCxx12345` produces all 5 required artifacts
- [ ] Artifacts pass the same validation as IDE-mode FL Agent
- [ ] `--dry-run` produces artifacts without external side effects
- [ ] Batch mode handles 10 IDs in parallel without cross-contamination
- [ ] Each stage is independently testable with mocked dependencies
- [ ] Total token usage per defect is within 2x of IDE-mode equivalent
