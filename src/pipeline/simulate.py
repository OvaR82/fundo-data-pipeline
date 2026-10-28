"""One simulated business day on the source: ~1% of rows inserted, updated or deleted.

Plays the role of the operational application. Also tries to register an existing person
again with a re-typed RUT, which is how duplicates are born.
"""
from __future__ import annotations

import random

from . import db
from .identity import normalize_rut
from .seed import Generator, fmt_rut

# (label, statement). Each statement picks its rows at random with ORDER BY NEWID().
DAY = [
    ("customers  update", """
        UPDATE dbo.customers SET
            phone = '+569' + RIGHT('0000000' + CAST(ABS(CHECKSUM(NEWID())) % 100000000 AS VARCHAR(8)), 8),
            updated_at = SYSUTCDATETIME()
        WHERE customer_id IN (SELECT TOP (25) customer_id FROM dbo.customers ORDER BY NEWID())"""),
    ("customers  delete", """
        DELETE FROM dbo.customers WHERE customer_id IN (
            SELECT TOP (3) c.customer_id FROM dbo.customers c
            WHERE c.dedup_status IS NULL   -- abandoned sign-ups only, never a merge participant
              AND NOT EXISTS (SELECT 1 FROM dbo.advances a WHERE a.customer_id = c.customer_id)
              AND NOT EXISTS (SELECT 1 FROM dbo.cards k WHERE k.customer_id = c.customer_id)
              AND NOT EXISTS (SELECT 1 FROM dbo.customers l WHERE l.merged_into_customer_id = c.customer_id)
            ORDER BY NEWID())"""),
    ("advances   insert", """
        INSERT dbo.advances (customer_id, amount, status)
        SELECT TOP (30) customer_id, (ABS(CHECKSUM(NEWID())) % 850 + 50) * 1000, 'requested'
        FROM dbo.customers WHERE dedup_status IS NULL ORDER BY NEWID()"""),
    ("advances   update", """
        UPDATE dbo.advances SET
            status = CASE status WHEN 'requested' THEN 'approved' WHEN 'approved' THEN 'funded'
                                 ELSE 'paid_off' END,
            updated_at = SYSUTCDATETIME()
        WHERE advance_id IN (SELECT TOP (40) advance_id FROM dbo.advances
                             WHERE status IN ('requested', 'approved', 'funded') ORDER BY NEWID())"""),
    ("advances   delete", """
        DELETE FROM dbo.advances WHERE advance_id IN (
            SELECT TOP (5) advance_id FROM dbo.advances WHERE status IN ('requested', 'rejected')
            ORDER BY NEWID())"""),
    ("cards      insert", """
        INSERT dbo.cards (customer_id, brand_code, last4, status)
        SELECT TOP (20) customer_id, 'VISA', RIGHT('000' + CAST(ABS(CHECKSUM(NEWID())) % 10000 AS VARCHAR(4)), 4), 'active'
        FROM dbo.customers WHERE dedup_status IS NULL ORDER BY NEWID()"""),
    ("cards      update", """
        UPDATE dbo.cards SET status = 'blocked', updated_at = SYSUTCDATETIME()
        WHERE card_id IN (SELECT TOP (15) card_id FROM dbo.cards WHERE status = 'active' ORDER BY NEWID())"""),
    ("cards      delete", """
        DELETE FROM dbo.cards WHERE card_id IN (
            SELECT TOP (3) c.card_id FROM dbo.cards c
            WHERE NOT EXISTS (SELECT 1 FROM dbo.transactions t WHERE t.card_id = c.card_id)
            ORDER BY NEWID())"""),
    ("transactions insert", """
        INSERT dbo.transactions (card_id, amount, merchant, txn_at)
        SELECT TOP (1000) card_id, (ABS(CHECKSUM(NEWID())) % 99000 + 1000) / 100.0, N'Lider',
               DATEADD(MINUTE, -(ABS(CHECKSUM(NEWID())) % 1440), SYSUTCDATETIME())
        FROM dbo.cards CROSS JOIN (VALUES (1), (2)) v(x)
        WHERE status = 'active' ORDER BY NEWID()"""),
    ("transactions update", """
        UPDATE dbo.transactions SET amount = amount + 1.00
        WHERE transaction_id IN (SELECT TOP (10) transaction_id FROM dbo.transactions ORDER BY NEWID())"""),
    ("transactions delete", """
        DELETE FROM dbo.transactions
        WHERE transaction_id IN (SELECT TOP (5) transaction_id FROM dbo.transactions ORDER BY NEWID())"""),
]


def simulate_day(conn) -> None:
    import pymssql

    rng = random.Random()
    gen = Generator()
    gen.rng = rng   # new people every day (the seed generator is deterministic on purpose)
    with db.transaction(conn) as cur:
        new_people = [gen.person() for _ in range(25)]
        db.insert_rows(conn, "dbo.customers", ["national_id", "first_name", "last_name", "email", "phone", "birth_date"],
                       [(fmt_rut(p["body"], rng.choice(["dotted", "plain"])), p["first"], p["last"], p["email"],
                         p["phone_digits"], p["dob"]) for p in new_people])
        print(f"  {'customers  insert':<20} {len(new_people):>5} rows")
        for label, sql in DAY:
            cur.execute(sql)
            print(f"  {label:<20} {cur.rowcount:>5} rows")

    # The application registers an existing, active person again, RUT typed differently.
    # Its own statement, outside the day's transaction: a rejection must not undo the day.
    cur = conn.cursor()
    candidates = db.query(conn, """
        SELECT TOP (50) national_id, first_name, last_name, email, phone, birth_date
        FROM dbo.customers WHERE national_id IS NOT NULL AND dedup_status IS NULL ORDER BY NEWID()""")
    for nid, first, last, email, phone, dob in candidates:
        canonical, status = normalize_rut(nid)
        if status != "valid":
            continue
        retyped = canonical.replace("-", "") if "-" in str(nid) else canonical
        try:
            cur.execute("INSERT dbo.customers (national_id, first_name, last_name, email, phone, birth_date) "
                        "VALUES (%s, %s, %s, %s, %s, %s)", (retyped, first, last, email, phone, dob))
            print(f"  duplicate attempt    RUT {retyped!r} ({first} {last}) -> CREATED a duplicate "
                  "(no prevention in the source yet)")
        except pymssql.IntegrityError:
            print(f"  duplicate attempt    RUT {retyped!r} ({first} {last}) -> REJECTED by "
                  "ux_customers_national_id_norm")
        break
