"""Connections to the source (SQL Server) and the warehouse (DuckDB)."""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = ROOT / "sql" / "source"
WAREHOUSE_PATH = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse.duckdb"))


def source(database: str = "fundo"):
    """Autocommit connection: transactions are always explicit (BEGIN TRAN / COMMIT)."""
    import pymssql  # imported lazily so pure-Python tests do not need it

    conn = pymssql.connect(
        server=os.environ.get("MSSQL_HOST", "localhost"),
        port=int(os.environ.get("MSSQL_PORT", "14333")),
        user="sa",
        password=os.environ.get("MSSQL_SA_PASSWORD", "Fundo_Local_2026"),
        database=database,
        autocommit=True,
        tds_version="7.4",
        login_timeout=15,
    )
    # FreeTDS session defaults differ from ODBC/JDBC drivers. Writes to a table with a filtered
    # index or an indexed computed column (added by 010_prevent_dups.sql) require these.
    conn.cursor().execute(
        "SET ANSI_NULLS ON; SET QUOTED_IDENTIFIER ON; SET ANSI_PADDING ON; SET ANSI_WARNINGS ON; "
        "SET ARITHABORT ON; SET CONCAT_NULL_YIELDS_NULL ON; SET NUMERIC_ROUNDABORT OFF;"
    )
    return conn


def run_sql_file(conn, filename: str) -> None:
    """Run a T-SQL script split on GO lines (GO is a client-side separator, not T-SQL)."""
    text = (SQL_DIR / filename).read_text(encoding="utf-8")
    cur = conn.cursor()
    for batch in re.split(r"^\s*GO\s*$", text, flags=re.MULTILINE | re.IGNORECASE):
        if batch.strip():
            cur.execute(batch)


@contextmanager
def transaction(conn):
    """Explicit SQL Server transaction: commits on success, rolls back on any exception."""
    cur = conn.cursor()
    cur.execute("BEGIN TRAN")
    try:
        yield cur
        cur.execute("COMMIT")
    except Exception:
        cur.execute("IF @@TRANCOUNT > 0 ROLLBACK")
        raise


def utcnow() -> datetime:
    """Naive UTC timestamp, the convention for every warehouse metadata column."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def query(conn, sql: str, params=None) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def insert_rows(conn, table: str, columns: list[str], rows: list[tuple], batch: int = 500) -> None:
    """Multi-row INSERT ... VALUES in batches (one round trip per batch instead of per row)."""
    cur = conn.cursor()
    row_tpl = "(" + ",".join(["%s"] * len(columns)) + ")"
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES " + ",".join([row_tpl] * len(chunk))
        cur.execute(sql, tuple(v for r in chunk for v in r))


def warehouse() -> duckdb.DuckDBPyConnection:
    WAREHOUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(WAREHOUSE_PATH))


def table_exists(wh, schema: str, name: str) -> bool:
    return wh.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
        [schema, name],
    ).fetchone()[0] > 0
