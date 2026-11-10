import inspect
import io
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from openpyxl import load_workbook

from cost_visibility import report_filter as rf


RESTRICTED = {"restricted": True, "patterns": ["valuation", "supplier cost"]}
OPEN = {"restricted": False, "patterns": []}


def sample():
    return {
        "columns": [
            {"fieldname": "item", "label": "Item", "fieldtype": "Data", "width": 90},
            {"fieldname": "valuation_rate", "label": "Valuation Rate", "fieldtype": "Currency"},
            {"fieldname": "qty", "label": "Qty", "fieldtype": "Float", "width": 70},
        ],
        "result": [
            {"item": "A", "valuation_rate": 1234, "qty": 2, "secret": 5678, "indent": 1},
            {"item": "B", "valuation_rate": 2345, "qty": 3},
            ["Total", 3579, 5],
        ],
        "add_total_row": 1,
        "skip_total_row": 0,
        "chart": {"data": [1234]},
        "report_summary": [{"value": 1234}],
        "message": "cost 1234",
        "extra": {"cost": 1234},
    }


def params(**kwargs):
    values = dict(report_name="Stock Report", file_format_type="CSV", filters={},
                  custom_columns=[], visible_idx=[], ignore_visible_idx=False,
                  include_indentation=False, include_filters=True, include_hidden_columns=True,
                  applied_filters={"anything": "SECRET FILTER"}, export_in_background=0)
    values.update(kwargs)
    return frappe._dict(values)


class FilterTests(unittest.TestCase):
    def test_unrestricted_returns_same_object(self):
        data = sample()
        self.assertIs(rf.filter_result(data, OPEN, "Report", "Item"), data)

    def test_dict_list_tuple_totals_and_unknown_keys(self):
        data = sample()
        data["result"].insert(2, ("C", 7777, 4))
        original = deepcopy(data)
        result = rf.filter_result(data, RESTRICTED, "Stock Report", "Item")
        self.assertEqual([c["fieldname"] for c in result["columns"]], ["item", "qty"])
        self.assertEqual(result["result"], [
            {"item": "A", "qty": 2, "indent": 1}, {"item": "B", "qty": 3},
            ["C", 4], ["Total", 5],
        ])
        self.assertEqual(data, original)
        self.assertIsNone(result["chart"])
        self.assertIsNone(result["report_summary"])
        self.assertIsNone(result["message"])
        self.assertNotIn("extra", result)

    def test_purchase_context_and_legacy_columns(self):
        data = {"columns": ["Item:Data:100", "Rate:Currency:90", "Qty:Float:80"],
                "result": [["A", 100, 4]]}
        result = rf.filter_result(data, RESTRICTED, "Purchase Register", "Purchase Invoice")
        self.assertEqual([c["fieldname"] for c in result["columns"]], ["item", "qty"])
        self.assertEqual(result["result"], [["A", 4]])
        sales = rf.filter_result(data, RESTRICTED, "Sales Register", "Sales Invoice")
        self.assertEqual(sales["result"], [["A", 100, 4]])

    def test_nested_metadata_duplicate_alias_and_short_rows(self):
        data = {"columns": [
            {"fieldname": "alias", "label": "Price", "df": {"fieldname": "valuation_rate"}},
            {"fieldname": "alias", "label": "Other"},
            {"fieldname": "qty", "label": "Qty", "options": "valuation_rate", "extra": "SECRET"},
        ], "result": [{"alias": 1234, "qty": {"secret": 1234}}, [1]]}
        result = rf.filter_result(data, RESTRICTED, "Report", "Item")
        self.assertEqual(result["columns"], [{"fieldname": "qty", "label": "Qty"}])
        self.assertEqual(result["result"], [{"qty": None}, [None]])

    def test_prepared_data_is_filtered_and_document_is_not_exposed(self):
        data = sample() | {"prepared_report": True,
                           "doc": {"name": "PR-1", "filters": '{"cost":1234}',
                                   "report_end_time": "2026-01-01", "secret": 1234},
                           "custom_filters": {"secret": 1234}}
        result = rf.filter_result(data, RESTRICTED, "Report", "Item")
        self.assertEqual(result["doc"], {"filters": "{}", "report_end_time": "2026-01-01"})
        self.assertEqual(result["custom_filters"], {"secret": 1234})
        self.assertNotIn("valuation_rate", result["result"][0])
        empty = rf.filter_result({"prepared_report": True, "doc": None}, RESTRICTED, "Report", "Item")
        self.assertEqual(empty["result"], [])
        self.assertIsNone(empty["doc"])

    def test_custom_report_columns_and_safe_filters_survive(self):
        data = {
            "columns": [
                {"fieldname": "item", "label": "Item"},
                {"fieldname": "description-item", "id": "description-item", "label": "Description",
                 "doctype": "Item", "link_field": {"fieldname": "item", "names": ["private"]},
                 "insert_after_index": 0},
                {"fieldname": "valuation_rate", "label": "Valuation Rate"},
            ],
            "result": [{"item": "A", "description-item": "Safe", "valuation_rate": 99,
                        "removed_field": "not returned"}],
            "custom_filters": {"company": "Example", "warehouses": ["A", "B"],
                               "valuation_rate": 99, "nested": {"secret": 99}},
        }
        result = rf.filter_result(data, RESTRICTED, "Saved Custom Report", "Item")
        self.assertEqual([col["fieldname"] for col in result["columns"]], ["item", "description-item"])
        self.assertEqual(result["columns"][1]["link_field"], {"fieldname": "item"})
        self.assertEqual(result["columns"][1]["id"], "description-item")
        self.assertEqual(result["columns"][1]["insert_after_index"], 0)
        self.assertEqual(result["result"], [{"item": "A", "description-item": "Safe"}])
        self.assertEqual(result["custom_filters"], {"company": "Example", "warehouses": ["A", "B"]})

    def test_label_only_data_and_blocked_label_collision(self):
        data = {"columns": [{"fieldname": "item", "label": "Item"},
                            {"fieldname": "secret", "label": "Supplier Cost"},
                            {"fieldname": "safe", "label": "Supplier Cost"}],
                "result": [{"Item": "A", "Supplier Cost": 99, "safe": 99}]}
        result = rf.filter_result(data, RESTRICTED, "Report", "Item")
        self.assertEqual(result["result"], [{"Item": "A"}])

    def test_all_columns_removed_produces_empty_report(self):
        data = {"columns": [{"fieldname": "valuation_rate", "label": "Valuation"}],
                "result": [[100], [100]], "add_total_row": 1}
        result = rf.filter_result(data, RESTRICTED, "Report", "Item")
        self.assertEqual(result["columns"], [])
        self.assertEqual(result["result"], [])
        self.assertEqual(result["add_total_row"], 0)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        config = patch.object(frappe, "conf", SimpleNamespace(read_from_replica=False))
        config.start()
        self.addCleanup(config.stop)

    def test_signatures_and_mapping(self):
        self.assertEqual(inspect.signature(rf.run), inspect.signature(rf.query_report.run))
        self.assertEqual(inspect.signature(rf.export_query), inspect.signature(rf.query_report.export_query))
        self.assertIn(rf.run, frappe.whitelisted)
        self.assertIn(rf.export_query, frappe.whitelisted)
        self.assertEqual(len(rf.OVERRIDE_WHITELISTED_METHODS), 2)

    def test_run_delegates_all_arguments_and_filters_prepared_at_read_time(self):
        payload = sample() | {"prepared_report": True, "doc": None}
        with patch.object(rf.query_report, "run", return_value=payload) as core, \
                patch.object(rf.query_report, "get_report_doc", return_value=SimpleNamespace(ref_doctype="Item")), \
                patch.object(rf, "get_policy", return_value=RESTRICTED):
            result = rf.run("Report", {"x": 1}, "spoofed", True, ["custom"], True, "parent", False, ["js"])
        core.assert_called_once_with(report_name="Report", filters={"x": 1}, user="spoofed",
                                     ignore_prepared_report=True, custom_columns=["custom"], is_tree=True,
                                     parent_field="parent", are_default_filters=False, js_filters=["js"])
        self.assertNotIn("valuation_rate", result["result"][0])

    def test_run_with_legacy_eight_argument_api_and_custom_reference(self):
        calls = []

        def legacy_run(report_name, filters=None, user=None, ignore_prepared_report=False,
                       custom_columns=None, is_tree=False, parent_field=None, are_default_filters=True):
            calls.append((report_name, filters, user, ignore_prepared_report, custom_columns,
                          is_tree, parent_field, are_default_filters))
            return sample()

        columns = [{"fieldname": "custom", "link_field": {"fieldname": "item"}}]
        with patch.object(rf.query_report, "run", legacy_run), \
                patch.object(rf.query_report, "get_report_doc", return_value=SimpleNamespace(ref_doctype="Item")) as lookup, \
                patch.object(rf, "get_policy", return_value=RESTRICTED):
            result = rf.run("Saved Custom", {"company": "Example"}, "user", True, columns,
                            True, "parent", False, ["newer-only"])
        self.assertEqual(calls, [("Saved Custom", {"company": "Example"}, "user", True,
                                  columns, True, "parent", False)])
        lookup.assert_called_once_with("Saved Custom")
        self.assertNotIn("valuation_rate", result["result"][0])

    def test_run_does_not_retry_report_type_errors(self):
        calls = []

        def legacy_run(report_name, filters=None, user=None, ignore_prepared_report=False,
                       custom_columns=None, is_tree=False, parent_field=None, are_default_filters=True):
            calls.append(report_name)
            raise TypeError("report script failure")

        with patch.object(rf.query_report, "run", legacy_run):
            with self.assertRaisesRegex(TypeError, "report script failure"):
                rf.run("Report", js_filters=["newer-only"])
        self.assertEqual(calls, ["Report"])

    def test_run_permission_exception_is_not_caught(self):
        with patch.object(rf.query_report, "run", side_effect=frappe.PermissionError), \
                patch.object(rf, "get_policy") as policy:
            with self.assertRaises(frappe.PermissionError):
                rf.run("Report")
            policy.assert_not_called()

    def test_run_unrestricted_is_identical_and_no_second_report_lookup(self):
        payload = sample()
        with patch.object(rf.query_report, "run", return_value=payload), \
                patch.object(rf, "get_policy", return_value=OPEN), \
                patch.object(rf.query_report, "get_report_doc") as lookup:
            self.assertIs(rf.run("Report", ignore_prepared_report=True), payload)
            lookup.assert_not_called()

    def test_unrestricted_export_uses_core_unchanged(self):
        p = params()
        with patch.object(rf, "get_policy", return_value=OPEN), \
                patch.object(rf.query_report, "_export_query", return_value="original") as core:
            self.assertEqual(rf._export_query(p, {}, False), "original")
            core.assert_called_once_with(p, {}, populate_response=False)

    def test_unrestricted_export_endpoint_delegates_without_touching_request(self):
        class NoFormAccess:
            @property
            def form_dict(self):
                raise AssertionError("unrestricted wrapper accessed form arguments")

        result = object()
        with patch.object(rf, "get_policy", return_value=OPEN), \
                patch.object(rf.frappe, "local", NoFormAccess()), \
                patch.object(rf.query_report, "export_query", return_value=result) as core, \
                patch.object(rf, "_check_export") as check, \
                patch.object(rf, "_export_query") as export, \
                patch.object(rf.frappe, "enqueue") as enqueue:
            self.assertIs(rf.export_query(), result)
        core.assert_called_once_with()
        check.assert_not_called()
        export.assert_not_called()
        enqueue.assert_not_called()

    def test_no_safe_export_columns_returns_no_file(self):
        data = {"columns": [{"fieldname": "valuation_rate", "label": "Valuation"}],
                "result": [[100]]}
        with patch.object(rf, "get_policy", return_value=RESTRICTED), \
                patch.object(rf.query_report, "run", return_value=data), \
                patch.object(rf.query_report, "get_report_doc", return_value=SimpleNamespace(ref_doctype="Item")), \
                patch.object(rf.frappe, "respond_as_web_page") as no_data, \
                patch.object(rf, "_", side_effect=lambda s: s):
            self.assertIsNone(rf._export_query(params(), {}, False))
        no_data.assert_called_once()

    def test_export_rerun_permission_exception_is_not_caught(self):
        with patch.object(rf, "get_policy", return_value=RESTRICTED), \
                patch.object(rf.query_report, "run", side_effect=frappe.PermissionError), \
                patch.object(rf.query_report, "get_report_doc") as lookup:
            with self.assertRaises(frappe.PermissionError):
                rf._export_query(params(), {}, False)
            lookup.assert_not_called()

    def test_foreground_and_background_permission_check(self):
        with patch.object(rf.frappe, "get_cached_value", return_value="Item"), \
                patch.object(rf.frappe.permissions, "can_export", side_effect=frappe.PermissionError):
            with self.assertRaises(frappe.PermissionError):
                rf._check_export("Report")
            with self.assertRaises(frappe.PermissionError):
                rf.run_export_query_job(params(), {})

    def test_background_uses_current_user_and_settings_not_client_email(self):
        p = params(user_email="attacker@example.com")
        with patch.object(rf, "_check_export") as check, \
                patch.object(rf.frappe, "session", SimpleNamespace(user="actual@example.com")), \
                patch.object(rf.frappe, "get_cached_value", return_value="actual@example.com") as lookup, \
                patch.object(rf, "_export_query", return_value=("Report", "csv", b"data")) as export, \
                patch("frappe.desk.utils.send_report_email") as send:
            rf.run_export_query_job(p, {})
        check.assert_called_once_with("Stock Report")
        lookup.assert_called_once_with("User", "actual@example.com", "email")
        export.assert_called_once_with(p, {}, populate_response=False)
        send.assert_called_once_with("actual@example.com", "Report", "csv", b"data", attached_to_name="Stock Report")

    def test_real_core_run_ignores_custom_user(self):
        with patch.object(rf.query_report, "_run", return_value={}) as internal:
            rf.query_report.run("Report", user="spoofed@example.com", ignore_prepared_report=True)
        self.assertNotIn("user", internal.call_args.kwargs)
        self.assertTrue(internal.call_args.kwargs["ignore_prepared_report"])

    def test_export_endpoint_checks_permission_and_enqueues_own_job(self):
        p = params(export_in_background=1, user="attacker", ignore_permissions=1)
        local = SimpleNamespace(form_dict=p)
        with patch.object(rf, "get_policy", return_value=RESTRICTED), \
                patch.object(rf.frappe, "local", local), \
                patch.object(rf.frappe, "session", SimpleNamespace(user="actual@example.com")), \
                patch.object(rf.frappe, "flags", SimpleNamespace(in_test=False)), \
                patch.object(rf.frappe, "get_cached_value", return_value="actual@example.com"), \
                patch.object(rf, "_check_export") as check, \
                patch.object(rf.frappe, "enqueue") as enqueue, \
                patch.object(rf.frappe, "msgprint"), patch.object(rf, "_", side_effect=lambda s: s):
            rf.export_query()
        check.assert_called_once_with("Stock Report")
        args, kwargs = enqueue.call_args
        self.assertEqual(args[0], "cost_visibility.report_filter.run_export_query_job")
        self.assertNotIn("user", kwargs["form_params"])
        self.assertNotIn("ignore_permissions", kwargs["form_params"])
        self.assertNotIn("user_email", kwargs)

    def test_foreground_export_does_not_run_after_permission_denied(self):
        with patch.object(rf, "get_policy", return_value=RESTRICTED), \
                patch.object(rf.frappe, "local", SimpleNamespace(form_dict=params())), \
                patch.object(rf, "_check_export", side_effect=frappe.PermissionError), \
                patch.object(rf, "_export_query") as export:
            with self.assertRaises(frappe.PermissionError):
                rf.export_query()
            export.assert_not_called()

    def test_job_uses_policy_at_execution_not_enqueue(self):
        p = params()
        with patch.object(rf, "_check_export"), \
                patch.object(rf.frappe, "session", SimpleNamespace(user="actual@example.com")), \
                patch.object(rf.frappe, "get_cached_value", return_value="actual@example.com"), \
                patch.object(rf, "get_policy", return_value=RESTRICTED) as policy, \
                patch.object(rf.query_report, "run", return_value=sample()), \
                patch.object(rf.query_report, "get_report_doc", return_value=SimpleNamespace(ref_doctype="Item")), \
                patch.object(rf.query_report, "_", side_effect=lambda s, **kw: s), \
                patch("frappe.desk.utils.send_report_email") as send:
            rf.run_export_query_job(p, {"delimiter": ",", "quoting": 0, "decimal_sep": "."})
        policy.assert_called_once()
        content = send.call_args.args[3]
        self.assertNotIn(b"1234", content)

    def test_visible_rows_recalculate_sanitized_total(self):
        with patch.object(rf, "get_policy", return_value=RESTRICTED), \
                patch.object(rf.query_report, "run", return_value=sample()), \
                patch.object(rf.query_report, "get_report_doc", return_value=SimpleNamespace(ref_doctype="Item")), \
                patch.object(rf.query_report, "_", side_effect=lambda s, **kw: s):
            _, _, content = rf._export_query(
                params(visible_idx=[0]), {"delimiter": ",", "quoting": 0, "decimal_sep": "."}, False
            )
        text = content.decode()
        self.assertIn("A,2", text)
        self.assertIn("Total,2", text)
        self.assertNotIn("B,3", text)
        self.assertNotIn("1234", text)

    def test_visible_indices_do_not_count_original_total_as_data(self):
        data = sample()
        data["result"].insert(2, {"item": "C", "qty": 4, "valuation_rate": 42})
        with patch.object(rf, "get_policy", return_value=RESTRICTED), \
                patch.object(rf.query_report, "run", return_value=data), \
                patch.object(rf.query_report, "get_report_doc", return_value=SimpleNamespace(ref_doctype="Item")), \
                patch.object(rf.query_report, "_", side_effect=lambda s, **kw: s):
            _, _, content = rf._export_query(
                params(visible_idx=[0, 3]), {"delimiter": ",", "quoting": 0, "decimal_sep": "."}, False
            )
        self.assertIn("Total,2", content.decode())
        self.assertNotIn("Total,7", content.decode())

    def test_csv_and_excel_use_sanitized_data_including_total(self):
        for format_type in ("CSV", "Excel"):
            with self.subTest(format_type=format_type), \
                    patch.object(rf, "get_policy", return_value=RESTRICTED), \
                    patch.object(rf.query_report, "run", return_value=sample()) as core, \
                    patch.object(rf.query_report, "get_report_doc", return_value=SimpleNamespace(ref_doctype="Item")), \
                    patch.object(rf.query_report, "_", side_effect=lambda x, **kw: x), \
                    patch.object(rf.frappe, "get_system_settings", side_effect=lambda key: {
                        "date_format": "yyyy-mm-dd", "time_format": "HH:mm:ss"
                    }.get(key)), \
                    patch.object(rf.query_report, "get_xlsx_styles", return_value=None):
                name, ext, content = rf._export_query(
                    params(file_format_type=format_type), {"delimiter": ",", "quoting": 0, "decimal_sep": "."}, False
                )
                self.assertEqual(name, "Stock Report")
                core.assert_called_once()
                if ext == "csv":
                    text = content.decode()
                else:
                    workbook = load_workbook(io.BytesIO(content), data_only=True)
                    text = str(list(workbook.active.values))
                self.assertIn("Item", text)
                self.assertIn("Qty", text)
                self.assertNotIn("Valuation", text)
                self.assertNotIn("1234", text)
                self.assertNotIn("3579", text)
                self.assertNotIn("SECRET FILTER", text)

    def test_legacy_export_builder_uses_two_values_and_filters_hidden_positional_cells(self):
        from frappe.utils.xlsxutils import make_xlsx as current_make_xlsx

        captured = []

        def legacy_build(data, visible_idx, include_indentation, include_filters=False,
                         ignore_visible_idx=False, include_hidden_columns=False):
            captured.append((data.columns, data.result, visible_idx))
            return ([col["label"] for col in data.columns], *data.result), [10] * len(data.columns)

        def legacy_make_xlsx(data, sheet_name, wb=None, column_widths=None):
            return current_make_xlsx(data, sheet_name, column_widths=column_widths)

        data = {"columns": [{"fieldname": "item", "label": "Item"},
                            {"fieldname": "valuation_rate", "label": "Valuation Rate"},
                            {"fieldname": "hidden_col", "label": "Hidden", "hidden": 1},
                            {"fieldname": "qty", "label": "Qty"}],
                "result": [["A", 1234, "HIDDEN", 3]]}
        for file_format in ("CSV", "Excel"):
            with self.subTest(file_format=file_format), \
                    patch.object(rf, "get_policy", return_value=RESTRICTED), \
                    patch.object(rf.query_report, "run", return_value=deepcopy(data)), \
                    patch.object(rf.query_report, "get_report_doc", return_value=SimpleNamespace(ref_doctype="Item")), \
                    patch.object(rf.query_report, "build_xlsx_data", legacy_build), \
                    patch("frappe.utils.xlsxutils.make_xlsx", legacy_make_xlsx), \
                    patch.object(rf.frappe, "get_system_settings", side_effect=lambda key: {
                        "date_format": "yyyy-mm-dd", "time_format": "HH:mm:ss"
                    }.get(key)):
                _, ext, content = rf._export_query(params(file_format_type=file_format, include_hidden_columns=False),
                                                   {"delimiter": ",", "quoting": 0, "decimal_sep": "."}, False)
            if ext == "csv":
                text = content.decode()
            else:
                workbook = load_workbook(io.BytesIO(content))
                self.assertEqual(workbook.active.title, "Query Report")
                text = str(list(workbook.active.values))
            self.assertIn("Item", text)
            self.assertIn("Qty", text)
            self.assertNotIn("1234", text)
            self.assertNotIn("HIDDEN", text)
        self.assertEqual(captured[0][1], [["A", 3]])


if __name__ == "__main__":
    unittest.main()
