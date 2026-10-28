"""Incremental load: source (SQL Server) -> warehouse (DuckDB).

Reliability model, per table:
  1. Extract inside one SNAPSHOT transaction on the source: the change set and the version it
     is valid up to are read from the same consistent point in time.
  2. Apply inside one DuckDB transaction: upserts + soft deletes + the new checkpoint in
     etl.state. Either all of it commits or none of it does.
  3. A run that dies anywhere leaves every table either fully at the old checkpoint or fully at
     the new one. Re-running reads the same change set again; applying net changes twice gives
     the same rows (upsert by key, delete-if-active), so the retry is safe.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass

import pyarrow as pa

from . import db
from .config import APPEND_WATERMARK, CHANGE_TRACKING, FULL_SNAPSHOT, TABLES, Table

META_COLUMNS = "_is_deleted BOOLEAN NOT NULL DEFAULT false, _deleted_at TIMESTAMP, _loaded_at TIMESTAMP NOT NULL"


class InjectedFailure(RuntimeError):
    """Raised on purpose by `load --fail-at <table>` to demonstrate recovery."""


def arrow_type(duck_type: str) -> pa.DataType:
    if m := re.fullmatch(r"DECIMAL\((\d+),(\d+)\)", duck_type):
        return pa.decimal128(int(m[1]), int(m[2]))
    return {"INTEGER": pa.int32(), "BIGINT": pa.int64(), "VARCHAR": pa.string(),
            "DATE": pa.date32(), "TIMESTAMP": pa.timestamp("us")}[duck_type]


def ensure_warehouse(wh) -> None:
    wh.execute("CREATE SCHEMA IF NOT EXISTS raw; CREATE SCHEMA IF NOT EXISTS etl;")
    for t in TABLES:
        cols = ", ".join(f"{c} {ty}" for c, ty in t.columns.items())
        wh.execute(f"CREATE TABLE IF NOT EXISTS raw.{t.name} ({cols}, {META_COLUMNS}, PRIMARY KEY ({t.pk}))")
    wh.execute("""
        CREATE TABLE IF NOT EXISTS etl.state (
            table_name VARCHAR PRIMARY KEY, last_version BIGINT, updated_at TIMESTAMP NOT NULL);
        CREATE TABLE IF NOT EXISTS etl.runs (
            run_id VARCHAR, table_name VARCHAR, strategy VARCHAR, mode VARCHAR,
            from_version BIGINT, to_version BIGINT, rows_upserted BIGINT, rows_deleted BIGINT,
            bytes_extracted BIGINT, seconds DOUBLE, finished_at TIMESTAMP);
    """)


def get_state(wh) -> dict[str, int | None]:
    return dict(wh.execute("SELECT table_name, last_version FROM etl.state").fetchall())


# ------------------------------------------------------------------------------ extract

@dataclass
class Batch:
    mode: str                  # "full" (re-read everything) or "incremental"
    from_version: int | None
    to_version: int | None
    upserts: pa.Table          # rows that exist in the source now (insert or update)
    deleted_keys: list         # keys gone from the source (incremental mode only)

    @property
    def nbytes(self) -> int:
        return self.upserts.nbytes + 8 * len(self.deleted_keys)


def _to_arrow(t: Table, rows: list[tuple]) -> pa.Table:
    schema = pa.schema([(c, arrow_type(ty)) for c, ty in t.columns.items()])
    return pa.table({c: pa.array([r[i] for r in rows], type=schema.field(c).type)
                     for i, c in enumerate(t.columns)}, schema=schema)


def extract(src, t: Table, last_version: int | None) -> Batch:
    cols = ", ".join(t.columns)
    if t.strategy == FULL_SNAPSHOT:
        rows = db.query(src, f"SELECT {cols} FROM dbo.{t.name}")
        return Batch("full", None, None, _to_arrow(t, rows), [])

    if t.strategy == APPEND_WATERMARK:
        source_max = db.query(src, f"SELECT COALESCE(MAX({t.pk}), -1) FROM dbo.{t.name}")[0][0]
        # Full read when never loaded, or when the source has fewer rows than we already loaded
        # (it was rebuilt or restored): only a full read, which also soft-deletes what is gone,
        # can resynchronise.
        full = last_version is None or last_version > source_max
        start = -1 if full else last_version
        rows = db.query(src, f"SELECT {cols} FROM dbo.{t.name} WHERE {t.pk} > %s ORDER BY {t.pk}", (start,))
        to_version = rows[-1][list(t.columns).index(t.pk)] if rows else start
        return Batch("full" if full else "incremental", last_version, to_version, _to_arrow(t, rows), [])

    assert t.strategy == CHANGE_TRACKING
    src.cursor().execute("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")
    with db.transaction(src) as cur:
        cur.execute("SELECT CHANGE_TRACKING_CURRENT_VERSION(), CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID(%s))",
                    (f"dbo.{t.name}",))
        current, min_valid = cur.fetchone()
        if min_valid is None:
            raise RuntimeError(f"change tracking is not enabled on dbo.{t.name}")
        if last_version is None or last_version < min_valid or last_version > current:
            # First load, explicit full refresh, the change history we need was cleaned up, or
            # the source's version went BACKWARDS (database rebuilt or restored from a backup):
            # the checkpoint is meaningless, so re-read everything. The full apply also
            # soft-deletes whatever the rebuilt source no longer has.
            cur.execute(f"SELECT {cols} FROM dbo.{t.name}")
            return Batch("full", last_version, current, _to_arrow(t, cur.fetchall()), [])
        cur.execute(f"""
            SELECT ct.{t.pk}, CASE WHEN s.{t.pk} IS NULL THEN 1 ELSE 0 END, {", ".join("s." + c for c in t.columns)}
            FROM CHANGETABLE(CHANGES dbo.{t.name}, %s) AS ct
            LEFT JOIN dbo.{t.name} AS s ON s.{t.pk} = ct.{t.pk}""", (last_version,))
        rows = cur.fetchall()
        # Row present in the snapshot -> upsert; row absent -> it was deleted.
        return Batch("incremental", last_version, current,
                     _to_arrow(t, [r[2:] for r in rows if r[1] == 0]),
                     [r[0] for r in rows if r[1] == 1])


# ------------------------------------------------------------------------------ apply

def apply(wh, t: Table, batch: Batch, fail: bool = False) -> int:
    """Write one batch atomically. Returns the number of rows soft-deleted."""
    now = db.utcnow()
    cols = list(t.columns)
    col_list = ", ".join(cols)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != t.pk)
    wh.register("stg", batch.upserts)
    wh.register("gone", pa.table({t.pk: pa.array(batch.deleted_keys, arrow_type(t.columns[t.pk]))}))
    wh.begin()
    try:
        wh.execute(f"""
            INSERT INTO raw.{t.name} ({col_list}, _is_deleted, _deleted_at, _loaded_at)
            SELECT {col_list}, false, NULL, ? FROM stg
            ON CONFLICT ({t.pk}) DO UPDATE SET {updates}, _is_deleted = false, _deleted_at = NULL,
                _loaded_at = EXCLUDED._loaded_at
        """, [now])
        # Full read: anything active that is not in it no longer exists in the source.
        # Incremental: Change Tracking told us exactly which keys are gone.
        gone_filter = f"{t.pk} NOT IN (SELECT {t.pk} FROM stg)" if batch.mode == "full" \
            else f"{t.pk} IN (SELECT {t.pk} FROM gone)"
        deleted = wh.execute(f"UPDATE raw.{t.name} SET _is_deleted = true, _deleted_at = ? "
                             f"WHERE NOT _is_deleted AND {gone_filter}", [now]).fetchone()[0]
        wh.execute("""
            INSERT INTO etl.state VALUES (?, ?, ?)
            ON CONFLICT (table_name) DO UPDATE SET last_version = EXCLUDED.last_version,
                                                   updated_at = EXCLUDED.updated_at
        """, [t.name, batch.to_version, now])
        if fail:
            raise InjectedFailure(f"injected failure while loading {t.name} (before commit)")
        wh.commit()
        return deleted
    except Exception:
        wh.rollback()
        raise


# ------------------------------------------------------------------------------ run

def run(full_refresh: list[str] | None = None, fail_at: str | None = None) -> None:
    src, wh = db.source(), db.warehouse()
    ensure_warehouse(wh)
    state = get_state(wh)
    run_id = uuid.uuid4().hex[:8]
    full_refresh = set(full_refresh or [])
    print(f"load run {run_id}")
    print(f"  {'table':<17} {'strategy':<17} {'mode':<12} {'versions':<15} {'upserted':>9} {'deleted':>8} {'KB':>8}")
    for t in TABLES:
        last = None if t.name in full_refresh else state.get(t.name)
        started = time.perf_counter()
        try:
            batch = extract(src, t, last)
            deleted = apply(wh, t, batch, fail=(t.name == fail_at))
        except Exception as exc:   # the run stops here; tables above are committed, this one is not
            print(f"  {t.name:<17} {t.strategy:<17} FAILED: {exc}")
            raise
        seconds = time.perf_counter() - started
        wh.execute("INSERT INTO etl.runs VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            run_id, t.name, t.strategy, batch.mode, batch.from_version, batch.to_version,
            batch.upserts.num_rows, deleted, batch.nbytes, seconds, db.utcnow()])
        print(f"  {t.name:<17} {t.strategy:<17} {batch.mode:<12} {f'{batch.from_version}->{batch.to_version}':<15} "
              f"{batch.upserts.num_rows:>9,} {deleted:>8,} {batch.nbytes / 1024:>8,.1f}")
