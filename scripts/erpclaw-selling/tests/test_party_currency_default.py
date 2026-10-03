"""A new customer takes its company's currency, not a hard-coded USD."""
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company,
)

mod = load_db_query()


def _set_company_currency(conn, company_id, currency):
    conn.execute(
        "UPDATE company SET default_currency = ? WHERE id = ?",
        (currency, company_id),
    )
    conn.commit()


def _add_customer(conn, company_id, name):
    return call_action(mod.add_customer, conn, ns(
        name=name, company_id=company_id,
        customer_type=None, customer_group=None,
        payment_terms_id=None, credit_limit=None,
        tax_id=None, exempt_from_sales_tax=None,
        primary_address=None, primary_contact=None,
    ))


def _currency_of(conn, customer_id):
    row = conn.execute(
        "SELECT default_currency FROM customer WHERE id = ?",
        (customer_id,)).fetchone()
    return row["default_currency"]


class TestAddCustomerTakesCompanyCurrency:
    def test_eur_company_stores_eur(self, conn):
        company_id = seed_company(conn)
        _set_company_currency(conn, company_id, "EUR")
        result = _add_customer(conn, company_id, "EUR Customer")
        assert is_ok(result)
        assert _currency_of(conn, result["customer_id"]) == "EUR"

    def test_usd_company_stores_usd(self, conn):
        company_id = seed_company(conn)
        result = _add_customer(conn, company_id, "USD Customer")
        assert is_ok(result)
        assert _currency_of(conn, result["customer_id"]) == "USD"


class TestImportCustomersTakeCompanyCurrency:
    def test_absent_blank_and_given_cells(self, conn, tmp_path):
        company_id = seed_company(conn)
        _set_company_currency(conn, company_id, "EUR")
        path = tmp_path / "customers.csv"
        path.write_text(
            "name,customer_type,default_currency\n"
            "Imp Absent,company\n"
            "Imp Blank,company,\n"
            "Imp GBP,company,GBP\n"
        )
        result = call_action(mod.import_customers, conn, ns(
            csv_path=str(path), company_id=company_id))
        assert is_ok(result)
        assert (result["imported"], result["skipped"],
                result["total_rows"]) == (3, 0, 3)
        stored = {r["name"]: r["default_currency"] for r in conn.execute(
            "SELECT name, default_currency FROM customer WHERE company_id = ?",
            (company_id,))}
        assert stored == {
            "Imp Absent": "EUR",
            "Imp Blank": "EUR",
            "Imp GBP": "GBP",
        }

    def test_unknown_company_refused_and_writes_nothing(self, conn, tmp_path):
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM customer").fetchone()["c"]
        path = tmp_path / "customers.csv"
        path.write_text("name,default_currency\nGhost Customer,EUR\n")
        result = call_action(mod.import_customers, conn, ns(
            csv_path=str(path), company_id="no-such-company"))
        assert is_error(result)
        assert result["message"] == "Company no-such-company not found"
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM customer").fetchone()["c"]
        assert after == before


class TestImportCustomersTypeNormalisation:
    def test_mixed_case_and_absent_cells_store_lowercase(self, conn, tmp_path):
        company_id = seed_company(conn)
        path = tmp_path / "customers.csv"
        path.write_text(
            "name,customer_type\n"
            "Type Company,Company\n"
            "Type Individual,INDIVIDUAL\n"
            "Type Default\n"
        )
        result = call_action(mod.import_customers, conn, ns(
            csv_path=str(path), company_id=company_id))
        assert is_ok(result)
        assert (result["imported"], result["skipped"],
                result["total_rows"]) == (3, 0, 3)
        stored = {r["name"]: r["customer_type"] for r in conn.execute(
            "SELECT name, customer_type FROM customer WHERE company_id = ?",
            (company_id,))}
        assert stored == {
            "Type Company": "company",
            "Type Individual": "individual",
            "Type Default": "company",
        }

    def test_invalid_type_refuses_whole_import(self, conn, tmp_path):
        company_id = seed_company(conn)
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM customer").fetchone()["c"]
        path = tmp_path / "customers.csv"
        path.write_text(
            "name,customer_type\n"
            "Good One,Company\n"
            "Bad One,Partnership\n"
        )
        result = call_action(mod.import_customers, conn, ns(
            csv_path=str(path), company_id=company_id))
        assert is_error(result)
        assert result["message"] == (
            "Row 2: customer_type 'Partnership' must be company or individual")
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM customer").fetchone()["c"]
        assert after == before
