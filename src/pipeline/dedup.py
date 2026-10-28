"""Duplicate-customer resolution in the warehouse. Non-destructive: raw.* is never modified.

Outputs (fully rebuilt on every run, atomically -> idempotent by construction):
  core.customer_quality     one row per customer: normalized id/email/phone, statuses, test flag
  core.customer_xref        customer_id -> golden_customer_id (NULL = excluded test data)
  core.dedup_review_queue   cases a human must decide; never merged automatically
  core.dim_customer         one row per real person (golden record)
  core.cards_resolved       view: every card with the golden customer it belongs to
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import pyarrow as pa

from . import db
from .identity import (classify_email, classify_phone, corroborates, fold, normalize_rut,
                       detect_test_account)

RECOMMENDATION = {
    "multiple_funded_same_rut": "Do not merge: both records hold money. Ops confirms identity in the "
                                "core system and consolidates there; until then report exposure per "
                                "group, never per record.",
    "same_rut_conflicting_attributes": "Same RUT but name and birth date disagree: likely a typo in one "
                                       "record. Verify the ID document and correct the RUT at the source.",
    "test_pattern_with_money": "Looks like test data but holds a funded/paid-off advance: real money "
                               "moved. Kept as a customer until finance confirms.",
    "possible_match:shared_email": "Suggestive only (families share inboxes). Merge only if a proving "
                                   "field confirms.",
    "possible_match:shared_phone": "Suggestive only (households, recycled numbers).",
    "possible_match:same_name_and_birth_date": "Suggestive only (namesakes exist). Check the ID document.",
}


@dataclass
class Result:
    quality: list[dict] = field(default_factory=list)
    xref: dict[int, tuple[int | None, str]] = field(default_factory=dict)
    reviews: list[dict] = field(default_factory=list)
    golden: list[dict] = field(default_factory=list)


def resolve(customers: list[dict]) -> Result:
    """customers: dicts with customer_id, national_id, first_name, last_name, email, phone,
    birth_date, created_at, updated_at, has_money."""
    res = Result()
    by_id = {c["customer_id"]: c for c in customers}
    q = {}
    for c in customers:
        rut, rut_status = normalize_rut(c["national_id"])
        email, email_status = classify_email(c["email"])
        phone, phone_status = classify_phone(c["phone"])
        q[c["customer_id"]] = row = {
            "customer_id": c["customer_id"], "national_id_norm": rut, "rut_status": rut_status,
            "email_norm": email, "email_status": email_status, "phone_e164": phone,
            "phone_status": phone_status,
            "test_reason": detect_test_account(c["first_name"], c["last_name"], c["email"]),
        }
        res.quality.append(row)

    def review(reason: str, ids, key) -> None:
        ids = sorted(ids)
        res.reviews.append({"review_id": f"{reason}:{'-'.join(map(str, ids))}", "reason": reason,
                            "customer_ids": ",".join(map(str, ids)), "match_key": key,
                            "recommendation": RECOMMENDATION[reason]})

    # 1. Test data is excluded, not merged. Unless real money moved: then a human decides.
    for c in customers:
        cid, reason = c["customer_id"], q[c["customer_id"]]["test_reason"]
        if reason:
            if c["has_money"]:
                review("test_pattern_with_money", [cid], reason)
            else:
                res.xref[cid] = (None, "excluded_test")

    # 2. Proving field: same valid RUT (+ corroborating name or birth date). Records already
    #    merged in the source resolve the same way, by their RUT: no special rule is needed.
    groups = defaultdict(list)
    for c in customers:
        cid = c["customer_id"]
        if cid not in res.xref and q[cid]["rut_status"] == "valid":
            groups[q[cid]["national_id_norm"]].append(c)
    for rut, members in groups.items():
        if len(members) < 2:
            continue
        funded = [m for m in members if m["has_money"]]
        if len(funded) > 1:   # business rule: both untouchable -> do not guess
            review("multiple_funded_same_rut", [m["customer_id"] for m in members], rut)
            continue
        # Survivor: the record holding money; otherwise the oldest (lowest id breaks ties).
        survivor = funded[0] if funded else min(members, key=lambda m: (m["created_at"], m["customer_id"]))
        for m in members:
            if m is survivor:
                continue
            if corroborates(m, survivor):
                res.xref[m["customer_id"]] = (survivor["customer_id"], "rut_match")
            else:
                review("same_rut_conflicting_attributes", [survivor["customer_id"], m["customer_id"]], rut)

    for c in customers:
        res.xref.setdefault(c["customer_id"], (c["customer_id"], "self"))

    # 3. Suggestive fields: report possible matches across different golden records, never merge.
    golden_of = {cid: g for cid, (g, _) in res.xref.items()}
    keys = {
        "possible_match:shared_email": lambda c: q[c["customer_id"]]["email_norm"],
        "possible_match:shared_phone": lambda c: q[c["customer_id"]]["phone_e164"],
        "possible_match:same_name_and_birth_date": lambda c: (
            f"{fold(c['first_name'])} {fold(c['last_name'])}|{c['birth_date']}" if c["birth_date"] else None),
    }
    already = [set(map(int, r["customer_ids"].split(","))) for r in res.reviews]
    for reason, key_fn in keys.items():
        buckets = defaultdict(set)
        for c in customers:
            g = golden_of[c["customer_id"]]
            if g is not None and (k := key_fn(c)):
                buckets[k].add(g)
        for k, goldens in buckets.items():
            if len(goldens) > 1 and not any(goldens <= ids for ids in already):
                review(reason, goldens, k)

    # 4. Golden records: survivor's identity, freshest valid contact data of the group.
    members_of = defaultdict(list)
    for cid, (g, _) in res.xref.items():
        if g is not None:
            members_of[g].append(by_id[cid])
    for g, members in members_of.items():
        s = by_id[g]
        newest_first = sorted(members, key=lambda m: m["updated_at"], reverse=True)
        res.golden.append({
            "golden_customer_id": g,
            "national_id_norm": q[g]["national_id_norm"],
            "first_name": s["first_name"], "last_name": s["last_name"], "birth_date": s["birth_date"],
            "email": next((q[m["customer_id"]]["email_norm"] for m in newest_first
                           if q[m["customer_id"]]["email_norm"]), None),
            "phone": next((q[m["customer_id"]]["phone_e164"] for m in newest_first
                           if q[m["customer_id"]]["phone_e164"]), None),
            "member_count": len(members),
            "member_ids": ",".join(str(m["customer_id"]) for m in sorted(members, key=lambda m: m["customer_id"])),
            "has_funded_advance": any(m["has_money"] for m in members),
            "first_seen_at": min(m["created_at"] for m in members),
        })
    return res


# ------------------------------------------------------------------------------ IO

def run() -> Result:
    wh = db.warehouse()
    rows = wh.execute("""
        SELECT c.customer_id, c.national_id, c.first_name, c.last_name, c.email, c.phone, c.birth_date,
               c.created_at, c.updated_at,
               EXISTS (SELECT 1 FROM raw.advances a WHERE a.customer_id = c.customer_id
                       AND NOT a._is_deleted AND a.status IN ('funded', 'paid_off')) AS has_money
        FROM raw.customers c WHERE NOT c._is_deleted ORDER BY c.customer_id
    """)
    names = [d[0] for d in rows.description]
    customers = [dict(zip(names, r)) for r in rows.fetchall()]
    res = resolve(customers)

    # Explicit schemas: a column that happens to be all NULL must not change type between runs.
    S, I, B, D, T = pa.string(), pa.int32(), pa.bool_(), pa.date32(), pa.timestamp("us")
    tables = {
        "customer_quality": pa.Table.from_pylist(res.quality, schema=pa.schema([
            ("customer_id", I), ("national_id_norm", S), ("rut_status", S), ("email_norm", S),
            ("email_status", S), ("phone_e164", S), ("phone_status", S), ("test_reason", S)])),
        "customer_xref": pa.table({
            "customer_id": pa.array(list(res.xref), I),
            "golden_customer_id": pa.array([g for g, _ in res.xref.values()], I),
            "rule": pa.array([r for _, r in res.xref.values()], S)}),
        "dedup_review_queue": pa.Table.from_pylist(res.reviews, schema=pa.schema([
            ("review_id", S), ("reason", S), ("customer_ids", S), ("match_key", S), ("recommendation", S)])),
        "dim_customer": pa.Table.from_pylist(res.golden, schema=pa.schema([
            ("golden_customer_id", I), ("national_id_norm", S), ("first_name", S), ("last_name", S),
            ("birth_date", D), ("email", S), ("phone", S), ("member_count", I), ("member_ids", S),
            ("has_funded_advance", B), ("first_seen_at", T)])),
    }
    wh.begin()   # swap all outputs together: readers never see a half-built set
    try:
        wh.execute("CREATE SCHEMA IF NOT EXISTS core")
        for name, tbl in tables.items():
            wh.register("_src", tbl)
            wh.execute(f"CREATE OR REPLACE TABLE core.{name} AS SELECT * FROM _src")
            wh.unregister("_src")
        wh.execute("""
            CREATE OR REPLACE VIEW core.cards_resolved AS
            SELECT k.card_id, k.customer_id AS source_customer_id, x.golden_customer_id, x.rule,
                   k.brand_code, k.last4, k.status
            FROM raw.cards k LEFT JOIN core.customer_xref x ON x.customer_id = k.customer_id
            WHERE NOT k._is_deleted
        """)
        wh.commit()
    except Exception:
        wh.rollback()
        raise
    print_summary(wh)
    return res


def print_summary(wh) -> None:
    def show(title, sql):
        print(f"  {title}")
        for k, v in wh.execute(sql).fetchall():
            print(f"    {k:<42} {v:>6,}")
    show("customer -> golden mapping (rule)", "SELECT rule, count(*) FROM core.customer_xref GROUP BY 1 ORDER BY 1")
    show("review queue (not merged, needs a human)",
         "SELECT reason, count(*) FROM core.dedup_review_queue GROUP BY 1 ORDER BY 1")
    show("national id status", "SELECT rut_status, count(*) FROM core.customer_quality GROUP BY 1 ORDER BY 1")
    show("email status", "SELECT email_status, count(*) FROM core.customer_quality GROUP BY 1 ORDER BY 1")
    show("phone status", "SELECT phone_status, count(*) FROM core.customer_quality GROUP BY 1 ORDER BY 1")
    golden = wh.execute("SELECT count(*), sum(member_count) FROM core.dim_customer").fetchone()
    moved = wh.execute("SELECT count(*) FROM core.cards_resolved "
                       "WHERE golden_customer_id <> source_customer_id").fetchone()[0]
    print(f"  {golden[1]:,} customer records -> {golden[0]:,} golden customers; "
          f"{moved:,} cards re-attributed to a surviving customer")
