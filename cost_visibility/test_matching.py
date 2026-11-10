import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from cost_visibility.matching import matches


def default_patterns():
    # Read the literal policy constant without importing Frappe.
    tree = ast.parse(Path(__file__).with_name("api.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_PATTERNS" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("DEFAULT_PATTERNS not found")


PATTERNS = default_patterns()


class TestPatterns(unittest.TestCase):
    def test_every_configured_default_matches_names_and_labels(self):
        for pattern in PATTERNS:
            with self.subTest(pattern=pattern):
                text = pattern.replace("_", " ").upper()
                self.assertTrue(matches("custom_" + text + "_field", PATTERNS))
                self.assertTrue(matches({"fieldname": "value", "label": text}, PATTERNS))

    def test_literal_custom_patterns_and_normalization(self):
        self.assertTrue(matches("  Supplier__COST\tValue ", [" SUPPLIER cost "]))
        self.assertTrue(matches({"label": "Special [rate]"}, ["[rate]"]))
        self.assertFalse(matches({"label": "Special rate"}, ["[rate]"]))
        self.assertTrue(matches("supplier_cost", "supplier cost"))
        self.assertFalse(matches("selling_rate", ["", " ", None]))
        self.assertFalse(matches("anything", None))

    def test_explicit_patterns_override_selling_precedence(self):
        field = {"fieldname": "selling_rate", "label": "Supplier Cost", "parent": "Sales Order Item"}
        self.assertTrue(matches(field, ["supplier cost"], report_name="Sales and Purchase"))
        self.assertTrue(matches(field, ["selling rate"]))

    def test_all_supported_metadata_shapes(self):
        for key in ("fieldname", "field", "id", "label", "name", "content"):
            for factory in (lambda **kw: kw, SimpleNamespace):
                with self.subTest(key=key, factory=factory):
                    self.assertTrue(matches(factory(**{key: "purchase_rate"}), PATTERNS))
        for outer in ("df", "docfield"):
            for key in ("fieldname", "label"):
                with self.subTest(outer=outer, key=key):
                    field = SimpleNamespace(**{outer: {key: "Stock_Value"}})
                    self.assertTrue(matches(field, PATTERNS))
                    self.assertTrue(matches({outer: SimpleNamespace(**{key: "Stock Value"})}, PATTERNS))

    def test_invalid_or_empty_field_values(self):
        for field in (None, "", {}, SimpleNamespace(), {"fieldname": 42}, {"df": None}):
            with self.subTest(field=field):
                self.assertFalse(matches(field, PATTERNS, doctype="Purchase Invoice"))

    def test_intrinsic_cost_fields_without_patterns(self):
        for field in ("incoming_rate", "outgoing_rate", "basic_rate", "basic_amount", "valuation",
                      "valuation_rate", "stock_value", "stock_value_difference"):
            with self.subTest(field=field):
                self.assertTrue(matches(field, []))
                self.assertTrue(matches({"id": "Stock Entry Detail:" + field}, []))
        for field in ("interest_rate", "exchange_rate", "tax_rate", "hourly_rate", "basic_salary",
                      "basic_rate_of_pay", "incoming_rate_limit", "stock_value_note"):
            with self.subTest(field=field):
                self.assertFalse(matches(field, []))


class TestPurchaseContext(unittest.TestCase):
    def test_purchase_doctypes_and_child_doctypes(self):
        fields = ("rate", "net_rate", "base_rate", "base_net_rate", "price_list_rate",
                  "base_price_list_rate", "stock_uom_rate", "amount", "net_amount", "base_amount",
                  "base_net_amount", "total", "net_total", "base_total", "base_net_total",
                  "grand_total", "base_grand_total", "rounded_total", "base_rounded_total")
        for doctype in ("Purchase Invoice", "Purchase Order", "Purchase Receipt", "Supplier Quotation"):
            for owner in (doctype, doctype + " Item"):
                for field in fields:
                    with self.subTest(owner=owner, field=field):
                        self.assertTrue(matches(field, PATTERNS, doctype=owner))
                        self.assertTrue(matches({"fieldname": field, "parent": owner}, PATTERNS))
                self.assertFalse(matches("qty", PATTERNS, doctype=owner))

    def test_owner_metadata_takes_precedence_over_report_context(self):
        for nested in ("df", "docfield"):
            for key in ("parent", "doctype"):
                for owner in ("Sales Invoice Item", "Sales Order", "Quotation Item", "Delivery Note"):
                    with self.subTest(nested=nested, key=key, owner=owner):
                        self.assertFalse(matches({"fieldname": "rate", nested: {key: owner}}, PATTERNS,
                                                 report_name="Buying Register", doctype="Purchase Invoice"))
                self.assertTrue(matches({"fieldname": "rate", nested: {key: "Purchase Order Item"}},
                                        PATTERNS, report_name="Sales Register"))
        self.assertFalse(matches({"fieldname": "rate", "parent": "Purchase Invoice Item",
                                  "docfield": {"doctype": "Sales Invoice Item"}}, PATTERNS))

    def test_mixed_selling_fields_are_visible(self):
        for field in (
            {"fieldname": "rate", "label": "Selling Rate"},
            {"fieldname": "selling_rate", "label": "Rate"},
            {"fieldname": "rate", "docfield": {"label": "Sales Rate"}},
            {"fieldname": "amount", "field": "sales_amount"},
        ):
            with self.subTest(field=field):
                self.assertFalse(matches(field, PATTERNS, doctype="Purchase Order"))

    def test_purchase_reports_and_reference_doctypes(self):
        for report in ("Item Wise Purchase Register", "BUYING_prices", "Supplier Quotation Summary"):
            for field in ("rate", "billed_amt", "billed_amount", "pending_amount", "received_qty_amount"):
                with self.subTest(report=report, field=field):
                    self.assertTrue(matches(field, PATTERNS, report_name=report, ref_doctype="Item"))
        for field in ("billed_amt", "billed_amount", "pending_amount", "received_qty_amount"):
            self.assertFalse(matches(field, PATTERNS, report_name="Sales Register"))
        self.assertTrue(matches("rate", PATTERNS, ref_doctype="Purchase Receipt Item"))
        self.assertTrue(matches("rate", PATTERNS, document={"doctype": "Purchase Invoice"}))
        self.assertTrue(matches("rate", PATTERNS, document=SimpleNamespace(doctype="Purchase Order")))
        self.assertFalse(matches("rate", PATTERNS, report_name="Sales and Purchase Comparison"))
        self.assertFalse(matches("rate", PATTERNS, report_name="Selling and Buying Comparison"))
        self.assertFalse(matches("rate", PATTERNS, report_name="Repurchase Analysis"))
        self.assertTrue(matches("rate", PATTERNS, report_name="Sales and Purchase", ref_doctype="Purchase Order"))

    def test_exact_names_and_company_currency_labels(self):
        for field in ("rate", "net rate", "price list rate", "amount", "net amount"):
            with self.subTest(field=field):
                self.assertTrue(matches(field + " (Company Currency)", PATTERNS, report_name="Buying"))
        self.assertTrue(matches("Purchase Invoice Item:base_rate", PATTERNS, doctype="Purchase Invoice"))
        for field in ("exchange_rate", "tax_rate", "interest_rate", "rate_of_return", "total_qty",
                      "paid_amount", "billed_qty", "quantity", "rate (foreign currency)"):
            with self.subTest(field=field):
                self.assertFalse(matches(field, PATTERNS, doctype="Purchase Invoice"))
        self.assertFalse(matches("rate", PATTERNS))
        self.assertFalse(matches("rate", PATTERNS, doctype="Sales Invoice"))


class TestItemPrice(unittest.TestCase):
    def test_buying_flag(self):
        for value in (True, 1, 1.0, "1"):
            with self.subTest(value=value):
                self.assertTrue(matches("price_list_rate", PATTERNS, doctype="Item Price", document={"buying": value}))
                self.assertTrue(matches("price_list_rate", PATTERNS,
                                        document=SimpleNamespace(doctype="Item Price", buying=value)))
                self.assertFalse(matches("rate", PATTERNS, doctype="Item Price", document={"buying": value}))
        for value in (False, 0, "0", None, "true", 2):
            with self.subTest(value=value):
                self.assertFalse(matches("price_list_rate", PATTERNS, doctype="Item Price", document={"buying": value}))
        self.assertFalse(matches("price_list_rate", PATTERNS, doctype="Item Price"))
        self.assertFalse(matches("price_list_rate", PATTERNS, report_name="Item Prices", ref_doctype="Item Price"))
        self.assertTrue(matches("price_list_rate", PATTERNS, report_name="Buying Price Report", ref_doctype="Item Price"))

    def test_selling_field_and_owner_precedence(self):
        self.assertFalse(matches({"fieldname": "price_list_rate", "label": "Selling Rate"}, PATTERNS,
                                 doctype="Item Price", document={"buying": 1}))
        self.assertFalse(matches({"fieldname": "price_list_rate", "parent": "Sales Order Item"}, PATTERNS,
                                 doctype="Item Price", document={"buying": 1}))
        self.assertTrue(matches("purchase_rate", PATTERNS, doctype="Item Price", document={"buying": 0}))


if __name__ == "__main__":
    unittest.main()
