"""Apply the warehouse's merge decisions to the source, then stop new duplicates there.

merge-source  1. `load` + `dedup`, so the decisions reflect the source as it is right now.
              2. Additive DDL from sql/source/010_prevent_dups.sql (normalized id column, log table).
              3. In ONE source transaction: for every auto-merge (rule = rut_match) move the loser's
                 cards and advances to the survivor and mark the loser merged_into_customer_id.
                 Test accounts -> dedup_status 'test'; records a human must look at -> 'review_hold'.
                 Every changed value is written to dbo.customer_merge_log first (the way back: one
                 UPDATE per log row). Finally the unique filtered index on national_id_norm is
                 created in the same transaction: if any two active customers still collide,
                 the whole thing rolls back. Nothing is ever deleted.

The merge reaches the warehouse through the next incremental load (Change Tracking).
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import db, dedup, load

INDEX = "ux_customers_national_id_norm"
HOLD_REASONS = ("multiple_funded_same_rut", "same_rut_conflicting_attributes", "test_pattern_with_money")


def _decisions(wh) -> tuple[list[tuple], list[tuple]]:
    merges = wh.execute("""
        SELECT customer_id, golden_customer_id FROM core.customer_xref
        WHERE rule = 'rut_match' ORDER BY customer_id""").fetchall()
    flags: dict[int, str] = {}
    # review_hold: cases a human must decide, plus ids that are not valid (two customers who both
    # typed the same wrong RUT must not block each other, and must not be merged either).
    for (cid,) in wh.execute("""
            SELECT CAST(unnest(string_split(customer_ids, ',')) AS INTEGER) FROM core.dedup_review_queue
            WHERE reason IN (SELECT unnest(?))
            UNION
            SELECT customer_id FROM core.customer_quality
            WHERE rut_status IN ('bad_check_digit', 'placeholder', 'malformed')""", [list(HOLD_REASONS)]).fetchall():
        flags[cid] = "review_hold"
    for (cid,) in wh.execute("SELECT customer_id FROM core.customer_xref WHERE rule = 'excluded_test'").fetchall():
        flags[cid] = "test"
    return merges, sorted(flags.items())


def merge_source() -> None:
    print("refreshing the warehouse so the decisions match the source right now")
    load.run()
    dedup.run()
    src, wh = db.source(), db.warehouse()
    db.run_sql_file(src, "010_prevent_dups.sql")
    merges, flags = _decisions(wh)
    batch = datetime.now(timezone.utc).strftime("merge-%Y%m%dT%H%M%S")
    done: list[str] = []   # printed only after COMMIT
    try:
        with db.transaction(src) as cur:
            cur.execute("CREATE TABLE #merge (loser INT PRIMARY KEY, survivor INT NOT NULL)")
            cur.execute("CREATE TABLE #flag (customer_id INT PRIMARY KEY, status VARCHAR(20) NOT NULL)")
            if merges:
                db.insert_rows(src, "#merge", ["loser", "survivor"], merges)
            if flags:
                db.insert_rows(src, "#flag", ["customer_id", "status"], flags)
            # Skip what was handled by an earlier batch, and re-check the money rule against the
            # source itself at merge time.
            cur.execute("""
                DELETE m FROM #merge m WHERE EXISTS (
                    SELECT 1 FROM dbo.advances a WHERE a.customer_id = m.loser AND a.status IN ('funded','paid_off'))
                OR EXISTS (SELECT 1 FROM dbo.customers c WHERE c.customer_id = m.loser AND c.dedup_status IS NOT NULL)""")
            cur.execute("DELETE f FROM #flag f JOIN dbo.customers c ON c.customer_id = f.customer_id "
                        "WHERE c.dedup_status IS NOT NULL")
            log = "INSERT dbo.customer_merge_log (batch_id, entity, entity_id, column_name, old_value, new_value) "
            for entity, table, key in (("card", "cards", "card_id"), ("advance", "advances", "advance_id")):
                cur.execute(log + f"""SELECT %s, '{entity}', x.{key}, 'customer_id', x.customer_id, m.survivor
                                      FROM dbo.{table} x JOIN #merge m ON x.customer_id = m.loser""", (batch,))
                cur.execute(f"""UPDATE x SET customer_id = m.survivor, updated_at = SYSUTCDATETIME()
                                FROM dbo.{table} x JOIN #merge m ON x.customer_id = m.loser""")
                done.append(f"  {table:<10} re-owned by survivor: {cur.rowcount}")
            cur.execute(log + """SELECT %s, 'customer', m.loser, 'merged_into_customer_id', NULL, CAST(m.survivor AS NVARCHAR(100)) FROM #merge m
                                 UNION ALL
                                 SELECT %s, 'customer', m.loser, 'dedup_status', NULL, 'merged' FROM #merge m
                                 UNION ALL
                                 SELECT %s, 'customer', f.customer_id, 'dedup_status', NULL, f.status FROM #flag f""",
                        (batch, batch, batch))
            cur.execute("""UPDATE c SET merged_into_customer_id = m.survivor, dedup_status = 'merged',
                                  updated_at = SYSUTCDATETIME()
                           FROM dbo.customers c JOIN #merge m ON c.customer_id = m.loser""")
            done.append(f"  customers  merged into a survivor: {cur.rowcount}")
            cur.execute("""UPDATE c SET dedup_status = f.status, updated_at = SYSUTCDATETIME()
                           FROM dbo.customers c JOIN #flag f ON c.customer_id = f.customer_id""")
            done.append(f"  customers  flagged test / review_hold: {cur.rowcount}")
            # Same transaction: a remaining collision between active customers fails the merge.
            cur.execute(f"""IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = '{INDEX}')
                            CREATE UNIQUE INDEX {INDEX} ON dbo.customers (national_id_norm) WHERE dedup_status IS NULL""")
    except Exception:
        print("  merge-source rolled back: nothing changed in the source")
        raise
    print("\n".join(done))
    print(f"  batch {batch} logged in dbo.customer_merge_log; unique index {INDEX} in place")
    print("  next: `load` + `dedup` to bring the merge into the warehouse")
