from datetime import date, datetime

from pipeline.dedup import resolve
from pipeline.identity import rut_check_digit


def rut(body: int, style: str = "plain") -> str:
    dv = rut_check_digit(body)
    return f"{body:,}".replace(",", ".") + f"-{dv}" if style == "dotted" else f"{body}{dv}"


def customer(cid, national_id, first="José", last="Pérez", email=None, phone=None,
             dob=date(1990, 1, 1), created=None, money=False):
    created = created or datetime(2024, 1, cid)
    return {"customer_id": cid, "national_id": national_id, "first_name": first, "last_name": last,
            "email": email, "phone": phone, "birth_date": dob, "created_at": created,
            "updated_at": created, "has_money": money}


def reasons(res):
    return sorted(r["reason"] for r in res.reviews)


def test_oldest_survives_when_nobody_has_money():
    res = resolve([customer(1, rut(15234567, "dotted")), customer(2, rut(15234567), first="JOSE", last="PEREZ")])
    assert res.xref == {1: (1, "self"), 2: (1, "rut_match")}


def test_funded_record_survives_even_if_newer():
    res = resolve([customer(1, rut(15234567)), customer(2, rut(15234567), money=True)])
    assert res.xref[1] == (2, "rut_match")
    assert res.xref[2] == (2, "self")


def test_two_funded_records_are_not_merged():
    res = resolve([customer(1, rut(15234567), money=True), customer(2, rut(15234567), money=True)])
    assert res.xref == {1: (1, "self"), 2: (2, "self")}
    assert reasons(res) == ["multiple_funded_same_rut"]


def test_shared_rut_with_different_person_goes_to_review():
    res = resolve([customer(1, rut(15234567)),
                   customer(2, rut(15234567), first="Ana", last="Soto", dob=date(1980, 5, 5))])
    assert res.xref[2] == (2, "self")
    assert reasons(res) == ["same_rut_conflicting_attributes"]


def test_shared_email_only_suggests():
    res = resolve([customer(1, rut(15234567), email="familia@gmail.com"),
                   customer(2, rut(16234568), first="Ana", email="familia@gmail.com")])
    assert res.xref == {1: (1, "self"), 2: (2, "self")}
    assert reasons(res) == ["possible_match:shared_email"]


def test_test_accounts_are_excluded_not_merged_unless_money_moved():
    placeholder = "11.111.111-1"
    res = resolve([customer(1, placeholder, "Test", "User", email="test@fundo.com"),
                   customer(2, placeholder, "QA", "Automation", email="qa1@fundo.com"),
                   customer(3, placeholder, "Test", "Payments", email="test.payments@fundo.com", money=True)])
    assert res.xref[1] == (None, "excluded_test")
    assert res.xref[2] == (None, "excluded_test")
    assert res.xref[3] == (3, "self")
    assert reasons(res) == ["test_pattern_with_money"]


def test_golden_record_takes_freshest_valid_contact():
    old = customer(1, rut(15234567), email="old@gmail.com", phone="000000000")
    new = customer(2, rut(15234567), email="bad@gmail", phone="9 1234 5678", created=datetime(2025, 1, 1))
    res = resolve([old, new])
    (g,) = res.golden
    assert (g["golden_customer_id"], g["email"], g["phone"], g["member_count"]) == (1, "old@gmail.com", "+56912345678", 2)


def test_siblings_sharing_a_mistyped_rut_are_not_merged():
    # Same surname, same RUT, different first name and birth date: two people, one typo.
    res = resolve([customer(1, rut(15234567), first="José", last="Pérez"),
                   customer(2, rut(15234567), first="Javiera", last="Pérez", dob=date(1985, 5, 5))])
    assert res.xref == {1: (1, "self"), 2: (2, "self")}
    assert reasons(res) == ["same_rut_conflicting_attributes"]
