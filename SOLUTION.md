# SOLUTION

**Where the effort went.** The core is *reliability*: problems 1 and 3 are one piece, because incremental loading is only safe to switch on when something proves the warehouse matches the source. Duplicate resolution (2) is the second focus, because that is where the identity decisions live. The source-side prevention in 2 is the smallest mechanism that does what the brief asks, and it is the first thing I would trim.

**Labels:** **[measured]** came out of this repo on a laptop (`report`, `etl.runs`, `dedup`, `check` output). **[estimated]** is reasoning or list prices.

**Skipped on purpose:** history retention, dropping the scratch table, orchestration, real BigQuery. Reasons in *What is not solved*.

---

## 1. Move only what changed

**Mechanism: SQL Server Change Tracking (CT).** It gives the *net* change per primary key since a version number, deletes included, without SQL Agent or before/after images; retention is one setting. CDC needs the Agent and stores full row images I don't need. A `rowversion`/`updated_at` watermark cannot see deletes.

### Per-table strategy (single source: `src/pipeline/config.py`)

| Table | Strategy | Why | Trade-off accepted |
|---|---|---|---|
| customers, advances, cards | CT → upsert + soft delete | Small, mutable, deletes matter | One extra synchronous write per DML on the source [estimated: ~ one narrow index] |
| transactions | CT → upsert + soft delete | Largest, mostly append. "Rarely changed" is not "never": a watermark silently misses corrections and reversals | Same overhead on the hottest table. Fallback if DBAs object: watermark + nightly key reconciliation to catch deletes |
| customer_history | Append by PK watermark | Insert-only (trigger-written); CT would be pure overhead | Identities can commit out of order under concurrency, so a row can be skipped; `check` names it, `--full-refresh` repairs it. Production: lag window, or CT |
| card_brands | Full snapshot | 4 rows: tracking costs more than copying | None |
| tmp_marketing_export_2021 | Not replicated | Scratch, no owner, no reader | Drop candidate, not dropped |

### Reliability
- **Consistent extract:** CT version and changed rows are read in one `SNAPSHOT` transaction, so the checkpoint matches the data.
- **Atomic apply:** upserts, soft deletes and the new checkpoint commit in **one DuckDB transaction per table**. A crash leaves each table fully at the old checkpoint or fully at the new one.
- **Idempotent replay:** upsert by key, delete only if active; applying the same change set twice gives the same rows (unit test; a second `load` moves 0 rows).
- **Mid-run failure** [measured]: `load --fail-at cards` commits customers and advances, rolls back cards, never reaches transactions. `check` names the missing keys; a plain `load` resumes and `check` passes.
- **Checkpoint no longer valid → automatic full reload of that table**, which also soft-deletes what disappeared: CT retention expired (pipeline down > 7 days), or the source was **rebuilt or restored** (its CT version goes *backwards*). The second case I found by running the demo against a recreated database with the old warehouse file: every check failed and incremental loads could not repair it, because `CHANGETABLE` with a future version returns nothing, silently. Now `check` names the cause and `load` recovers.

### Measured [6 simulated days, ~1 % of rows changed per day]

| | Full copy (today) | Incremental (per day) |
|---|---|---|
| Rows | 112,629 | ~1,235 |
| Payload extracted | 6,472 KB | 68.7 KB (**1.06 %**) |
| transactions load time | 2.14 s | 0.25 s |
| Re-run, no changes | — | 0 rows |

The 1 % is my input (the simulator's change rate), not a finding. What is measured is that only the changed rows move. Times are laptop numbers on a tiny volume.

---

## 2. Duplicate customers

### Identity rules (`src/pipeline/identity.py`)

| Proves identity (may merge) | Only suggests (review queue, never merged) |
|---|---|
| **RUT** normalized (`12.345.678-5` = `123456785`) with a **valid check digit**, **not a placeholder** (`11.111.111-1` passes the check digit), **and** corroborated by a similar full name **or** the same birth date | **Email** (families, company inboxes), **phone** (households, recycled numbers), **name**, even **name + birth date** (namesakes) |

The corroboration matters: a shared RUT whose name and birth date both disagree is a typo, not one person, and goes to review. **Review corrected me here:** my first version also accepted "same surname + same first initial", which would merge *José Pérez* with *Javiera Pérez* on a mistyped RUT. It was removed and a test pins the case.

### Results [measured, fresh run]
- 4,873 active records − 164 merged by RUT − 6 test accounts excluded = **4,703 golden customers**.
- Review queue: 3 groups with money on several records, 2 RUT collisions with conflicting attributes, 1 test-looking account with money, 22 suggestive matches (10 email, 8 phone, 4 name + birth date).
- **The data corrected me:** I planted 2 "both funded" groups; the run found 3. The third is a RUT typo between two different people who both hold advances. It goes to review (safe), but the money rule fires before corroboration, so its label overstates certainty. Known, not fixed.

### Business rules
- **Funded / paid-off is untouchable:** that record survives even if newer (25 planted groups), and never loses a card or advance. A check enforces it; `merge-source` re-checks it against the source at merge time.
- **Two records with money:** no merge, both stay golden, group goes to review. What I would do: ops verifies the ID document; if it is one person, consolidate **in the core system first** and let the warehouse follow. Until then, report exposure per review group, not per record.
- **Otherwise the oldest record survives** (lowest id breaks ties): it is what other systems most likely reference. The golden record takes the survivor's identity and the **freshest valid** email/phone of the group.
- **Test data is excluded, not merged.** Signals: non-routable domain, an **internal mailbox that starts with a test word** (`test@`, `qa1@fundo.com`), or a **whole name token** that is a test word. So `Testerman`, `contest.winner@gmail.com` and staff on `@fundo.com` are kept. A test-looking account **with a funded advance** goes to review: real money moved. Exclusion runs before grouping, since test accounts share a placeholder RUT.
- **Malformed contacts: counted and flagged, never guessed** [measured]. Emails 102 malformed, 39 placeholder, 39 missing; phones 111 malformed, 88 placeholder; RUTs 25 bad check digit, 10 missing, 4 placeholder. Only unambiguous reformatting is applied (~3,500 phones to E.164); `juan@gmail` stays NULL with a status in `core.customer_quality`. Malformed values and placeholders never take part in matching, otherwise `000000000` would be one giant cluster.
- **Cards follow the survivor** (163–166 re-attributed across runs [measured]; the simulated day deletes a few unused cards) via `core.cards_resolved`; the raw row keeps its original owner as lineage. If this goes wrong: cards left on the merged record hide exposure; deleted cards orphan their transactions; cards moved to the **wrong** person (a false merge on email or phone) put one person's spend and credit risk on another. That last case is why only proving fields may merge.

### Stop the same duplicate being created again (`merge-source`)
`merge-source` runs `load` + `dedup` first, so decisions reflect the source right now. Additive DDL (`010_prevent_dups.sql`) adds a **bounded, normalized, persisted column `national_id_norm`** (the fix for the bad schema choice, original column untouched) and an append-only `customer_merge_log`. Then, in **one source transaction**: cards and non-funded advances move to the survivor, losers get `merged_into_customer_id`, test and review cases are flagged, every changed value is logged first, and the **unique filtered index** on active customers is created last, so any remaining collision rolls everything back. Nothing is deleted. [measured] Before: the simulator's re-typed RUT prints `CREATED a duplicate`; after: `REJECTED`. A second run is a no-op.

The normalization exists twice, in Python (`identity.py`, with check-digit validation) and in T-SQL (the computed column, without it), and no test compares them; that is why the index only covers ids Python marked valid, and everything else is put on `review_hold` instead of trusted. The log is the way back (one `UPDATE` per row); an automated undo command cost 50 lines of edge cases, so I removed it. **Known hole:** the 36 records on `review_hold` (including invalid RUTs) are outside the index. In production the merge and the constraint belong to the app team, with validation at sign-up; this shows the mechanism, not the rollout.

---

## 3. Prove the data is correct (`check`)

One command, PASS/FAIL per check, exit code 1 on any failure, history in `etl.check_results`, ~1 s here [measured].
- **freshness:** source changes not loaded yet; also flags a checkpoint ahead of the source (rebuilt/restored).
- **row_count:** active rows, source vs warehouse.
- **reconcile:** per bucket of keys (1k, 10k for transactions) compares count, sum(key), sum(amount), sum(epoch of `updated_at`/`txn_at`). Only differing buckets are pulled key by key, listing **MISSING**, **SHOULD NOT BE THERE** and **CONTENT differs**. Healthy runs move aggregates only, so it scales.
- **referential:** no active child points at a missing parent.
- **dedup invariants:** dedup is not stale; no funded customer was merged away.

**Break it on purpose** [measured]: `break --scenario lose-rows | zombie | amount-drift` each fail with the exact keys. A plain `load` does **not** repair them (CT only knows source changes), which is the point of the check; `load --full-refresh transactions` does.

**Blind spots:** the fingerprint relies on `updated_at`; a change that does not bump it (or a `merchant` change) is invisible. Fix: a per-row hash computed identically on both sides; skipped for time. Checks run against a live source, so changes landing mid-check show as freshness failures: correct, but noisy.

---

## Cost impact [estimated]
**Today:** extracting and transferring 100 % of the database daily (source CPU/IO, egress, compute scale with size, not change); storage of daily copies if kept; downstream queries rebuilding from full copies, billed per byte scanned. Batch loads are free.

**After:** transfer drops to about the change rate (~1 % here [measured]), no daily copies. New cost: BigQuery `MERGE` scans the target, so partition `transactions` by `txn_at`, cluster by key, stage changes with free load jobs, then merge. Checks add cheap per-bucket aggregates.

---

## What is not solved
- **History retention:** irreversible and legally constrained. Proposal: partition `customer_history` by `changed_at` with expiration per policy; archive then purge at the source.
- **Scratch tables:** excluded, not dropped (needs owner sign-off). Production: find candidates in `sys.dm_db_index_usage_stats`, rename before dropping.
- **Schema evolution:** the contract is `config.py`; a breaking change fails loudly, nothing propagates.
- **PII** unmasked in the warehouse; a single `sa` login with the password in compose (local only).
- **Dedup thresholds** (name ratio ≥ 0.85) not tuned on real data.
- **Not tested on Apple Silicon** (x86 image under Rosetta).

---

## Towards production
**Tools.** On GCP, **Datastream** (managed SQL Server CDC → BigQuery: backfill, changes, deletes) replaces my extract/apply code, the part I would least like to own. If that is not possible, this CT pattern as a Cloud Run job scheduled by Cloud Scheduler or Composer, landing in staging and merging into partitioned tables. Golden-customer logic in **dbt** with tests. `check` after every load, publishing to a table and an alert, with one "is the warehouse trustworthy" status for dashboard owners.

**One-time vs permanent.** One-time: backfill, cleaning the historical duplicate backlog (ops works the review queue), the unique-index rollout, scratch cleanup, retention purge. Permanent: replication, reconciliation checks, dedup of new records with the review queue, sign-up validation and the constraint in the app, a retention job.

**Deliver first: the reconciliation checks**, against today's full-copy pipeline. Read-only, so zero risk; they put numbers on "no trust" immediately; and they are the safety net for everything else: incremental runs side by side with the full copy until the checks prove them equal, and only then is the full copy turned off. Then incremental, for cost. Then deduplication, which needs business sign-off on the identity rules.
