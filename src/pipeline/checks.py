"""Is the warehouse complete and correct right now? One command, PASS/FAIL per check.

Source-vs-warehouse checks:
  freshness     changes in the source not loaded yet (Change Tracking / watermark)
  row_count     active rows in source == active rows in warehouse
  reconcile     per key bucket: count, sum(key), sum(amount), sum(timestamp). Only buckets that
                disagree are drilled into, key by key, to name the missing rows, the rows that
                should no longer be there, and the rows whose content differs.
Warehouse-internal checks:
  referential   no active child row points at a missing/deleted parent
  dedup_*       business invariants of the golden customer layer
Results are also stored in etl.check_results (history of every check run).
"""
from __future__ import annotations

from . import db
from .config import APPEND_WATERMARK, CHANGE_TRACKING, TABLES, Table
from .load import ensure_warehouse, get_state

MAX_KEYS_SHOWN = 8
MAX_DRILL = 20


def _fmt_keys(keys) -> str:
    keys = sorted(keys)
    more = f" (+{len(keys) - MAX_KEYS_SHOWN} more)" if len(keys) > MAX_KEYS_SHOWN else ""
    return ", ".join(map(str, keys[:MAX_KEYS_SHOWN])) + more


def freshness(src, t: Table, last) -> tuple[str, str]:
    if last is None:
        return "FAIL", "never loaded"
    if t.strategy == CHANGE_TRACKING:
        current, min_valid = db.query(src, "SELECT CHANGE_TRACKING_CURRENT_VERSION(), "
                                           "CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID(%s))", (f"dbo.{t.name}",))[0]
        if min_valid is None:
            return "FAIL", "change tracking is not enabled on this table"
        if last > current:
            return "FAIL", (f"checkpoint {last} is ahead of the source ({current}): the source was rebuilt "
                            f"or restored; run load (it will re-read the table)")
        if last < min_valid:
            return "FAIL", f"checkpoint {last} older than CT retention ({min_valid}): run load --full-refresh {t.name}"
        pending = db.query(src, f"SELECT COUNT(*) FROM CHANGETABLE(CHANGES dbo.{t.name}, %s) ct", (last,))[0][0]
    else:
        source_max = db.query(src, f"SELECT COALESCE(MAX({t.pk}), -1) FROM dbo.{t.name}")[0][0]
        if last > source_max:
            return "FAIL", (f"watermark {last} is ahead of the source (max {source_max}): the source was "
                            f"rebuilt or restored; run load (it will re-read the table)")
        pending = db.query(src, f"SELECT COUNT(*) FROM dbo.{t.name} WHERE {t.pk} > %s", (last,))[0][0]
    return ("PASS", f"up to date at version {last}") if pending == 0 else \
           ("FAIL", f"{pending:,} source changes not loaded yet (version {last})")


def row_count(src, wh, t: Table) -> tuple[str, str]:
    s = db.query(src, f"SELECT COUNT(*) FROM dbo.{t.name}")[0][0]
    w = wh.execute(f"SELECT count(*) FROM raw.{t.name} WHERE NOT _is_deleted").fetchone()[0]
    return ("PASS" if s == w else "FAIL"), f"source={s:,} warehouse={w:,}" + ("" if s == w else f" diff={w - s:+,}")


def _fingerprints(src, wh, t: Table) -> tuple[dict, dict]:
    s_exprs, w_exprs = ["COUNT(*)", f"SUM(CAST({t.pk} AS BIGINT))"], ["count(*)", f"sum({t.pk})"]
    if t.amount_col:
        s_exprs.append(f"SUM({t.amount_col})")
        w_exprs.append(f"sum({t.amount_col})")
    if t.ts_col:
        s_exprs.append(f"SUM(DATEDIFF_BIG(SECOND, '19700101', {t.ts_col}))")
        w_exprs.append(f"sum(date_diff('second', TIMESTAMP '1970-01-01', {t.ts_col}))")
    b = t.bucket_size
    s = db.query(src, f"SELECT {t.pk} / {b}, {', '.join(s_exprs)} FROM dbo.{t.name} GROUP BY {t.pk} / {b}")
    w = wh.execute(f"SELECT {t.pk} // {b}, {', '.join(w_exprs)} FROM raw.{t.name} "
                   f"WHERE NOT _is_deleted GROUP BY 1").fetchall()
    norm = lambda rows: {int(r[0]): tuple(int(v) if isinstance(v, int) else v for v in r[1:]) for r in rows}
    return norm(s), norm(w)


def _rows(src, wh, t: Table, where_src: str, where_wh: str) -> tuple[dict, dict]:
    cols = list(t.columns)
    s = db.query(src, f"SELECT {', '.join(cols)} FROM dbo.{t.name} WHERE {where_src}")
    w = wh.execute(f"SELECT {', '.join(cols)} FROM raw.{t.name} WHERE NOT _is_deleted AND {where_wh}").fetchall()
    i = cols.index(t.pk)
    return {r[i]: tuple(r) for r in s}, {r[i]: tuple(r) for r in w}


def reconcile(src, wh, t: Table) -> tuple[str, str]:
    if t.bucket_size is None:      # tiny table: compare every row
        s, w = _rows(src, wh, t, "1=1", "true")
        bad_buckets = None
    else:
        fs, fw = _fingerprints(src, wh, t)
        bad_buckets = sorted(b for b in fs.keys() | fw.keys() if fs.get(b) != fw.get(b))
        if not bad_buckets:
            return "PASS", f"{len(fs)} key buckets of {t.bucket_size:,} match (count, keys, amounts, timestamps)"
        # Drill into at most MAX_DRILL buckets: enough to name the keys, without turning a large
        # failure into a full copy. The message says when keys come from a subset.
        ranges = " OR ".join(f"({t.pk} >= {b * t.bucket_size} AND {t.pk} < {(b + 1) * t.bucket_size})"
                             for b in bad_buckets[:MAX_DRILL])
        s, w = _rows(src, wh, t, ranges, ranges)
    missing = s.keys() - w.keys()
    unexpected = w.keys() - s.keys()
    changed = {k for k in s.keys() & w.keys() if s[k] != w[k]}
    if not (missing or unexpected or changed):
        return "PASS", "all rows match"
    parts = []
    if bad_buckets is not None:
        sampled = f" (keys below from the first {MAX_DRILL})" if len(bad_buckets) > MAX_DRILL else ""
        parts.append(f"{len(bad_buckets)} bucket(s) differ{sampled}")
    if missing:
        parts.append(f"MISSING in warehouse {len(missing)}: [{_fmt_keys(missing)}]")
    if unexpected:
        parts.append(f"SHOULD NOT BE THERE {len(unexpected)}: [{_fmt_keys(unexpected)}]")
    if changed:
        parts.append(f"CONTENT differs {len(changed)}: [{_fmt_keys(changed)}]")
    return "FAIL", "; ".join(parts)


REFERENTIAL = [
    ("advances", "customer_id", "customers", "customer_id"),
    ("cards", "customer_id", "customers", "customer_id"),
    ("cards", "brand_code", "card_brands", "brand_code"),
    ("transactions", "card_id", "cards", "card_id"),
]


def referential(wh) -> list[tuple]:
    out = []
    for child, fk, parent, pk in REFERENTIAL:
        n = wh.execute(f"""
            SELECT count(*) FROM raw.{child} c WHERE NOT c._is_deleted AND NOT EXISTS (
                SELECT 1 FROM raw.{parent} p WHERE p.{pk} = c.{fk} AND NOT p._is_deleted)""").fetchone()[0]
        out.append(("referential", f"{child}.{fk}", "PASS" if n == 0 else "FAIL",
                    f"{n:,} orphan rows -> {parent}"))
    return out


def dedup_invariants(wh) -> list[tuple]:
    if not db.table_exists(wh, "core", "customer_xref"):
        return [("dedup", "customers", "SKIP", "dedup has not run yet")]
    checks = [
        ("dedup_coverage", "every active customer is resolved (dedup is not stale)", """
            SELECT count(*) FROM raw.customers c WHERE NOT c._is_deleted
              AND NOT EXISTS (SELECT 1 FROM core.customer_xref x WHERE x.customer_id = c.customer_id)"""),
        ("dedup_funded_survives", "no customer with a funded/paid-off advance was merged away", """
            SELECT count(DISTINCT x.customer_id) FROM core.customer_xref x
            JOIN raw.advances a ON a.customer_id = x.customer_id AND NOT a._is_deleted
            WHERE a.status IN ('funded', 'paid_off')
              AND x.golden_customer_id IS DISTINCT FROM x.customer_id"""),
    ]
    out = []
    for name, label, sql in checks:
        n = wh.execute(sql).fetchone()[0]
        out.append((name, "customers", "PASS" if n == 0 else "FAIL", f"{label}: {n:,} violations"))
    return out


def run() -> bool:
    src, wh = db.source(), db.warehouse()
    ensure_warehouse(wh)
    state = get_state(wh)
    results: list[tuple[str, str, str, str]] = []
    for t in TABLES:
        if t.strategy in (CHANGE_TRACKING, APPEND_WATERMARK):
            results.append(("freshness", t.name, *freshness(src, t, state.get(t.name))))
        results.append(("row_count", t.name, *row_count(src, wh, t)))
        results.append(("reconcile", t.name, *reconcile(src, wh, t)))
    results += referential(wh)
    results += dedup_invariants(wh)

    now = db.utcnow()
    wh.execute("""CREATE TABLE IF NOT EXISTS etl.check_results (
        checked_at TIMESTAMP, check_name VARCHAR, target VARCHAR, status VARCHAR, detail VARCHAR)""")
    wh.executemany("INSERT INTO etl.check_results VALUES (?, ?, ?, ?, ?)", [(now, *r) for r in results])

    for name, target, status, detail in results:
        print(f"  {status:<5} {name:<22} {target:<24} {detail}")
    failed = [r for r in results if r[2] == "FAIL"]
    print()
    if failed:
        print(f"WAREHOUSE NOT TRUSTWORTHY: {len(failed)} of {len(results)} checks failed")
    else:
        print(f"WAREHOUSE OK: {len(results)} checks, 0 failed")
    return not failed
