"""Identity rules: normalization, validation and test-account detection. Pure functions.

Two kinds of fields (see SOLUTION.md):
  * PROVES identity  : a national ID (Chilean RUT) with a valid check digit, that is not a
                       placeholder, corroborated by name or birth date.
  * SUGGESTS a match : email, phone, name, name + birth date. Shared by families, households,
                       company mailboxes, recycled numbers and namesakes -> review, never merge.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# ---------------------------------------------------------------- national id (RUT)

def rut_check_digit(body: int) -> str:
    """Modulo-11 check digit of a Chilean RUT."""
    total, factor = 0, 2
    for digit in reversed(str(body)):
        total += int(digit) * factor
        factor = 2 if factor == 7 else factor + 1
    rest = 11 - total % 11
    return {11: "0", 10: "K"}.get(rest, str(rest))


def normalize_rut(raw: str | None) -> tuple[str | None, str]:
    """-> (canonical 'BODY-DV' or None, status). status: valid | bad_check_digit |
    placeholder | malformed | missing."""
    if raw is None or not str(raw).strip():
        return None, "missing"
    s = re.sub(r"[.\-\s]", "", str(raw)).upper()
    if not re.fullmatch(r"\d{6,9}[\dK]", s):
        return None, "malformed"
    body, dv = s[:-1].lstrip("0"), s[-1]
    if not body:
        return None, "malformed"
    canonical = f"{body}-{dv}"
    # 11.111.111-1 has a *valid* check digit, so the check digit alone does not catch it.
    if len(set(body)) == 1 or body in {"12345678", "123456789"}:
        return canonical, "placeholder"
    if rut_check_digit(int(body)) != dv:
        return canonical, "bad_check_digit"
    return canonical, "valid"


# ---------------------------------------------------------------- email

_EMAIL_RE = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")
_PLACEHOLDER_LOCALS = {"noemail", "sinmail", "sincorreo", "nomail", "no", "na", "none", "null", "xxx"}


def classify_email(raw: str | None) -> tuple[str | None, str]:
    """-> (lower-cased email or None, status). status: valid | placeholder | malformed | missing.
    Fix nothing that needs a guess: 'juan@gmail' is not silently turned into 'juan@gmail.com'."""
    if raw is None or not str(raw).strip():
        return None, "missing"
    e = str(raw).strip().lower()
    if not _EMAIL_RE.fullmatch(e):
        return None, "malformed"
    if e.split("@")[0] in _PLACEHOLDER_LOCALS:
        return None, "placeholder"
    return e, "valid"


# ---------------------------------------------------------------- phone (Chilean mobile)

def classify_phone(raw: str | None) -> tuple[str | None, str]:
    """-> (E.164 '+569XXXXXXXX' or None, status). status: valid | placeholder | malformed | missing.
    Only unambiguous reformatting (spaces, dashes, missing +56) counts as valid."""
    if raw is None or not str(raw).strip():
        return None, "missing"
    digits = re.sub(r"\D", "", str(raw))
    if digits and len(set(digits)) == 1:
        return None, "placeholder"
    if len(digits) == 11 and digits.startswith("56"):
        digits = digits[2:]
    if len(digits) == 9 and digits.startswith("9"):
        if len(set(digits[1:])) == 1:
            return None, "placeholder"
        return f"+56{digits}", "valid"
    return None, "malformed"


# ---------------------------------------------------------------- names

def fold(text: str | None) -> str:
    """Lower-case, strip accents, collapse whitespace: 'José  PÉREZ' -> 'jose perez'."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return " ".join(t.lower().split())


def names_similar(first_a, last_a, first_b, last_b) -> bool:
    a, b = fold(f"{first_a} {last_a}"), fold(f"{first_b} {last_b}")
    if not a or not b:
        return False
    # Accents, case and small typos pass; a different first name with the same surname does not
    # (siblings share surnames, and a shared RUT between them is a typo, not one person).
    return SequenceMatcher(None, a, b).ratio() >= 0.85


def corroborates(a: dict, b: dict) -> bool:
    """Second, independent signal required before a shared RUT is accepted as the same person.
    A shared RUT with a different name AND a different birth date is more likely a typo."""
    if a.get("birth_date") and a.get("birth_date") == b.get("birth_date"):
        return True
    return names_similar(a["first_name"], a["last_name"], b["first_name"], b["last_name"])


# ---------------------------------------------------------------- test accounts

INTERNAL_DOMAINS = {"fundo.com"}
NON_ROUTABLE_DOMAINS = {"example.com", "example.org", "test.com", "mailinator.com"}
_TEST_WORDS = r"(test|testing|qa|prueba|pruebas|dummy|demo|fake)"
_TEST_MAILBOX = re.compile(rf"^{_TEST_WORDS}\d*([._+-].*)?$")
_TEST_NAME_TOKENS = {"test", "testing", "prueba", "dummy", "fake"}


def detect_test_account(first_name, last_name, email) -> str | None:
    """Return why a customer looks like test data, or None.

    Deliberately narrow, because the naive pattern (LIKE '%test%' or '@fundo.com') catches
    real people:
      * 'Testerman' is a surname: we match whole name tokens, never substrings.
      * Staff use @fundo.com for genuine accounts: an internal domain only counts when the
        mailbox itself starts with a test word (test@, qa1@, test.payments@).
    The caller still refuses to exclude anyone with a funded / paid-off advance.
    """
    e = (email or "").strip().lower()
    if "@" in e:
        local, domain = e.rsplit("@", 1)
        if domain in NON_ROUTABLE_DOMAINS:
            return f"non_routable_domain:{domain}"
        if domain in INTERNAL_DOMAINS and _TEST_MAILBOX.match(local):
            return "internal_test_mailbox"
    tokens = set(re.findall(r"[a-z]+", fold(f"{first_name} {last_name}")))
    if tokens & _TEST_NAME_TOKENS:
        return "test_word_in_name"
    return None
