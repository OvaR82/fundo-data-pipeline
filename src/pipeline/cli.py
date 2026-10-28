"""Command line entry point: `python -m pipeline <command>` (inside docker: `docker compose run --rm pipeline <command>`)."""
from __future__ import annotations

import argparse
import sys
import time

from . import db, seed, simulate


def setup() -> None:
    # Right after the container starts, SQL Server already accepts logins (so the healthcheck
    # passes) while it is still recovering its system databases; CREATE DATABASE then fails
    # for a few seconds. Retry instead of asking the user to wait and try again.
    for attempt in range(12):
        try:
            db.run_sql_file(db.source("master"), "000_database.sql")
            break
        except Exception as exc:
            if attempt == 11:
                raise
            print(f"  SQL Server is still starting up ({str(exc).splitlines()[0][:80]}); retrying in 5 s")
            time.sleep(5)
    db.run_sql_file(db.source(), "001_schema.sql")
    print("source schema ready (database fundo, change tracking on)")


def reset() -> None:
    # Warehouse file first: if another program holds it open (a SQL client, the VS Code DuckDB
    # extension) the delete fails here and nothing else is touched.
    for suffix in ("", ".wal"):
        p = db.WAREHOUSE_PATH.with_name(db.WAREHOUSE_PATH.name + suffix)
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            raise SystemExit(f"cannot delete {p.name} ({exc.strerror}): close any tool that has the "
                             "warehouse open, then run reset again. The source database was NOT dropped.")
    master = db.source("master")
    master.cursor().execute("IF DB_ID('fundo') IS NOT NULL BEGIN "
                            "ALTER DATABASE fundo SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE fundo; END")
    print("dropped source database `fundo` and the warehouse file (synthetic data only)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pipeline", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="create the source database and schema (idempotent)")
    sub.add_parser("seed", help="fill the source with synthetic data (once)")
    sub.add_parser("simulate", help="apply one business day of changes to the source")
    sub.add_parser("test", help="run the unit tests")
    rs = sub.add_parser("reset", help="drop the source database and the warehouse file")
    rs.add_argument("--yes", action="store_true", required=True)
    a = p.parse_args(argv)

    if a.cmd == "setup":
        setup()
    elif a.cmd == "seed":
        seed.seed(db.source())
    elif a.cmd == "simulate":
        simulate.simulate_day(db.source())
    elif a.cmd == "test":
        import pytest
        return pytest.main(["-q", str(db.ROOT / "tests")])
    elif a.cmd == "reset":
        reset()
    return 0


if __name__ == "__main__":
    sys.exit(main())
