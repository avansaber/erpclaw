"""A new supplier takes its company's currency, not a hard-coded USD."""
from buying_helpers import (
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


def _add_supplier(conn, company_id, name):
    return call_action(mod.add_supplier, conn, ns(
        name=name, company_id=company_id,
        supplier_type=None, supplier_group=None,
        payment_terms_id=None, tax_id=None,
        is_1099_vendor=None, primary_address=None,
    ))


def _currency_of(conn, supplier_id):
    row = conn.execute(
        "SELECT default_currency FROM supplier WHERE id = ?",
        (supplier_id,)).fetchone()
    return row["default_currency"]


class TestAddSupplierTakesCompanyCurrency:
    def test_eur_company_stores_eur(self, conn):
        company_id = seed_company(conn)
        _set_company_currency(conn, company_id, "EUR")
        result = _add_supplier(conn, company_id, "EUR Supplier")
        assert is_ok(result)
        assert _currency_of(conn, result["supplier_id"]) == "EUR"

    def test_usd_company_stores_usd(self, conn):
        company_id = seed_company(conn)
        result = _add_supplier(conn, company_id, "USD Supplier")
        assert is_ok(result)
        assert _currency_of(conn, result["supplier_id"]) == "USD"


class TestImportSuppliersTakeCompanyCurrency:
    def test_absent_blank_and_given_cells(self, conn, tmp_path):
        company_id = seed_company(conn)
        _set_company_currency(conn, company_id, "EUR")
        path = tmp_path / "suppliers.csv"
        path.write_text(
            "name,supplier_type,default_currency\n"
            "Imp Absent,company\n"
            "Imp Blank,company,\n"
            "Imp GBP,company,GBP\n"
        )
        result = call_action(mod.import_suppliers, conn, ns(
            csv_path=str(path), company_id=company_id))
        assert is_ok(result)
        assert (result["imported"], result["skipped"],
                result["total_rows"]) == (3, 0, 3)
        stored = {r["name"]: r["default_currency"] for r in conn.execute(
            "SELECT name, default_currency FROM supplier WHERE company_id = ?",
            (company_id,))}
        assert stored == {
            "Imp Absent": "EUR",
            "Imp Blank": "EUR",
            "Imp GBP": "GBP",
        }

    def test_unknown_company_refused_and_writes_nothing(self, conn, tmp_path):
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM supplier").fetchone()["c"]
        path = tmp_path / "suppliers.csv"
        path.write_text("name,default_currency\nGhost Supplier,EUR\n")
        result = call_action(mod.import_suppliers, conn, ns(
            csv_path=str(path), company_id="no-such-company"))
        assert is_error(result)
        assert result["message"] == "Company no-such-company not found"
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM supplier").fetchone()["c"]
        assert after == before


class TestImportSuppliersTypeNormalisation:
    def test_mixed_case_and_absent_cells_store_lowercase(self, conn, tmp_path):
        company_id = seed_company(conn)
        path = tmp_path / "suppliers.csv"
        path.write_text(
            "name,supplier_type\n"
            "Type Company,Company\n"
            "Type Individual,INDIVIDUAL\n"
            "Type Default\n"
        )
        result = call_action(mod.import_suppliers, conn, ns(
            csv_path=str(path), company_id=company_id))
        assert is_ok(result)
        assert (result["imported"], result["skipped"],
                result["total_rows"]) == (3, 0, 3)
        stored = {r["name"]: r["supplier_type"] for r in conn.execute(
            "SELECT name, supplier_type FROM supplier WHERE company_id = ?",
            (company_id,))}
        assert stored == {
            "Type Company": "company",
            "Type Individual": "individual",
            "Type Default": "company",
        }

    def test_invalid_type_refuses_whole_import(self, conn, tmp_path):
        company_id = seed_company(conn)
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM supplier").fetchone()["c"]
        path = tmp_path / "suppliers.csv"
        path.write_text(
            "name,supplier_type\n"
            "Good One,Company\n"
            "Bad One,Partnership\n"
        )
        result = call_action(mod.import_suppliers, conn, ns(
            csv_path=str(path), company_id=company_id))
        assert is_error(result)
        assert result["message"] == (
            "Row 2: supplier_type 'Partnership' must be company or individual")
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM supplier").fetchone()["c"]
        assert after == before
