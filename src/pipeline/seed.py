"""Synthetic source data with the traps the brief asks for. Deterministic (fixed random seed).

Volumes: ~5k customers, ~5k advances, ~5.5k cards, ~95k transactions.
Traps planted (counts printed at the end):
  * duplicate people: same RUT typed in different formats, names with/without accents
      - some groups where the NEWER record holds the funded/paid-off advance
      - 2 groups where BOTH records hold one (must not be auto-merged)
  * look-alikes that are NOT the same person: shared family email, shared household phone,
    namesakes with the same birth date, a RUT typo that collides with someone else's RUT
  * test accounts (test@fundo.com, qa1@fundo.com, ...), one of them with a funded advance
  * real people the naive test filter catches: surname Testerman, contest.winner@gmail.com,
    staff with @fundo.com addresses and real activity
  * malformed / placeholder emails and phones, RUTs with a wrong check digit or missing
"""
from __future__ import annotations

import random
from collections import Counter
from datetime import date, datetime, timedelta

from . import db
from .identity import fold, normalize_rut, rut_check_digit

SEED = 42
START = datetime(2023, 10, 1)
ANCHOR = datetime(2026, 9, 30)

FIRST = ["José", "María", "Juan", "Camila", "Pedro", "Valentina", "Diego", "Javiera", "Matías",
         "Catalina", "Sebastián", "Fernanda", "Nicolás", "Constanza", "Felipe", "Daniela", "Tomás",
         "Francisca", "Benjamín", "Antonia", "Vicente", "Isidora", "Cristóbal", "Martina", "Ignacio",
         "Sofía", "Joaquín", "Paula", "Andrés", "Carolina", "Rodrigo", "Gabriela", "Álvaro", "Ana",
         "Luis", "Rocío", "Pablo", "Josefa", "Gonzalo", "Bárbara"]
LAST = ["González", "Muñoz", "Rojas", "Díaz", "Pérez", "Soto", "Contreras", "Silva", "Martínez",
        "Sepúlveda", "Morales", "Rodríguez", "López", "Fuentes", "Hernández", "Torres", "Araya",
        "Flores", "Espinoza", "Valenzuela", "Castillo", "Tapia", "Reyes", "Gutiérrez", "Castro",
        "Pizarro", "Álvarez", "Vásquez", "Sánchez", "Fernández", "Ramírez", "Carrasco", "Gómez",
        "Cortés", "Herrera", "Núñez", "Jara", "Vergara", "Rivera", "Figueroa"]
DOMAINS = ["gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "live.cl"]
BRANDS = [("VISA", "Visa"), ("MC", "Mastercard"), ("AMEX", "American Express"), ("RDC", "Redcompra")]
MONEY = ("funded", "paid_off")


def fmt_rut(body: int, style: str, dv: str | None = None) -> str:
    dv = dv or rut_check_digit(body)
    if style == "dotted":
        return f"{body:,}".replace(",", ".") + f"-{dv}"
    if style == "compact":
        return f"{body}{dv}"
    if style == "padded":
        return f"  {body}-{dv.lower()} "
    return f"{body}-{dv}"


def fmt_phone(digits9: str, style: int) -> str:
    return [f"+56{digits9}", digits9, f"+56 9 {digits9[1:5]} {digits9[5:]}",
            f"9 {digits9[1:5]} {digits9[5:]}"][style % 4]


class Generator:
    def __init__(self) -> None:
        self.rng = random.Random(SEED)
        self.customers: list[dict] = []
        self.advances: list[dict] = []
        self.cards: list[dict] = []
        self.used_bodies: set[int] = set()
        self.facts: Counter = Counter()

    # -------------------------------------------------------------- building blocks
    def when(self, after: datetime = START) -> datetime:
        span = int((ANCHOR - after).total_seconds())
        return after + timedelta(seconds=self.rng.randint(0, max(span, 1)))

    def new_body(self) -> int:
        while True:
            body = self.rng.randint(5_000_000, 26_000_000)
            if body not in self.used_bodies:
                self.used_bodies.add(body)
                return body

    def person(self, first=None, last=None, email=None) -> dict:
        r = self.rng
        first, last = first or r.choice(FIRST), last or r.choice(LAST)
        body = self.new_body()
        return {
            "body": body, "first": first, "last": last,
            "dob": date(1960, 1, 1) + timedelta(days=r.randint(0, 15_000)),
            "email": email or f"{fold(first)}.{fold(last).replace(' ', '')}{r.randint(1, 999)}@{r.choice(DOMAINS)}",
            "phone_digits": "9" + "".join(str(r.randint(0, 9)) for _ in range(8)),
        }

    def add_customer(self, p: dict, created_at: datetime, rut_style: str = "plain", **override) -> dict:
        c = {
            "national_id": fmt_rut(p["body"], rut_style),
            "first_name": p["first"], "last_name": p["last"],
            "email": p["email"],
            "phone": fmt_phone(p["phone_digits"], self.rng.randint(0, 3)),
            "birth_date": p["dob"], "created_at": created_at,
        }
        c.update(override)
        c["updated_at"] = c["created_at"] + timedelta(hours=self.rng.randint(0, 2000))
        self.customers.append(c)
        return c

    def add_advance(self, c: dict, status: str) -> None:
        created = self.when(c["created_at"])
        self.advances.append({"customer": c, "status": status, "created_at": created,
                              "amount": self.rng.randint(50, 900) * 1000})

    def add_card(self, c: dict) -> None:
        self.cards.append({"customer": c, "created_at": self.when(c["created_at"]),
                           "brand": self.rng.choice(BRANDS)[0],
                           "last4": f"{self.rng.randint(0, 9999):04d}",
                           "status": self.rng.choices(["active", "blocked", "expired"], [90, 5, 5])[0]})

    def random_activity(self, c: dict) -> None:
        r = self.rng
        if r.random() < 0.70:
            for _ in range(r.choice([1, 1, 1, 2])):
                self.add_advance(c, r.choices(
                    ["requested", "approved", "funded", "paid_off", "rejected"], [8, 7, 25, 45, 15])[0])
        if r.random() < 0.90:
            for _ in range(r.choice([1, 1, 1, 1, 2])):
                self.add_card(c)

    # -------------------------------------------------------------- population
    def build(self) -> None:
        r = self.rng
        normal = []
        for _ in range(4_500):
            c = self.add_customer(self.person(), self.when(), r.choice(["dotted", "plain", "compact"]))
            self.random_activity(c)
            normal.append(c)

        # Duplicate people. Same RUT in another format, name spelled differently.
        for g in range(150):
            p = self.person()
            times = sorted(self.when() for _ in range(3 if g % 10 == 0 else 2))
            styles = r.sample(["dotted", "plain", "compact", "padded"], len(times))
            group = []
            for i, (t, style) in enumerate(zip(times, styles)):
                variant = {}
                if i > 0 and r.random() < 0.5:
                    variant = {"first_name": fold(p["first"]).title(), "last_name": fold(p["last"]).upper()}
                if i > 0 and r.random() < 0.4:   # moved to a new email account
                    variant["email"] = f"{fold(p['first'])}{r.randint(10, 99)}@{r.choice(DOMAINS)}"
                group.append(self.add_customer(p, t, style, **variant))
            for c in group:                       # every record owns a card -> cards must move
                self.add_card(c)
            if g < 25:                            # the NEWER record holds the money
                self.add_advance(group[-1], r.choice(MONEY))
                self.add_advance(group[0], "rejected")
                self.facts["dup groups: newest record funded"] += 1
            elif g < 27:                          # both hold money -> must not be auto-merged
                self.add_advance(group[0], "paid_off")
                self.add_advance(group[1], "funded")
                self.facts["dup groups: two funded records"] += 1
            elif g < 50:
                self.add_advance(group[0], r.choice(MONEY))
                self.facts["dup groups: oldest record funded"] += 1
            else:
                if r.random() < 0.5:
                    self.add_advance(r.choice(group), r.choice(["requested", "rejected"]))
                self.facts["dup groups: no money"] += 1

        # Look-alikes that are NOT the same person.
        for i in range(3):   # RUT typo: someone else's RUT, different name and birth date
            victim = normal[i * 7]
            p = self.person()
            c = self.add_customer(p, self.when(victim["created_at"]),
                                  national_id=victim["national_id"].replace(".", ""))
            self.random_activity(c)
            self.facts["look-alike: RUT collision, different person"] += 1
        for i in range(8):   # family sharing one email
            a = normal[100 + i]
            p = self.person(last=a["last_name"], email=a["email"])
            self.random_activity(self.add_customer(p, self.when()))
            self.facts["look-alike: shared email (family)"] += 1
        for i in range(8):   # household sharing one phone
            a = normal[200 + i]
            self.random_activity(self.add_customer(self.person(), self.when(), phone=a["phone"]))
            self.facts["look-alike: shared phone (household)"] += 1
        for i in range(4):   # namesakes born the same day
            a = normal[300 + i]
            p = self.person(first=a["first_name"], last=a["last_name"])
            p["dob"] = a["birth_date"]
            self.random_activity(self.add_customer(p, self.when()))
            self.facts["look-alike: same name + birth date"] += 1

        # Test accounts. Several share a placeholder RUT, so a naive RUT merge would fuse them.
        placeholder = fmt_rut(11_111_111, "dotted")
        for first, last, email, rut in [
            ("Test", "User", "test@fundo.com", placeholder),
            ("QA", "Automation", "qa1@fundo.com", placeholder),
            ("Prueba", "Prueba", "prueba@fundo.com", placeholder),
            ("Ana", "Rojas", "qa.ana@fundo.com", None),
            ("Dummy", "Account", "dummy.account@example.com", None),
            ("Cliente", "Demo", "test+3@fundo.com", None),
        ]:
            c = self.add_customer(self.person(first, last, email), self.when(), **({"national_id": rut} if rut else {}))
            self.add_card(c)
            self.facts["test accounts"] += 1
        c = self.add_customer(self.person("Test", "Payments", "test.payments@fundo.com"), self.when(),
                              national_id=placeholder)
        self.add_advance(c, "funded")
        self.add_card(c)
        self.facts["test-looking account WITH a funded advance"] += 1

        # Real people a naive filter would wrongly drop.
        for first, last, email in [
            ("Carlos", "Testerman", "carlos.testerman@gmail.com"),
            ("Daniela", "Muñoz", "contest.winner@gmail.com"),
            ("María", "González", "maria.gonzalez@fundo.com"),   # staff
            ("Pedro", "Soto", "pedro.soto@fundo.com"),           # staff
            ("Tomás", "Quintana", "tquintana@fundo.com"),        # staff
        ]:
            c = self.add_customer(self.person(first, last, email), self.when())
            self.add_advance(c, "paid_off")
            self.add_card(c)
            self.facts["real people that look like test data"] += 1

        # Malformed / placeholder contacts and bad national ids on ordinary customers.
        bad_emails = ["{f}.{l}@gmail", "{f}.{l}.gmail.com", "{f} {l}@gmail.com", "noemail@noemail.com", ""]
        bad_phones = ["12345", "000000000", "n/a", "+56 2 2345 678", "999999999"]
        for c in r.sample(normal, 180):
            c["email"] = r.choice(bad_emails).format(f=fold(c["first_name"]), l=fold(c["last_name"]))
        for c in r.sample(normal, 200):
            c["phone"] = r.choice(bad_phones)
        for c in r.sample(normal[400:], 25):     # one digit off -> check digit no longer matches
            body = int(normalize_rut(c["national_id"])[0].split("-")[0])
            wrong = "0" if rut_check_digit(body) != "0" else "1"
            c["national_id"] = fmt_rut(body, "plain", dv=wrong)
        for c in r.sample(normal[400:], 10):
            c["national_id"] = None

        # Assign ids in creation order, like an IDENTITY column would.
        self.customers.sort(key=lambda c: c["created_at"])
        for i, c in enumerate(self.customers, start=1):
            c["customer_id"] = i
        self.advances.sort(key=lambda a: a["created_at"])
        self.cards.sort(key=lambda c: c["created_at"])


def is_seeded(conn) -> bool:
    return db.query(conn, "SELECT COUNT(*) FROM dbo.customers")[0][0] > 0


def seed(conn) -> None:
    if is_seeded(conn):
        print("source already seeded - skipping (use `reset --yes` for a fresh start)")
        return
    g = Generator()
    g.build()
    with db.transaction(conn) as cur:
        db.insert_rows(conn, "dbo.card_brands", ["brand_code", "brand_name"], BRANDS)
        cur.execute("SET IDENTITY_INSERT dbo.customers ON")
        db.insert_rows(conn, "dbo.customers",
                       ["customer_id", "national_id", "first_name", "last_name", "email", "phone",
                        "birth_date", "created_at", "updated_at"],
                       [(c["customer_id"], c["national_id"], c["first_name"], c["last_name"], c["email"],
                         c["phone"], c["birth_date"], c["created_at"], c["updated_at"]) for c in g.customers])
        cur.execute("SET IDENTITY_INSERT dbo.customers OFF")
        db.insert_rows(conn, "dbo.advances", ["customer_id", "amount", "status", "created_at", "updated_at"],
                       [(a["customer"]["customer_id"], a["amount"], a["status"], a["created_at"],
                         a["created_at"] + timedelta(days=g.rng.randint(0, 60))) for a in g.advances])
        db.insert_rows(conn, "dbo.cards", ["customer_id", "brand_code", "last4", "status", "created_at", "updated_at"],
                       [(c["customer"]["customer_id"], c["brand"], c["last4"], c["status"], c["created_at"],
                         c["created_at"]) for c in g.cards])
        # Transactions are generated set-based in T-SQL: ~95k rows in one statement.
        # Every 33rd card has none (so some cards can be deleted later without breaking FKs).
        cur.execute("""
            ;WITH n AS (SELECT TOP (40) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n FROM sys.all_objects)
            INSERT dbo.transactions (card_id, amount, merchant, txn_at, created_at)
            SELECT c.card_id,
                   CAST(((c.card_id * 7919 + n.n * 104729) % 99000 + 1000) / 100.0 AS DECIMAL(12,2)),
                   CHOOSE((c.card_id + n.n) % 8 + 1, N'Lider', N'Jumbo', N'Copec', N'Falabella',
                          N'Uber', N'Rappi', N'Netflix', N'Cruz Verde'),
                   t.txn_at, t.txn_at
            FROM dbo.cards c
            JOIN n ON n.n <= (c.card_id * 13) % 33 + 2
            CROSS APPLY (SELECT DATEADD(MINUTE, (c.card_id * 31 + n.n * 7) % 1440,
                         DATEADD(DAY, (c.card_id * 17 + n.n * 37) % (DATEDIFF(DAY, c.created_at, '2026-09-30') + 1),
                                 CAST(CAST(c.created_at AS DATE) AS DATETIME2(3)))) AS txn_at) t
            WHERE c.card_id % 33 <> 0
            ORDER BY t.txn_at
        """)
        cur.execute("""
            INSERT dbo.tmp_marketing_export_2021 (email, campaign)
            SELECT TOP (300) email, N'spring_2021_blast' FROM dbo.customers ORDER BY customer_id
        """)

    counts = {t: db.query(conn, f"SELECT COUNT(*) FROM dbo.{t}")[0][0]
              for t in ["customers", "advances", "cards", "transactions", "customer_history",
                        "card_brands", "tmp_marketing_export_2021"]}
    print("seeded source:", ", ".join(f"{k}={v:,}" for k, v in counts.items()))
    for k, v in g.facts.items():
        print(f"  planted  {k}: {v}")
