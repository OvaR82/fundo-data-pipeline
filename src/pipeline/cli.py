"""Command line entry point: `python -m pipeline <command>` (inside docker: `docker compose run --rm pipeline <command>`)."""
from __future__ import annotations

import argparse
import sys
import time

from . import breakit, checks, db, dedup, load, prevent, report, seed, simulate
from .config import TABLES_BY_NAME


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
    # extension) the delete fails here and nothing else is touched. Dropping the source while
    # keeping an old warehouse is exactly the half-reset that makes every check fail.
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


def step(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 70 - len(title)))


def demo() -> bool:
    step("1. source schema")
    setup()
    step("2. seed synthetic source data (skipped if already seeded)")
    seed.seed(db.source())
    step("3. load (first run = full initial load; later runs = incremental)")
    load.run()
    step("4. simulate one business day in the source (~1% of rows change)")
    simulate.simulate_day(db.source())
    step("5. incremental load: only the changes move")
    load.run()
    step("6. same load again: nothing new -> nothing moves, same result")
    load.run()
    step("7. resolve duplicate customers")
    dedup.run()
    step("8. reconcile source vs warehouse")
    ok = checks.run()
    step("9. transfer measured so far")
    report.run()
    return ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pipeline", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="setup, seed, load, simulate a day, load twice, dedup, check, report")
    sub.add_parser("setup", help="create the source database and schema (idempotent)")
    sub.add_parser("seed", help="fill the source with synthetic data (once)")
    sub.add_parser("simulate", help="apply one business day of changes to the source")
    ld = sub.add_parser("load", help="incremental load source -> warehouse")
    ld.add_argument("--full-refresh", nargs="+", choices=list(TABLES_BY_NAME), default=[],
                    help="re-read these tables completely (repair path)")
    ld.add_argument("--fail-at", choices=list(TABLES_BY_NAME),
                    help="crash on purpose while loading this table, before it commits")
    sub.add_parser("dedup", help="resolve duplicate customers in the warehouse")
    sub.add_parser("check", help="is the warehouse complete and correct? exit code 1 if not")
    br = sub.add_parser("break", help="damage the warehouse on purpose")
    br.add_argument("--scenario", choices=list(breakit.SCENARIOS), required=True)
    sub.add_parser("merge-source", help="load + dedup, apply merges in the source (logged), add the unique index")
    sub.add_parser("report", help="measured transfer: full copy vs incremental")
    qy = sub.add_parser("query", help="run a SQL query against the warehouse and print the result")
    qy.add_argument("sql")
    sub.add_parser("test", help="run the unit tests")
    rs = sub.add_parser("reset", help="drop the source database and the warehouse file")
    rs.add_argument("--yes", action="store_true", required=True)
    a = p.parse_args(argv)

    if a.cmd == "demo":
        return 0 if demo() else 1
    if a.cmd == "setup":
        setup()
    elif a.cmd == "seed":
        seed.seed(db.source())
    elif a.cmd == "simulate":
        simulate.simulate_day(db.source())
    elif a.cmd == "load":
        try:
            load.run(a.full_refresh, a.fail_at)
        except load.InjectedFailure as exc:
            print(f"\nRUN FAILED: {exc}. Tables above it are committed; this one rolled back. "
                  "Run `load` again to resume.")
            return 1
    elif a.cmd == "dedup":
        dedup.run()
    elif a.cmd == "check":
        return 0 if checks.run() else 1
    elif a.cmd == "break":
        breakit.run(a.scenario)
    elif a.cmd == "merge-source":
        prevent.merge_source()
    elif a.cmd == "report":
        report.run()
    elif a.cmd == "query":
        wh = db.warehouse()            # keep the connection alive while the relation prints
        if (rel := wh.sql(a.sql)) is not None:
            rel.show(max_width=200, max_rows=100)
    elif a.cmd == "test":
        import pytest
        return pytest.main(["-q", str(db.ROOT / "tests")])
    elif a.cmd == "reset":
        reset()
    return 0


if __name__ == "__main__":
    sys.exit(main())
