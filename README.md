# ARGS Core v1 (MVP)

Goal: build an autonomous MA (Management / Risk Gate) core as policy-as-code:
- YAML policy
- deterministic rule evaluation
- append-only Event Log (JSONL)
- demo without IBKR and without runtime AS/WA

## Target architecture

AS (Analytical System) -> MA (Management/Risk Gate) -> WA (Worker Agents)
+ META AUDIT LAYER (offline, between iterations)

v1 implements: MA + Event Log + Demo only.

Context v1 (labels):
- Instrument: HG / MHG (Copper)
- Timeframe: 5m
- Environment: IBKR_PAPER_LABEL

## Setup (Windows PowerShell)

Check Python:
py -3.11 -V

Install deps:
py -3.11 -m pip install -r requirements.txt

Run demo:
py -3.11 -m args.demo.demo_ma_eval

View log:
type args\data\events.jsonl

## Notes

UNKNOWN / NO-DECISION is valid and safety-first.
Outcome (PnL / market move) does not affect MA correctness.

## Replay Harness (offline audit)

Run replay on the existing event log to verify that MA decisions are reproducible:

```powershell
py -3.11 -m args.demo.demo_replay

## Replay Harness (offline audit)

## Regression Set (offline guardrail)

Run regression tests (expected MA outputs for fixed scenarios):

```powershell
py -3.11 -m args.demo.demo_regression

## Regression Set (offline guardrail)

## Policy Diff Audit (offline)

Compare two policy YAML files and print a compact impact summary:

```powershell
py -3.11 -m args.demo.demo_policy_diff

## Meta Audit (Offline Governance)

Meta Audit is an **offline governance check** for MA policy changes.
It is designed to detect **dangerous or permissive modifications** to policy-as-code
*before* they reach runtime.

### What Meta Audit checks
- Removal or softening of **hard gates**
  (e.g. UNKNOWN → ALLOW is a FAIL)
- Unsafe changes to critical **risk limits**
- Structural drift in policy intent

### Guarantees
- **Offline only** (no runtime usage)
- **No side-effects** (never writes to events.jsonl)
- Deterministic: same inputs → same report
- Replay-safe and audit-friendly

### How to run
```bash
py -3.11 -m args.demo.demo_meta_audit

## Hardening Pack (Stage 15)

### Checkpoint script (snapshot + logs + sanity)
Run one command to reset copy-policy, run sanity suite, save logs, and create a repo snapshot:

```powershell
cd C:\Users\mukol\ARGS-Core-v1
powershell -ExecutionPolicy Bypass -File .\scripts\checkpoint.ps1 -Tag "stage15"

## Streamlit UI (Control Panel)

Install UI dependencies:

```powershell
cd C:\Users\mukol\ARGS-Core-v1
py -3.11 -m pip install -r .\requirements_ui.txt

## UI Polishing (Stage 17A)

Control Panel improvements:
- Refresh UI state / Clear results
- Tabs: Dashboard / Logs / Events / About
- Open folders: repo / logs / snapshots parent
- Log filtering by filename substring
- Safer output rendering with `st.text` (prevents UI artifacts)

## Contracts (Stage 17B)

Goal: define and validate MA input schema from policy-as-code.

### Path inventory from policy
Extracts all `path` atoms and ops used in the baseline policy:

```powershell
py -3.11 -m args.contracts.paths_from_policy

## Paper Loop v0 (Stage 19A)

One-command deterministic run that produces per-run artifacts (no accumulation):

```powershell
py -3.11 -m args.demo.demo_paper_loop_v0

schtasks /Query /TN "\ARGS_IBKR_Tap_1m" /V /FO LIST | Select-String "Logon Mode:|Status:|Last Result:"
schtasks /Query /TN "\ARGS_Stage5_Evidence_5m" /V /FO LIST | Select-String "Logon Mode:|Status:|Last Result:"

## Runtime Contract v1 (Market-only Ops / Unattended)

### Control Plane (single source of truth)
- `args/data/control_plane.json` is the only runtime configuration.
- Default is **safe-by-default**:
  - `execution_mode=DRYRUN`
  - `enable_paper_execution=false`
  - `global_mode=ONLY_EXITS` (or `HALT` when required)

### Always-on Sensing (READ-ONLY)
- IBKR event tap writes: `args/data/ibkr_events_live.jsonl`
- Scheduled task: `\ARGS_IBKR_Tap_1m`
- Must be READ-ONLY (no order placement/cancel/replace).

### Stage5 Execution Core (deterministic)
- Engine reads JSONL via `IbkrFileAdapterV0` (READ-ONLY unless explicitly enabled).
- Safety gates:
  - actions only if `enable_paper_execution=true` AND `execution_mode=PAPER`
  - `global_mode` limits actions (ONLY_EXITS/HALT).
- Fill integrity:
  - execId tagging for EXEC_DETAILS
  - FILL dedup (order_id + ts_utc + reason + filled_qty)

### Evidence (automatic)
- Terminal evidence pack: `scripts/stage5_terminal_evidence_pack.ps1`
- Scheduled task: `\ARGS_Stage5_Evidence_5m`
- Evidence folders: `args/stage5_evidence/<UTC_TS>/`
- Ops evidence: `args/ops_evidence/chaos/<UTC_TS>/`

### Health
- `args/data/ops_health.json` is the runtime health snapshot.
- Required properties: `ok`, `mode`, `loops`, `events_seen`, `reconcile_ratio`, `last_error`.


## Product Standard
См. docs/product_standard_v1.md — канонический стандарт артефактов, exit codes, JSON stdout и debug-by-evidence.

## Golden commands
См. docs/product_standard_v1.md (раздел 4).
