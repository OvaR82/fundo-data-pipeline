# Fundo take-home: incremental SQL Server → warehouse, duplicate customers, trust checks

A small local version of the pipeline: SQL Server (source) → Python → DuckDB (BigQuery stand-in).
Design decisions, measurements and what was skipped are in **[SOLUTION.md](SOLUTION.md)**.

## Requirements

- Docker with Compose v2 (Docker Desktop, or Docker Engine + compose plugin ≥ 2.20).
- ~3 GB free RAM for SQL Server.
- Apple Silicon: the SQL Server image is x86-only; enable *"Use Rosetta for x86/amd64 emulation"* in Docker Desktop.
- Nothing else. Python, drivers and DuckDB live inside the `pipeline` image.
- A `Makefile` with the same commands is included as a shortcut. It is optional.

## Run it (2 commands)

```bash
docker compose up -d --wait
```
Starts SQL Server 2022 and waits until it is healthy (~30–60 s on first start, plus the image download).

```bash
docker compose run --rm --build pipeline demo
```
Builds the pipeline image (first time ~1 min), then runs the whole story end to end:

| Step | What happens | What to look for |
|---|---|---|
| 1 | Create database `fundo`, tables, Change Tracking | `source schema ready` |
| 2 | Seed synthetic data with the planted traps | row counts + `planted ...` lines |
| 3 | First load = full initial load of every table | `mode=full` per table |
| 4 | Simulate one business day (~1 % of rows change) + one duplicate registration | rows inserted/updated/deleted |
| 5 | Incremental load | `mode=incremental`, only the changed rows move |
| 6 | Same load again | **0 rows** moved (except `card_brands`, 4 rows copied whole by design) |
| 7 | Resolve duplicate customers | merges by rule, review queue, contact quality counts |
| 8 | Reconcile source vs warehouse | `WAREHOUSE OK: N checks, 0 failed` |
| 9 | Transfer report | full copy vs incremental, measured (~6.5 MB vs ~69 KB) |

The whole demo takes ~25 s after the image is built. Exit code 0 means every check passed.

The demo can be re-run: setup and seed are idempotent (seed is skipped when data exists), and each
re-run simulates one more day.

The warehouse is a file at `./data/warehouse.duckdb`. To look inside without installing anything:

```bash
docker compose run --rm pipeline query "SELECT reason, customer_ids, recommendation FROM core.dedup_review_queue ORDER BY reason"
```

## Commands

All commands run as `docker compose run --rm pipeline <command>` (add `--build` after editing code).

| Command | What it does |
|---|---|
| `demo` | Everything above, in order. Exit code 1 if a check fails. |
| `setup` | Create the source database and schema. Idempotent. |
| `seed` | Fill the source with synthetic data. Skipped if already seeded. |
| `simulate` | Apply one business day of inserts/updates/deletes to the source, plus one duplicate registration attempt. |
| `load` | Incremental load of every table. Safe to run any number of times. |
| `load --full-refresh <table…>` | Re-read those tables completely (the repair path). |
| `load --fail-at <table>` | Crash on purpose while loading that table, before it commits. |
| `dedup` | Rebuild the golden-customer layer (`core.*`) from the replicated tables. |
| `check` | Source-vs-warehouse reconciliation. Prints PASS/FAIL per check, exit code 1 on any FAIL. |
| `break --scenario lose-rows\|zombie\|amount-drift` | Damage the warehouse on purpose. |
| `merge-source` | `load` + `dedup`, then apply the merge decisions in the source (every change logged) and add a unique index that blocks new duplicates. |
| `report` | Measured transfer per table: full copy vs incremental. |
| `query "<sql>"` | Run SQL against the warehouse. |
| `test` | Unit tests (identity rules, dedup decisions, load atomicity/idempotency). |
| `reset --yes` | Drop the source database and delete the warehouse file. Synthetic data only. |

## Demo: run twice, same result

```bash
docker compose run --rm pipeline load
docker compose run --rm pipeline load
```
The second run shows `upserted 0, deleted 0` for every incrementally loaded table (`card_brands` is a
4-row full snapshot and always shows 4), and `check` stays green.

## Demo: recover from a failure mid-run

```bash
docker compose run --rm pipeline simulate
docker compose run --rm pipeline load --fail-at cards
docker compose run --rm pipeline check
docker compose run --rm pipeline load
docker compose run --rm pipeline dedup
docker compose run --rm pipeline check
```
1. `load --fail-at cards` loads `card_brands`, `customers`, `advances` (committed), then crashes inside
   the `cards` transaction: `RUN FAILED ... Run load again to resume` (exit code 1). `transactions` and
   `customer_history` are not reached.
2. `check` exits 1. `customers` and `advances` PASS; `cards`, `transactions` and `customer_history`
   FAIL with `N source changes not loaded yet` and the exact keys missing. Nothing is half-written:
   every table is either fully at the old checkpoint or fully at the new one.
3. `load` again picks up exactly where each table stopped: `customers`/`advances` move 0 rows, the
   others catch up.
4. `dedup` resolves the customers that arrived today (without it, `check` correctly reports
   `dedup_coverage` as stale), then `check` → `WAREHOUSE OK: 23 checks, 0 failed`.

## Demo: break the warehouse, watch the check fail, repair

```bash
docker compose run --rm pipeline break --scenario lose-rows
docker compose run --rm pipeline check
docker compose run --rm pipeline load --full-refresh transactions
docker compose run --rm pipeline check
```
`check` exits 1 and names the problem down to the keys:
```
FAIL  row_count   transactions   source=94,509 warehouse=94,484 diff=-25
FAIL  reconcile   transactions   1 bucket(s) differ; MISSING in warehouse 25: [5001, 5002, 5003, ... (+17 more)]   (exact ids vary per run)
WAREHOUSE NOT TRUSTWORTHY: 2 of 23 checks failed
```
A plain incremental `load` would *not* fix this (Change Tracking only knows
about source changes), which is why the checks exist. The full refresh of that one table repairs it.

Other scenarios: `zombie` (rows deleted in the source are active again in the warehouse →
`SHOULD NOT BE THERE`; needs a prior `simulate` + `load` so there are deleted rows) and
`amount-drift` (values changed → `CONTENT differs`). Same repair command.

## Demo: stop duplicates at the source

```bash
docker compose run --rm pipeline merge-source
docker compose run --rm pipeline simulate
docker compose run --rm pipeline load
docker compose run --rm pipeline dedup
docker compose run --rm pipeline check
```
1. `merge-source` first runs `load` and `dedup` so its decisions match the source right now. Then, in
   one source transaction: moves cards (and non-funded advances) of each merged-away customer to the
   survivor, marks the loser `merged_into_customer_id`, flags test accounts and review cases
   (`dedup_status`), writes every changed value to `dbo.customer_merge_log`, and creates a unique
   index on the normalized national ID for active customers. Nothing is deleted; the log is the way
   back (one `UPDATE` per row).
2. `simulate` tries to register an existing active person again with a re-typed RUT: now `REJECTED by
   ux_customers_national_id_norm` (before `merge-source` the same attempt prints `CREATED a duplicate`).
3. `load` brings the merge into the warehouse through the normal incremental path; `dedup` resolves
   the merged records by their RUT, exactly as before; `check` is green.

Running `merge-source` again is a no-op (0 rows re-owned) until new duplicates appear.

## Tests

```bash
docker compose run --rm pipeline test
```

## Clean up

To start the story over, reset source and warehouse together:
```bash
docker compose run --rm pipeline reset --yes
```
To remove everything:
```bash
docker compose down -v
```
Removes the SQL Server container and its data. Delete `./data/` to remove the warehouse file.

Note: `down -v` followed by `up` gives you a brand-new source next to the *old* warehouse file. The
pipeline notices (the source's Change Tracking version goes backwards), `check` says so, and the next
`load` re-reads every table; it is just slower than `reset --yes`.

## Layout

```
docker-compose.yml        SQL Server + one-shot pipeline container
sql/source/               source DDL: schema + Change Tracking, duplicate prevention (additive)
src/pipeline/config.py    per-table load strategy (the one place that decides it)
src/pipeline/load.py      extract (CT / watermark / snapshot) + atomic apply
src/pipeline/checks.py    reconciliation
src/pipeline/identity.py  identity rules (pure functions)
src/pipeline/dedup.py     golden customers, review queue
src/pipeline/prevent.py   merge-source (source-side prevention)
src/pipeline/seed.py      synthetic data and its traps;  simulate.py: one business day
tests/                    unit tests
```
