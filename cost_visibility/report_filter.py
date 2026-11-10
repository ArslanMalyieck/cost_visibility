"""Optional whitelisted query-report overrides, not an authorization boundary.

Install OVERRIDE_WHITELISTED_METHODS in hooks to activate. Other endpoints,
including Report Builder and prepared-report attachment downloads, are untouched.
"""

import datetime
import json
from decimal import Decimal

import frappe
from frappe import _
from frappe.desk import query_report
from frappe.desk.reportview import clean_params, parse_json
from frappe.utils import cint, sbool

from cost_visibility.api import get_policy
from cost_visibility.matching import matches


OVERRIDE_WHITELISTED_METHODS = {
    "frappe.desk.query_report.run": "cost_visibility.report_filter.run",
    "frappe.desk.query_report.export_query": "cost_visibility.report_filter.export_query",
}

_COLUMN_KEYS = frozenset({
    "fieldname", "label", "fieldtype", "options", "width", "precision", "hidden",
    "align", "editable", "bold", "is_tree", "convertible", "doctype", "parent",
})
_SCALARS = (str, int, float, bool, Decimal, datetime.date, datetime.time, datetime.timedelta)


def _scalar(value):
    return value if value is None or isinstance(value, _SCALARS) else None


def filter_result(data, policy, report_name, ref_doctype):
    """Filter a core result at read time, including old prepared result shapes."""
    if not policy.get("restricted"):
        return data

    patterns = policy.get("patterns") or []

    def sensitive(field):
        return matches(field, patterns, report_name=report_name, ref_doctype=ref_doctype)

    source_columns = data.get("columns") or []
    columns = [query_report.get_column_as_dict(column) for column in source_columns]
    # A duplicate name/label must not reintroduce a removed column's value.
    blocked = set()
    for col in columns:
        if sensitive(col):
            blocked.update(v for v in (col.get("fieldname"), col.get("label")) if isinstance(v, str))

    kept = []
    for index, col in enumerate(columns):
        name, label = col.get("fieldname"), col.get("label")
        if not isinstance(name, str) or not name:
            continue
        if sensitive(col) or name in blocked or (isinstance(label, str) and label in blocked):
            continue
        clean = {key: _scalar(value) for key, value in col.items() if key in _COLUMN_KEYS}
        if sensitive(clean.get("options")):
            clean.pop("options", None)
        kept.append((index, clean))

    keys = {value for _, col in kept for value in (col.get("fieldname"), col.get("label"))
            if isinstance(value, str) and value not in blocked}
    rows = []
    for row in (data.get("result") or []) if kept else []:
        if isinstance(row, dict):
            cleaned = {key: _scalar(value) for key, value in row.items() if key in keys}
            # Tree indentation is structural, never a free-form value.
            if "indent" in row and isinstance(row["indent"], (int, float)) and "indent" not in blocked:
                cleaned["indent"] = row["indent"]
            rows.append(cleaned)
        elif isinstance(row, (list, tuple)):
            rows.append([_scalar(row[index]) if index < len(row) else None for index, _ in kept])
        else:
            rows.append([None] * len(kept))

    result = {
        "columns": [column for _, column in kept],
        "result": rows,
        "message": None,
        "chart": None,
        "report_summary": None,
    }
    for key in ("skip_total_row", "add_total_row", "prepared_report", "execution_time",
                "snapshot_report", "snapshot_at"):
        if key in data:
            result[key] = _scalar(data[key])
    if not kept:
        result["add_total_row"] = 0

    # The prepared document contains filters, arbitrary fields and an attachment.
    # Keep only what Desk needs for its prepared-report state, without the raw link.
    if data.get("prepared_report"):
        doc = data.get("doc")
        result["doc"] = ({"filters": "{}", "report_end_time": _scalar(doc.get("report_end_time"))}
                         if doc else None)
    return result


@frappe.whitelist()
@frappe.read_only()
def run(
    report_name: str,
    filters: str | dict | None = None,
    user: str | None = None,
    ignore_prepared_report: bool = False,
    custom_columns: str | list | None = None,
    is_tree: bool = False,
    parent_field: str | None = None,
    are_default_filters: bool = True,
    js_filters: str | list | None = None,
) -> dict:
    data = query_report.run(
        report_name, filters, user, ignore_prepared_report, custom_columns,
        is_tree, parent_field, are_default_filters, js_filters,
    )
    policy = get_policy()
    if not policy.get("restricted"):
        return data
    report = query_report.get_report_doc(report_name)
    return filter_result(data, policy, report_name, report.ref_doctype)


def _check_export(report_name):
    frappe.permissions.can_export(
        frappe.get_cached_value("Report", report_name, "ref_doctype"), raise_exception=True
    )


@frappe.whitelist()
def export_query():
    if not get_policy().get("restricted"):
        return query_report.export_query()

    from frappe.desk.utils import pop_csv_params

    form_params = frappe._dict(frappe.local.form_dict)
    csv_params = pop_csv_params(form_params)
    clean_params(form_params)
    parse_json(form_params)
    _check_export(form_params.report_name)
    if int(form_params.export_in_background or 0):
        email = frappe.get_cached_value("User", frappe.session.user, "email")
        frappe.enqueue(
            "cost_visibility.report_filter.run_export_query_job",
            form_params=form_params, csv_params=csv_params, queue="long", now=frappe.flags.in_test,
        )
        frappe.msgprint(_(
            "Your report is being generated in the background. You will receive an email on {0} with a download link once it is ready."
        ).format(email))
        return
    return _export_query(form_params, csv_params)


def run_export_query_job(form_params, csv_params):
    from frappe.desk.utils import send_report_email

    # Frappe's job runner restores the enqueuing user's session before this call.
    _check_export(form_params.report_name)
    email = frappe.get_cached_value("User", frappe.session.user, "email")
    exported = _export_query(form_params, csv_params, populate_response=False)
    if exported:
        name, extension, content = exported
        send_report_email(email, name, extension, content, attached_to_name=form_params.report_name)


def _export_query(form_params, csv_params, populate_response=True):
    policy = get_policy()
    if not policy.get("restricted"):
        return query_report._export_query(form_params, csv_params, populate_response=populate_response)

    from frappe.desk.utils import get_csv_bytes, provide_binary_file
    from frappe.utils.xlsxutils import handle_html, make_xlsx

    report_name = form_params.report_name
    file_format = form_params.file_format_type
    custom_columns = frappe.parse_json(form_params.custom_columns or "[]")
    data = query_report.run(report_name, form_params.filters, custom_columns=custom_columns,
                            are_default_filters=False)
    report = query_report.get_report_doc(report_name)
    data = frappe._dict(filter_result(data, policy, report_name, report.ref_doctype))
    data.report_name = report_name
    # These are client-controlled and must not be copied into the output.
    data.filters = {}
    data.applied_filters = {}
    if not data.columns:
        frappe.respond_as_web_page(_("No data to export"), _("You can try changing the filters of your report."))
        return

    visible_idx = form_params.visible_idx or []
    if isinstance(visible_idx, str):
        visible_idx = json.loads(visible_idx)
    has_total = cint(data.get("add_total_row"))
    if (visible_idx and not sbool(form_params.get("ignore_visible_idx"))
            and len(visible_idx) < len(data.result) - (1 if has_total else 0)):
        visible_idx = set(visible_idx)
        rows = data.result[:-1] if has_total else data.result
        data.result = [row for index, row in enumerate(rows) if index in visible_idx]
        if has_total and data.result:
            data.result = query_report.add_total_row(data.result, data.columns)

    query_report.format_fields(data, file_format)
    table, widths, styles = query_report.build_xlsx_data(
        data, include_indentation=form_params.include_indentation, include_filters=False,
        include_hidden_columns=form_params.include_hidden_columns, build_styles=file_format == "Excel",
    )
    if file_format == "CSV":
        extension = "csv"
        content = get_csv_bytes([[handle_html(v) if isinstance(v, str) else v for v in row]
                                 for row in table], csv_params)
    elif file_format == "Excel":
        extension = "xlsx"
        content = make_xlsx(table, report_name, column_widths=widths, styles=styles).getvalue()
    else:
        frappe.throw(title=_("Unsupported file format: {0}").format(file_format),
                     msg=_("Only CSV and Excel formats are supported for export"))
    if not populate_response:
        return report_name, extension, content
    return provide_binary_file(_(report_name), extension, content)
