"""Break the warehouse on purpose so the checks have something to catch.

Each scenario damages raw.transactions in a way an incremental load cannot notice by itself
(Change Tracking only reports source changes), which is exactly why the checks exist.
Repair for all of them: `load --full-refresh transactions`.
"""
from __future__ import annotations

from . import db

SCENARIOS = {
    "lose-rows": (
        "a batch of 25 transactions never arrived (deleted from the warehouse)",
        "DELETE FROM raw.transactions WHERE transaction_id IN ("
        "  SELECT transaction_id FROM raw.transactions WHERE NOT _is_deleted ORDER BY transaction_id LIMIT 25 OFFSET 5000)",
    ),
    "zombie": (
        "5 transactions that were deleted in the source are active again in the warehouse",
        "UPDATE raw.transactions SET _is_deleted = false, _deleted_at = NULL WHERE transaction_id IN ("
        "  SELECT transaction_id FROM raw.transactions WHERE _is_deleted ORDER BY transaction_id LIMIT 5)",
    ),
    "amount-drift": (
        "10 transaction amounts silently changed in the warehouse",
        "UPDATE raw.transactions SET amount = amount + 10 WHERE transaction_id IN ("
        "  SELECT transaction_id FROM raw.transactions WHERE NOT _is_deleted ORDER BY transaction_id LIMIT 10 OFFSET 20000)",
    ),
}


def run(scenario: str) -> None:
    description, sql = SCENARIOS[scenario]
    affected = db.warehouse().execute(sql).fetchone()[0]
    print(f"broke the warehouse: {description} -> {affected} rows affected")
    if affected == 0 and scenario == "zombie":
        print("  (no deleted rows yet: run `simulate` and `load` first)")
    print("now run `check` to see it caught, then `load --full-refresh transactions` to repair")
