"""Measured numbers from the run log: what a daily full copy moves vs what incremental moves."""
from __future__ import annotations

from . import db
from .config import EXCLUDED_TABLES, FULL_SNAPSHOT


def run() -> None:
    wh = db.warehouse()
    rows = wh.execute("""
        WITH ok AS (SELECT * FROM etl.runs),
        full_load AS (   -- most recent full read of each table = cost of copying it today
            SELECT table_name, arg_max(rows_upserted, finished_at) AS full_rows,
                   arg_max(bytes_extracted, finished_at) AS full_bytes
            FROM ok WHERE mode = 'full' GROUP BY 1),
        incr AS (        -- incremental runs that actually moved something
            SELECT table_name, count(*) AS runs, avg(rows_upserted + rows_deleted) AS avg_rows,
                   avg(bytes_extracted) AS avg_bytes
            FROM ok WHERE mode = 'incremental' AND (rows_upserted + rows_deleted) > 0 GROUP BY 1)
        SELECT f.table_name, any_value(r.strategy), f.full_rows, f.full_bytes,
               i.runs, i.avg_rows, i.avg_bytes
        FROM full_load f JOIN ok r USING (table_name) LEFT JOIN incr i USING (table_name)
        GROUP BY ALL ORDER BY f.full_bytes DESC
    """).fetchall()
    print("Transfer per load, MEASURED from etl.runs (bytes = extracted Arrow payload)")
    print(f"  {'table':<17} {'strategy':<17} {'full rows':>10} {'full KB':>9} {'incr runs':>9} "
          f"{'avg rows':>9} {'avg KB':>8} {'% of full':>9}")
    tot_full = tot_incr = 0.0
    for name, strategy, full_rows, full_bytes, runs, avg_rows, avg_bytes in rows:
        if strategy == FULL_SNAPSHOT:   # copied whole on every run, by design
            runs, avg_rows, avg_bytes = 0, full_rows, full_bytes
        tot_full += full_bytes
        tot_incr += avg_bytes or 0
        pct = f"{100 * (avg_bytes or 0) / full_bytes:8.2f}%" if full_bytes else "      n/a"
        print(f"  {name:<17} {strategy:<17} {full_rows:>10,} {full_bytes / 1024:>9,.1f} {runs or 0:>9} "
              f"{(avg_rows or 0):>9,.0f} {(avg_bytes or 0) / 1024:>8,.1f} {pct}")
    if tot_full:
        print(f"  {'TOTAL per day':<35} {'':>10} {tot_full / 1024:>9,.1f} {'':>9} {'':>9} "
              f"{tot_incr / 1024:>8,.1f} {100 * tot_incr / tot_full:8.2f}%")
    for name, why in EXCLUDED_TABLES.items():
        print(f"  not replicated: {name} - {why}")
