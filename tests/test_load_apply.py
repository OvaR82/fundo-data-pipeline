"""Warehouse side of the load, without SQL Server: replay is idempotent, failure is atomic."""
from datetime import datetime
from decimal import Decimal

import duckdb
import pytest

from pipeline.config import TABLES_BY_NAME
from pipeline.load import Batch, InjectedFailure, _to_arrow, apply, ensure_warehouse, get_state

T = TABLES_BY_NAME["advances"]
TS = datetime(2026, 1, 1)


def row(aid, status="requested", amount="100.00"):
    return (aid, 1, Decimal(amount), status, TS, TS)


def snapshot(wh):
    return wh.execute("SELECT * EXCLUDE (_loaded_at, _deleted_at) FROM raw.advances ORDER BY 1").fetchall()


@pytest.fixture
def wh():
    con = duckdb.connect(":memory:")
    ensure_warehouse(con)
    apply(con, T, Batch("full", None, 10, _to_arrow(T, [row(1), row(2), row(3)]), []))
    return con


def test_incremental_batch_applied_twice_gives_same_result(wh):
    # Change Tracking re-delivers the same net change set if a run is retried; applying it
    # again must not alter anything.
    batch = Batch("incremental", 10, 11, _to_arrow(T, [row(2, "funded"), row(4)]), [3])
    apply(wh, T, batch)
    first = snapshot(wh)
    apply(wh, T, batch)
    assert snapshot(wh) == first
    active = wh.execute("SELECT advance_id, status FROM raw.advances WHERE NOT _is_deleted ORDER BY 1").fetchall()
    assert active == [(1, "requested"), (2, "funded"), (4, "requested")]


def test_failure_before_commit_changes_nothing(wh):
    before, state = snapshot(wh), get_state(wh)
    batch = Batch("incremental", 10, 11, _to_arrow(T, [row(2, "funded")]), [1])
    with pytest.raises(InjectedFailure):
        apply(wh, T, batch, fail=True)
    assert snapshot(wh) == before
    assert get_state(wh) == state == {"advances": 10}


def test_full_refresh_soft_deletes_rows_missing_from_source(wh):
    apply(wh, T, Batch("full", None, 12, _to_arrow(T, [row(1)]), []))
    rows = wh.execute("SELECT advance_id, _is_deleted FROM raw.advances ORDER BY 1").fetchall()
    assert rows == [(1, False), (2, True), (3, True)]


def test_deleted_row_reinserted_is_active_again(wh):
    apply(wh, T, Batch("incremental", 10, 11, _to_arrow(T, []), [2]))
    apply(wh, T, Batch("incremental", 11, 12, _to_arrow(T, [row(2)]), []))
    assert wh.execute("SELECT _is_deleted FROM raw.advances WHERE advance_id = 2").fetchone() == (False,)
