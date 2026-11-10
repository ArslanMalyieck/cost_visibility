import csv
import inspect
import io
import json
import unittest
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from openpyxl import load_workbook

from cost_visibility import report_builder as rb


RESTRICTED = {"restricted": True, "patterns": ["valuation", "supplier cost"]}
OPEN = {"restricted": False, "patterns": []}
CSV = {"delimiter": ",", "quoting": csv.QUOTE_MINIMAL, "decimal_sep": "."}


def df(name, label, fieldtype="Data", options=None):
    return frappe._dict(fieldname=name, label=label, fieldtype=fieldtype,
                        options=options, translatable=False, hide_days=False)


META = {
    "Purchase Invoice": {
        "item": df("item", "Item"), "qty": df("qty", "Quantity", "Float"),
        "rate": df("rate", "Rate", "Currency"),
        "masked": df("masked", "Supplier Cost", "Currency"),
        "state": df("state", "State", "Select"),
        "elapsed": df("elapsed", "Elapsed", "Duration"),
        "items": df("items", "Items", "Table", "Purchase Invoice Item"),
    },
    "Purchase Invoice Item": {
        "rate": df("rate", "Rate", "Currency"), "qty": df("qty", "Quantity", "Float"),
    },
    "Sales Invoice": {
        "rate": df("rate", "Rate", "Currency"), "item": df("item", "Item"),
    },
    "Sales Invoice Item": {"rate": df("rate", "Rate", "Currency")},
}


def meta(doctype):
    fields = META.get(doctype, {})
    return SimpleNamespace(get_field=fields.get, get_label=lambda name: name,
                           translated_doctype=False)


def params(**values):
    args = dict(doctype="Purchase Invoice", fields=["item", "rate", "qty"],
                file_format_type="CSV", title="Purchase Report", report_name="Purchase Report",
                filters={}, save_user_settings=True)
    args.update(values)
    return frappe._dict(args)


class PrintTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(rb, "get_policy", return_value=RESTRICTED))
        self.stack.enter_context(patch.object(frappe, "conf", SimpleNamespace(read_from_replica=False)))
        self.stack.enter_context(patch.object(rb, "_", side_effect=lambda value, **kw: value))
        self.stack.enter_context(patch.object(rb.reportview, "_", side_effect=lambda value, **kw: value))
        self.stack.enter_context(patch.object(frappe, "get_meta", side_effect=meta))
        self.stack.enter_context(patch.object(rb.reportview, "is_virtual_doctype", return_value=False))

    def run_get(self, fields, rows, **kwargs):
        args = frappe._dict(doctype="Purchase Invoice", fields=fields, **kwargs)
        with patch.object(rb.reportview, "get_form_params", return_value=args) as form, \
                patch.object(rb.reportview, "execute", return_value=rows) as execute:
            result = rb.get()
        form.assert_called_once_with()
        execute.assert_called_once_with(**args)
        return result

    def test_mapping_and_unrestricted_exact_delegation(self):
        self.assertEqual(inspect.signature(rb.get), inspect.signature(rb.reportview.get))
        self.assertIn(rb.get, frappe.whitelisted)
        self.assertEqual(rb.OVERRIDE_WHITELISTED_METHODS["frappe.desk.reportview.get"],
                         "cost_visibility.report_builder.get")
        payload = {"keys": ["rate"], "values": [[1234]], "user_info": {}}
        with patch.object(rb, "get_policy", return_value=OPEN), \
                patch.object(rb.reportview, "get", return_value=payload) as core, \
                patch.object(rb.reportview, "get_form_params") as validation:
            self.assertIs(rb.get(), payload)
        core.assert_called_once_with()
        validation.assert_not_called()

    def test_actual_form_validation_aggregate_and_compression(self):
        fields = ["name", "item", "rate", "qty", "masked"]
        form = frappe._dict(doctype="Purchase Invoice", fields=json.dumps(fields),
                            group_by="item", aggregate_function="sum", aggregate_on_field="rate",
                            aggregate_on_doctype="Purchase Invoice", view="Report", user="spoof",
                            ignore_permissions=1, filters="[]")
        local = SimpleNamespace(form_dict=form)
        fake_meta = meta("Purchase Invoice")
        fake_meta.get_high_permlevel_fields = lambda: []
        rows = [frappe._dict(qty=2, _aggregate_column=700, name="INV-1", masked=300,
                             item="A", rate=100),
                frappe._dict(qty=3, _aggregate_column=900, name="INV-2", masked=400,
                             item="B", rate=150)]
        with patch.object(frappe, "local", local), \
                patch.object(frappe, "db", SimpleNamespace(has_column=lambda *args: True)), \
                patch.object(frappe, "get_meta", side_effect=lambda name: fake_meta if name == "Purchase Invoice" else meta(name)), \
                patch.object(rb.reportview, "execute", return_value=rows) as execute:
            result = rb.get()
        self.assertEqual(result, {
            "keys": ["qty", "_aggregate_column", "name", "masked", "item", "rate"],
            "values": [[2, None, "INV-1", None, "A", None],
                       [3, None, "INV-2", None, "B", None]], "user_info": {},
        })
        query = execute.call_args.kwargs
        self.assertEqual(query["fields"][-1], {"SUM": "`tabPurchase Invoice`.`rate`", "as": "_aggregate_column"})
        self.assertNotIn("user", query)
        self.assertNotIn("ignore_permissions", query)
        self.assertNotIn("view", query)
        self.assertEqual(query["filters"], [])
        self.assertEqual(form.fields, json.dumps(fields))

    def test_child_alias_ambiguous_keys_and_unknown_outputs(self):
        fields = ["name", "`tabPurchase Invoice Item`.`rate` as 'Purchase Invoice Item:rate'",
                  "`tabPurchase Invoice Item`.`qty` as 'Purchase Invoice Item:qty'",
                  "masked as harmless", "item as duplicate", "rate as duplicate", "qty"]
        rows = [frappe._dict(name="INV", **{"Purchase Invoice Item:rate": 99,
                            "Purchase Invoice Item:qty": 4, "harmless": 1234,
                            "duplicate": 100, "qty": 5, "unexpected": 987})]
        result = self.run_get(fields, rows)
        self.assertEqual(result["keys"], list(rows[0]))
        self.assertEqual(result["values"], [["INV", None, 4, None, None, 5, None]])

    def test_list_standard_fields_assignments_and_totals_preserved(self):
        rows = [frappe._dict(name="A", _assign='["alice"]', _comments="[]", _comment_count=2,
                             rate=100, qty=2),
                frappe._dict(name="B", _assign="", _comments="[]", _comment_count=0,
                             rate=150, qty=3)]
        def user_info(user, users):
            users[user] = {"fullname": "Alice"}
        with patch.object(rb.reportview, "add_user_info", side_effect=user_info), \
                patch("frappe.desk.query_report._", side_effect=lambda value, **kw: value):
            result = self.run_get(["name", "_assign", "_comments", "rate", "qty"], rows,
                                  with_comment_count=1, add_total_row=1)
        self.assertEqual(result["user_info"], {"alice": {"fullname": "Alice"}})
        self.assertEqual(result["keys"], ["name", "_assign", "_comments", "_comment_count", "rate", "qty"])
        self.assertEqual(result["values"][0], ["A", '["alice"]', "[]", 2, None, 2])
        self.assertEqual(result["values"][1], ["B", "", "[]", 0, None, 3])
        self.assertEqual(result["values"][2], ["Total", "", "", 2.0, None, 5.0])

    def test_nested_aggregate_and_safe_count(self):
        fields = ["name", {"COUNT": "*", "as": "count"},
                  {"DIV": [{"SUM": "rate"}, {"NULLIF": ["qty", 0]}], "as": "average"},
                  {"AVG": ["qty"], "as": "average_qty"},
                  {"SUM": "rate"}]
        rows = [frappe._dict(name="A", count=2, average=500, average_qty=3, **{"SUM(rate)": 1000})]
        self.assertEqual(self.run_get(fields, rows)["values"], [["A", 2, None, 3, None]])

    def test_missing_expression_mappings_fail_closed_without_breaking_simple_fields(self):
        from frappe.database import query

        fields = ["item", "rate", {"COUNT": "*", "as": "count"},
                  {"MYSTERY": ["rate"], "as": "unknown"}]
        rows = [frappe._dict(item="A", rate=1234, count=2, unknown=1234)]
        with patch.dict(query.__dict__):
            query.__dict__.pop("FUNCTION_MAPPING", None)
            query.__dict__.pop("OPERATOR_MAPPING", None)
            self.assertEqual(self.run_get(fields, rows)["values"], [["A", None, None, None]])

    def test_v15_string_aggregate_is_masked_without_mappings(self):
        from frappe.database import query

        fields = ["item", "sum(`tabPurchase Invoice`.`rate`) AS _aggregate_column"]
        rows = [frappe._dict(item="A", _aggregate_column=1234)]
        with patch.dict(query.__dict__):
            query.__dict__.pop("FUNCTION_MAPPING", None)
            query.__dict__.pop("OPERATOR_MAPPING", None)
            self.assertEqual(self.run_get(fields, rows)["values"], [["A", None]])

    def test_unknown_and_malformed_expressions_fail_closed(self):
        fields = [{"MYSTERY": ["qty"], "as": "unknown"},
                  {"SUM": {"MYSTERY": ["qty"]}, "as": "nested"},
                  {"SUM": "'x' || rate || 'y'", "as": "injected"},
                  {"SUM": "qty", 1: "rate", "as": "malformed"}, "item"]
        rows = [frappe._dict(unknown=2, nested=2, injected=1234, malformed=1234, item="A")]
        self.assertEqual(self.run_get(fields, rows)["values"], [[None, None, None, None, "A"]])

    def test_standard_columns_and_selling_child_context(self):
        fields = ["name", "`tabSales Invoice Item`.`rate`", "item", "owner"]
        rows = [frappe._dict(name="A", rate=100, item="B", owner="alice", modified="today")]
        self.assertEqual(self.run_get(fields, rows)["values"], [["A", 100, "B", "alice", "today"]])

    def test_wildcard_expands_via_real_form_validation(self):
        local = SimpleNamespace(form_dict=frappe._dict(doctype="Purchase Invoice", fields="*",
                                                        with_comment_count=1, view="List"))
        fake_meta = meta("Purchase Invoice")
        fake_meta.get_high_permlevel_fields = lambda: []
        rows = [frappe._dict(name="A", rate=1234, qty=2, _comment_count=1)]
        with patch.object(frappe, "local", local), \
                patch.object(frappe, "get_meta", return_value=fake_meta), \
                patch.object(rb.reportview, "get_permitted_fields", return_value=["name", "rate", "qty"]), \
                patch.object(rb.reportview, "execute", return_value=rows) as execute:
            result = rb.get()
        self.assertEqual(result["values"], [["A", None, 2, 1]])
        self.assertEqual(execute.call_args.kwargs["fields"], ["name", "rate", "qty"])
        self.assertEqual(local.form_dict.fields, "*")

    def test_unresolved_columns_and_custom_standard_field_pattern(self):
        rows = [frappe._dict(name="A", qty=2, _comment_count=7, other=1234)]
        fields = ["name", "qty", {"unsupported": ["rate"]}]
        with patch.object(rb, "get_policy", return_value={"restricted": True, "patterns": ["_comment_count"]}):
            result = self.run_get(fields, rows, with_comment_count=1)
        self.assertEqual(result["values"], [["A", 2, None, None]])

    def test_computed_comment_count_respects_configured_pattern(self):
        rows = [frappe._dict(name="A", _comment_count=7)]
        with patch.object(rb, "get_policy", return_value={"restricted": True, "patterns": ["_comment_count"]}):
            self.assertEqual(self.run_get(["name"], rows, with_comment_count=1)["values"], [["A", None]])

    def test_empty_results_and_default_field(self):
        self.assertEqual(self.run_get(["rate"], []), [])
        self.assertEqual(self.run_get(None, [frappe._dict(name="INV")])["values"], [["INV"]])

    def test_virtual_doctype_uses_core_call_and_compress_contract(self):
        args = frappe._dict(doctype="Purchase Invoice", fields=["item", "rate"], add_total_row=1)
        rows = [frappe._dict(item="A", rate=1234)]
        controller = SimpleNamespace(get_list=object())
        with patch.object(frappe, "__version__", "16.27.1"), \
                patch.object(rb.reportview, "get_form_params", return_value=args), \
                patch.object(rb.reportview, "is_virtual_doctype", return_value=True), \
                patch.object(rb.reportview, "get_controller", return_value=controller), \
                patch.object(frappe, "call", return_value=rows) as call, \
                patch.object(rb.reportview, "execute") as execute:
            result = rb.get()
        call.assert_called_once_with(controller.get_list, args=args, **args)
        execute.assert_not_called()
        self.assertEqual(result, {"keys": ["item", "rate"], "values": [["A", None]], "user_info": {}})

    def test_v15_virtual_doctype_calls_controller_positionally(self):
        args = frappe._dict(doctype="Purchase Invoice", fields=["item", "rate"], add_total_row=1)
        rows = [frappe._dict(item="A", rate=1234)]
        seen = []
        def get_list(arg, /):
            seen.append(arg)
            return rows
        with patch.object(frappe, "__version__", "15.126.1"), \
                patch.object(rb.reportview, "get_form_params", return_value=args), \
                patch.object(rb.reportview, "is_virtual_doctype", return_value=True), \
                patch.object(rb.reportview, "get_controller", return_value=SimpleNamespace(get_list=get_list)), \
                patch.object(frappe, "call") as call:
            result = rb.get()
        self.assertEqual(seen, [args])
        call.assert_not_called()
        self.assertEqual(result, {"keys": ["item", "rate"], "values": [["A", None]], "user_info": {}})

    def test_virtual_controller_errors_are_not_retried(self):
        args = frappe._dict(doctype="Purchase Invoice", fields=["item"])
        for version in ("15.126.1", "16.27.1"):
            def get_list(*args, **kwargs):
                raise TypeError("controller failed")
            with self.subTest(version=version), patch.object(frappe, "__version__", version), \
                    patch.object(rb.reportview, "get_form_params", return_value=args), \
                    patch.object(rb.reportview, "is_virtual_doctype", return_value=True), \
                    patch.object(rb.reportview, "get_controller", return_value=SimpleNamespace(get_list=get_list)), \
                    patch.object(frappe, "call", side_effect=lambda fn, **kwargs: fn(**kwargs)) as call:
                with self.assertRaisesRegex(TypeError, "controller failed"):
                    rb.get()
                self.assertEqual(call.call_count, 0 if version.startswith("15") else 1)

    def test_validation_and_permission_exceptions_propagate(self):
        with patch.object(rb.reportview, "get_form_params", side_effect=ValueError("invalid")), \
                patch.object(rb.reportview, "execute") as execute:
            with self.assertRaisesRegex(ValueError, "invalid"):
                rb.get()
            execute.assert_not_called()
        with patch.object(rb.reportview, "get_form_params", return_value=frappe._dict(doctype="Purchase Invoice", fields=["item"])), \
                patch.object(rb.reportview, "execute", side_effect=frappe.PermissionError):
            with self.assertRaises(frappe.PermissionError):
                rb.get()

    def test_snapshot_requested_fields_before_query_mutation(self):
        args = frappe._dict(doctype="Purchase Invoice", fields=["rate as harmless", "qty"])
        def execute(**kwargs):
            kwargs["fields"][:] = ["qty"]
            return [frappe._dict(harmless=1234, qty=3)]
        with patch.object(rb.reportview, "get_form_params", return_value=args), \
                patch.object(rb.reportview, "execute", side_effect=execute):
            self.assertEqual(rb.get()["values"], [[None, 3]])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(rb, "get_policy", return_value=RESTRICTED))
        self.stack.enter_context(patch.object(frappe, "conf", SimpleNamespace(read_from_replica=False)))
        self.stack.enter_context(patch.object(frappe, "local", SimpleNamespace(lang="en")))
        self.stack.enter_context(patch.object(frappe, "session", SimpleNamespace(user="alice")))
        self.stack.enter_context(patch.object(frappe, "get_meta", side_effect=meta))
        self.stack.enter_context(patch.object(rb, "_", side_effect=lambda value, **kw: value))
        self.stack.enter_context(patch.object(rb.reportview, "_", side_effect=lambda value, **kw: value))
        self.stack.enter_context(patch("frappe.core.doctype.access_log.access_log.make_access_log"))
        self.can_export = self.stack.enter_context(patch.object(frappe.permissions, "can_export", return_value=True))
        self.stack.enter_context(patch.object(frappe, "get_system_settings", side_effect=lambda key: {
            "date_format": "yyyy-mm-dd", "time_format": "HH:mm:ss"
        }.get(key)))
        self.stack.enter_context(patch("frappe.locale.get_number_format", return_value=SimpleNamespace(
            thousands_separator=",", precision=2)))
        self.stack.enter_context(patch.object(frappe, "db", SimpleNamespace(get_default=lambda key: None)))

    def query(self, rows):
        query = SimpleNamespace(fields=None)
        def execute(**kwargs):
            query.fields = kwargs["fields"]
            self.executed = deepcopy(kwargs)
            return rows
        query.execute = execute
        self.stack.enter_context(patch.object(rb.reportview, "DatabaseQuery", return_value=query))
        return query

    def test_mapping_signature_and_unrestricted_delegation(self):
        self.assertEqual(inspect.signature(rb.export_query), inspect.signature(rb.reportview.export_query))
        self.assertIn(rb.export_query, frappe.whitelisted)
        self.assertEqual(rb.OVERRIDE_WHITELISTED_METHODS["frappe.desk.reportview.export_query"],
                         "cost_visibility.report_builder.export_query")
        p = params()
        with patch.object(rb, "get_policy", return_value=OPEN), \
                patch.object(rb.reportview, "export_query", return_value=object()) as endpoint, \
                patch.object(rb.reportview, "_export_query", return_value=object()) as internal:
            self.assertIs(rb.export_query(), endpoint.return_value)
            self.assertIs(rb._export_query(p, CSV, False), internal.return_value)
            internal.assert_called_once_with(p, CSV, populate_response=False)

    def test_csv_and_excel_filter_aligned_rows_and_totals(self):
        for fmt in ("CSV", "Excel"):
            with self.subTest(fmt=fmt), patch.object(rb.reportview, "DatabaseQuery") as factory:
                query = factory.return_value
                def execute(**kwargs):
                    query.fields = kwargs["fields"]
                    self.assertEqual(kwargs["fields"], ["item", "rate", "qty", "`tabPurchase Invoice`.`owner`"])
                    self.assertTrue(kwargs["save_user_settings"])
                    return [("A", 1234.0, 2, "alice"), ("B", 2000.0, 3, "alice")]
                query.execute.side_effect = execute
                name, extension, content = rb._export_query(
                    params(file_format_type=fmt, add_totals_row="1"), CSV.copy(), False)
                self.assertEqual(name, "Purchase Report")
                if extension == "csv":
                    rows = list(csv.reader(io.StringIO(content.decode())))
                else:
                    rows = list(load_workbook(io.BytesIO(content), data_only=True).active.values)
                self.assertEqual(list(rows[0]), ["Sr", "Item", "Quantity", "owner"])
                self.assertEqual([str(x) for x in rows[1]], ["1", "A", "2", "alice"])
                self.assertEqual([str(x or "") for x in rows[3]], ["3", "Total", "5", ""])
                self.assertNotIn("1234", str(rows))
                self.assertNotIn("3234", str(rows))

    def test_v15_csv_and_excel_without_style_api(self):
        from frappe.utils import xlsxutils

        calls = []
        def legacy_make_xlsx(data, sheet_name, wb=None, column_widths=None):
            calls.append(deepcopy(data))
            return xlsxutils.make_xlsx(data, sheet_name)
        legacy = SimpleNamespace(handle_html=xlsxutils.handle_html, make_xlsx=legacy_make_xlsx)
        v15_info = [
            {"name": "item", "label": "Item", "fieldtype": "Data", "translatable": False},
            {"name": "rate", "label": "Rate", "fieldtype": "Data", "translatable": False},
            {"name": "qty", "label": "Quantity", "fieldtype": "Data", "translatable": False},
            {"name": "owner", "label": "owner", "fieldtype": "Data", "translatable": False},
        ]
        self.query([("A", 1234, 2, "alice")])
        with patch.object(frappe.utils, "xlsxutils", legacy), \
                patch.object(rb.reportview, "get_field_info", return_value=v15_info):
            for fmt in ("CSV", "Excel"):
                with self.subTest(fmt=fmt):
                    _, extension, content = rb._export_query(params(file_format_type=fmt), CSV.copy(), False)
                    rows = (list(csv.reader(io.StringIO(content.decode()))) if extension == "csv" else
                            list(load_workbook(io.BytesIO(content), data_only=True).active.values))
                    self.assertEqual(list(rows[0]), ["Sr", "Item", "Quantity", "owner"])
                    self.assertEqual([str(value) for value in rows[1]], ["1", "A", "2", "alice"])
                    self.assertNotIn("1234", str(rows))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ["Sr", "Item", "Quantity", "owner"])

    def test_v16_excel_styles_use_filtered_columns_and_rows(self):
        from frappe.utils import xlsxutils

        self.query([("A", 1234, 2, "alice")])
        with patch.object(xlsxutils, "get_default_xlsx_styles", wraps=xlsxutils.get_default_xlsx_styles) as styles, \
                patch.object(xlsxutils, "make_xlsx", wraps=xlsxutils.make_xlsx) as make:
            rb._export_query(params(file_format_type="Excel", add_totals_row="1"), CSV.copy(), False)
        self.assertEqual([column["label"] for column in styles.call_args.kwargs["columns"]],
                         ["Sr", "Item", "Quantity", "owner"])
        self.assertNotIn("1234", str(styles.call_args.kwargs["data"]))
        self.assertTrue(styles.call_args.kwargs["has_total_row"])
        self.assertIn("styles", make.call_args.kwargs)

    def test_unknown_export_expressions_are_omitted(self):
        info = {"fieldname": "qty", "label": "Quantity"}
        for field in ("mystery(qty)", "qty + rate", "*", "sum(*)", "'x' || rate || 'y'",
                      {"MYSTERY": ["qty"], "as": "safe"},
                      {"SUM": {"MYSTERY": ["qty"]}, "as": "safe"}):
            with self.subTest(field=field):
                self.assertTrue(rb._sensitive(field, info, "Purchase Invoice", "Report", []))
        self.assertFalse(rb._sensitive("sum(qty)", info, "Purchase Invoice", "Report", []))
        self.assertTrue(rb._sensitive("sum(rate)", info, "Purchase Invoice", "Report", []))

    def test_unknown_export_columns_are_removed_from_csv(self):
        fields = ["item", "mystery(qty) as safe", {"SUM": {"MYSTERY": ["qty"]}, "as": "nested"}]
        self.query([("A", 1234, 5678, "alice")])
        _, _, content = rb._export_query(params(fields=fields), CSV.copy(), False)
        self.assertEqual(list(csv.reader(io.StringIO(content.decode()))),
                         [["Sr", "Item", "owner"], ["1", "A", "alice"]])

    def test_unrestricted_v15_delegates_without_compatibility_apis(self):
        from frappe.database import query

        p = params()
        payload = object()
        with patch.object(frappe, "__version__", "15.126.1"), \
                patch.object(rb, "get_policy", return_value=OPEN), \
                patch.object(frappe.utils, "xlsxutils", SimpleNamespace()), \
                patch.dict(query.__dict__), \
                patch.object(rb.reportview, "get", return_value=payload) as get, \
                patch.object(rb.reportview, "export_query", return_value=payload) as export, \
                patch.object(rb.reportview, "_export_query", return_value=payload) as internal:
            query.__dict__.pop("FUNCTION_MAPPING", None)
            query.__dict__.pop("OPERATOR_MAPPING", None)
            self.assertIs(rb.get(), payload)
            self.assertIs(rb.export_query(), payload)
            self.assertIs(rb._export_query(p, CSV, False), payload)
        get.assert_called_once_with()
        export.assert_called_once_with()
        internal.assert_called_once_with(p, CSV, populate_response=False)

    def test_child_alias_label_and_aggregate_operand(self):
        fields = ["`tabPurchase Invoice Item`.`rate` as 'Purchase Invoice Item:rate'",
                  "`tabPurchase Invoice Item`.`qty` as 'Purchase Invoice Item:qty'",
                  "masked as safe", {"SUM": "`tabPurchase Invoice Item`.`rate`", "as": "_aggregate_column"},
                  "item"]
        self.query([(1234, 2, 5678, 9999, "A", "alice")])
        _, _, content = rb._export_query(params(fields=fields), CSV.copy(), False)
        rows = list(csv.reader(io.StringIO(content.decode())))
        self.assertEqual(rows[0], ["Sr", "Quantity (Purchase Invoice Item)", "Item", "owner"])
        self.assertEqual(rows[1], ["1", "2", "A", "alice"])

    def test_matcher_sees_alias_original_label_and_sales_context(self):
        info = {"fieldname": "Rate", "label": "Rate"}
        self.assertTrue(rb._sensitive("masked as harmless", info, "Purchase Invoice", "Report", []))
        self.assertTrue(rb._sensitive("item as supplier_cost", info, "Purchase Invoice", "Report", RESTRICTED["patterns"]))
        self.assertTrue(rb._sensitive({"AVG": "`tabPurchase Invoice Item`.`rate`", "as": "average"},
                                      info, "Sales Invoice", "Report", []))
        self.assertFalse(rb._sensitive("rate", info, "Sales Invoice", "Sales Report", []))
        self.assertTrue(rb._sensitive("items.rate", info, "Purchase Invoice", "Report", []))
        self.assertFalse(rb._sensitive("`tabSales Invoice Item`.`rate`", info,
                                       "Purchase Invoice", "Report", []))

    def test_owner_only_permission_granted_before_output_filtering(self):
        self.query([("A", 1234, 2, "alice")])
        self.can_export.side_effect = [False, True]
        _, _, content = rb._export_query(params(), CSV.copy(), False)
        self.assertNotIn("1234", content.decode())
        self.assertIn("A,2,alice", content.decode())

    def test_translation_duration_and_filtered_columns_stay_aligned(self):
        self.query([("Open", 1234, 90, "alice")])
        state = META["Purchase Invoice"]["state"]
        with patch.dict(state, {"translatable": True}), \
                patch.object(frappe, "local", SimpleNamespace(lang="fr")), \
                patch.object(rb, "_", side_effect=lambda value, **kw: "Ouvert" if value == "Open" else value), \
                patch.object(rb.reportview, "format_duration", return_value="1m 30s"):
            _, _, content = rb._export_query(
                params(fields=["state", "rate", "elapsed"], translate_values="1"), CSV.copy(), False)
        rows = list(csv.reader(io.StringIO(content.decode())))
        self.assertEqual(rows[0], ["Sr", "State", "Elapsed", "owner"])
        self.assertEqual(rows[1], ["1", "Ouvert", "1m 30s", "alice"])

    def test_real_validation_and_foreground_response(self):
        self.query([("A", 1234, 2, "alice")])
        form = params(fields='["item", "rate", "qty"]', file_format_type="CSV", user="spoof",
                      ignore_permissions=1, export_in_background="0")
        local = SimpleNamespace(lang="en", form_dict=form)
        fake_meta = meta("Purchase Invoice")
        fake_meta.get_high_permlevel_fields = lambda: []
        with patch.object(frappe, "local", local), \
                patch.object(frappe, "get_meta", side_effect=lambda name: fake_meta if name == "Purchase Invoice" else meta(name)), \
                patch("frappe.desk.utils.provide_binary_file") as provide:
            rb.export_query()
        self.assertNotIn("user", self.executed)
        self.assertNotIn("ignore_permissions", self.executed)
        self.assertTrue(self.executed["as_list"])
        self.assertEqual(self.executed["fields"][:3], ["item", "rate", "qty"])
        self.assertNotIn(b"1234", provide.call_args.args[2])

    def test_validation_failure_prevents_export_or_enqueue(self):
        with patch.object(rb.reportview, "get_form_params", side_effect=ValueError("invalid field")), \
                patch.object(rb, "_export_query") as export, patch.object(frappe, "enqueue") as enqueue:
            with self.assertRaisesRegex(ValueError, "invalid field"):
                rb.export_query()
        export.assert_not_called()
        enqueue.assert_not_called()

    def test_owner_permissions_before_filtering_and_query_selection(self):
        self.query([("A", 1234, 2, "bob")])
        self.can_export.side_effect = [False, True]
        with self.assertRaises(frappe.PermissionError):
            rb._export_query(params(selected_items='["A"]', filters={"other": 1}), CSV.copy(), False)
        self.assertEqual(self.executed["filters"], {"name": ("in", ["A"])})
        self.assertEqual(self.can_export.call_args_list[1].kwargs, {"is_owner": True})

    def test_no_export_permission_and_query_errors_propagate(self):
        query = self.query([])
        self.can_export.return_value = False
        with self.assertRaises(frappe.PermissionError):
            rb._export_query(params(), CSV.copy(), False)
        query.execute = lambda **kwargs: (_ for _ in ()).throw(ValueError("query error"))
        with self.assertRaisesRegex(ValueError, "query error"):
            rb._export_query(params(), CSV.copy(), False)

    def test_positional_mismatch_fails_closed(self):
        self.query([("A", 1234, 2, "alice", "unexpected")])
        with patch.object(frappe, "throw", side_effect=ValueError("alignment")):
            with self.assertRaisesRegex(ValueError, "alignment"):
                rb._export_query(params(), CSV.copy(), False)

    def test_endpoint_uses_core_validation_and_background_job(self):
        form = params(export_in_background="1", csv_delimiter=";")
        with patch.object(rb.reportview, "get_form_params", return_value=form) as validate, \
                patch.object(frappe, "get_cached_value", return_value="alice@example.com"), \
                patch.object(frappe, "flags", SimpleNamespace(in_test=False)), \
                patch.object(frappe, "enqueue") as enqueue, patch.object(frappe, "msgprint"):
            rb.export_query()
        validate.assert_called_once_with()
        self.assertEqual(enqueue.call_args.args[0], "cost_visibility.report_builder.run_report_view_export_job")
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["queue"], "long")
        self.assertEqual(kwargs["user_email"], "alice@example.com")
        self.assertTrue(kwargs["form_params"]["as_list"])
        self.assertIsNone(kwargs["form_params"]["limit_page_length"])
        self.assertEqual(kwargs["csv_params"]["delimiter"], ";")

    def test_job_checks_policy_at_execution_and_preserves_email_contract(self):
        p = params()
        with patch.object(rb, "get_policy", return_value=OPEN) as policy, \
                patch.object(rb.reportview, "_export_query", return_value=("Title", "csv", b"content")) as core, \
                patch("frappe.desk.utils.send_report_email") as send:
            rb.run_report_view_export_job("alice@example.com", p, CSV.copy())
        policy.assert_called_once_with()
        core.assert_called_once()
        send.assert_called_once_with("alice@example.com", "Title", "csv", b"content", attached_to_name="Title")

    def test_restricted_job_serializes_filtered_content(self):
        self.query([("A", 1234, 2, "alice")])
        with patch("frappe.desk.utils.send_report_email") as send:
            rb.run_report_view_export_job("alice@example.com", params(), CSV.copy())
        self.assertNotIn(b"1234", send.call_args.args[3])
        self.assertIn(b"A,2,alice", send.call_args.args[3])


if __name__ == "__main__":
    unittest.main()
