from pipeline.identity import (classify_email, classify_phone, corroborates, normalize_rut,
                               rut_check_digit, detect_test_account)


def test_rut_formats_normalize_to_the_same_value():
    body = 15234567
    dv = rut_check_digit(body)
    variants = [f"15.234.567-{dv}", f"15234567-{dv}", f"15234567{dv}", f"  15234567-{dv.lower()} "]
    assert {normalize_rut(v) for v in variants} == {(f"15234567-{dv}", "valid")}


def test_rut_with_wrong_check_digit_does_not_prove_identity():
    body = 12345679
    wrong = "0" if rut_check_digit(body) != "0" else "1"
    assert normalize_rut(f"{body}-{wrong}")[1] == "bad_check_digit"


def test_placeholder_rut_is_not_proof_even_with_a_valid_check_digit():
    assert rut_check_digit(11111111) == "1"
    assert normalize_rut("11.111.111-1")[1] == "placeholder"


def test_rut_garbage():
    assert normalize_rut(None) == (None, "missing")
    assert normalize_rut("N/A") == (None, "malformed")


def test_naive_test_pattern_does_not_catch_real_people():
    assert detect_test_account("Carlos", "Testerman", "carlos.testerman@gmail.com") is None
    assert detect_test_account("Daniela", "Muñoz", "contest.winner@gmail.com") is None
    assert detect_test_account("María", "González", "maria.gonzalez@fundo.com") is None  # staff
    assert detect_test_account("Ana", "Testa", "ana.testa@fundo.com") is None


def test_test_accounts_are_caught():
    assert detect_test_account("Test", "User", "test@fundo.com")
    assert detect_test_account("QA", "Automation", "qa1@fundo.com")
    assert detect_test_account("Cliente", "Demo", "test+3@fundo.com")
    assert detect_test_account("Ana", "Rojas", "qa.ana@fundo.com")
    assert detect_test_account("Dummy", "Account", "dummy.account@example.com")


def test_email_classification():
    assert classify_email(" Juan.Perez@Gmail.com ") == ("juan.perez@gmail.com", "valid")
    assert classify_email("juan.perez@gmail")[1] == "malformed"
    assert classify_email("juan perez@gmail.com")[1] == "malformed"
    assert classify_email("noemail@noemail.com")[1] == "placeholder"
    assert classify_email("")[1] == "missing"


def test_phone_classification():
    assert classify_phone("+56 9 1234 5678") == ("+56912345678", "valid")
    assert classify_phone("912345678") == ("+56912345678", "valid")
    assert classify_phone("000000000")[1] == "placeholder"
    assert classify_phone("999999999")[1] == "placeholder"
    assert classify_phone("12345")[1] == "malformed"
    assert classify_phone("n/a")[1] == "malformed"


def test_corroboration_needs_name_or_birth_date():
    a = {"first_name": "José", "last_name": "Pérez", "birth_date": "1990-01-01"}
    assert corroborates(a, {"first_name": "JOSE", "last_name": "PEREZ", "birth_date": None})
    assert corroborates(a, {"first_name": "Ana", "last_name": "Soto", "birth_date": "1990-01-01"})
    assert not corroborates(a, {"first_name": "Ana", "last_name": "Soto", "birth_date": "1985-05-05"})
