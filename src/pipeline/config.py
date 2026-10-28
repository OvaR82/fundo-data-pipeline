"""Per-table load strategy. This file is the single place that decides how each table moves."""
from __future__ import annotations

from dataclasses import dataclass

CHANGE_TRACKING = "change_tracking"   # SQL Server Change Tracking -> upsert + soft delete
APPEND_WATERMARK = "append_watermark"  # insert-only table: rows with pk > last loaded pk
FULL_SNAPSHOT = "full_snapshot"        # tiny table: copy everything, diff in the warehouse


@dataclass(frozen=True)
class Table:
    name: str
    pk: str
    strategy: str
    columns: dict[str, str]            # column -> DuckDB type (also the warehouse contract)
    reason: str
    # Reconciliation fingerprint (see checks.py). None -> compare the full key set directly.
    bucket_size: int | None = None
    amount_col: str | None = None      # summed per bucket: catches missed amount updates
    ts_col: str | None = None          # summed (epoch seconds) per bucket: catches missed updates


TABLES: list[Table] = [
    Table(
        name="card_brands", pk="brand_code", strategy=FULL_SNAPSHOT,
        columns={"brand_code": "VARCHAR", "brand_name": "VARCHAR"},
        reason="4 rows. Tracking changes would cost more than copying the table.",
    ),
    Table(
        name="customers", pk="customer_id", strategy=CHANGE_TRACKING,
        columns={
            "customer_id": "INTEGER", "national_id": "VARCHAR", "first_name": "VARCHAR",
            "last_name": "VARCHAR", "email": "VARCHAR", "phone": "VARCHAR", "birth_date": "DATE",
            "created_at": "TIMESTAMP", "updated_at": "TIMESTAMP",
            "merged_into_customer_id": "INTEGER", "dedup_status": "VARCHAR",
        },
        reason="Small and mutable (contact changes, merges). Deletes matter.",
        bucket_size=1_000, ts_col="updated_at",
    ),
    Table(
        name="advances", pk="advance_id", strategy=CHANGE_TRACKING,
        columns={
            "advance_id": "INTEGER", "customer_id": "INTEGER", "amount": "DECIMAL(12,2)",
            "status": "VARCHAR", "created_at": "TIMESTAMP", "updated_at": "TIMESTAMP",
        },
        reason="Status moves requested -> approved -> funded -> paid_off; cancelled ones are deleted.",
        bucket_size=1_000, amount_col="amount", ts_col="updated_at",
    ),
    Table(
        name="cards", pk="card_id", strategy=CHANGE_TRACKING,
        columns={
            "card_id": "INTEGER", "customer_id": "INTEGER", "brand_code": "VARCHAR",
            "last4": "VARCHAR", "status": "VARCHAR", "created_at": "TIMESTAMP",
            "updated_at": "TIMESTAMP",
        },
        reason="Mutable (blocked/expired, re-owned on merge); unused cards get deleted.",
        bucket_size=1_000, ts_col="updated_at",
    ),
    Table(
        name="transactions", pk="transaction_id", strategy=CHANGE_TRACKING,
        columns={
            "transaction_id": "BIGINT", "card_id": "INTEGER", "amount": "DECIMAL(12,2)",
            "merchant": "VARCHAR", "txn_at": "TIMESTAMP", "created_at": "TIMESTAMP",
        },
        reason=(
            "Largest table, mostly append. 'Rarely changed' is not 'never': corrections and "
            "reversals are updates/deletes an insert watermark would silently miss."
        ),
        bucket_size=10_000, amount_col="amount", ts_col="txn_at",
    ),
    Table(
        name="customer_history", pk="history_id", strategy=APPEND_WATERMARK,
        columns={
            "history_id": "BIGINT", "customer_id": "INTEGER", "operation": "VARCHAR",
            "national_id": "VARCHAR", "first_name": "VARCHAR", "last_name": "VARCHAR",
            "email": "VARCHAR", "phone": "VARCHAR", "changed_at": "TIMESTAMP",
        },
        reason="Insert-only by design (trigger-written). Change tracking would be pure overhead.",
        bucket_size=1_000, ts_col="changed_at",
    ),
]

# Present in the source, deliberately not replicated.
EXCLUDED_TABLES = {
    "tmp_marketing_export_2021": "Scratch table with no owner and no reader. Not copied; "
                                 "listed as a drop candidate pending owner sign-off.",
}

TABLES_BY_NAME = {t.name: t for t in TABLES}
