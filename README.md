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

