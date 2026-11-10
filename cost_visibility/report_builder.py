"""Optional Report Builder view and export overrides; not an authorization boundary.

Install OVERRIDE_WHITELISTED_METHODS in hooks to activate. Direct calls to
Frappe's exporter and other data access paths are not intercepted. This mirrors
the installed Frappe reportview exporter and must be reviewed on Frappe upgrades.
"""

import json
import re
from copy import deepcopy

import frappe
import frappe.permissions
from frappe import _
from frappe.desk import reportview

from cost_visibility.api import get_policy
from cost_visibility.matching import matches


OVERRIDE_WHITELISTED_METHODS = {
    "frappe.desk.reportview.export_query": "cost_visibility.report_builder.export_query",
    "frappe.desk.reportview.get": "cost_visibility.report_builder.get",
}


_FIELD_REFERENCE = re.compile(r"^(?:(?P<table>`[^`]+`|\w+)\.)?(?P<name>`\w+`|\w+)$")
_ALIAS = re.compile(r"^[\w :\-]+$")


def _reference_sensitive(reference, doctype, report_name, patterns, alias=None):
    """Classify a simple SQL reference, or return None if it cannot be resolved."""
    from frappe.model import std_fields
    from frappe.model.meta import get_default_df

    match = _FIELD_REFERENCE.fullmatch(reference.strip())
    if not match:
        return None
    table = (match.group("table") or "").strip("`")
    name = match.group("name").strip("`")
    owner = doctype
    try:
        if table.startswith("tab"):
            owner = table[3:]
        elif table:
            link = frappe.get_meta(doctype).get_field(table)
            if not link or link.fieldtype not in ("Link", "Table", "Table MultiSelect") or not link.options:
                return None
            owner = link.options
        df = frappe.get_meta(owner).get_field(name) or get_default_df(name)
    except frappe.DoesNotExistError:
        return None
    standard_df = next((item for item in std_fields if item["fieldname"] == name), {})
    if not df and name in reportview.optional_fields:
        df = frappe._dict(fieldname=name, label=standard_df.get("label"))
    if not df:
        return None
    field = {"fieldname": name, "doctype": owner, "df": df,
             "label": _(df.label or standard_df.get("label") or "")}
    if matches(field, patterns, doctype=owner, report_name=report_name, ref_doctype=doctype):
        return True
    return bool(alias and matches({"fieldname": alias, "doctype": owner}, patterns, doctype=owner,
                                  report_name=report_name, ref_doctype=doctype))


def _expression_sensitive(value, doctype, report_name, patterns):
    """Classify Frappe query-builder expressions recursively; None means unknown."""
    from frappe.database.query import FUNCTION_MAPPING, OPERATOR_MAPPING

    if isinstance(value, str):
        value = value.strip()
        if value == "*" or (len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]):
            return False
        return _reference_sensitive(value, doctype, report_name, patterns)
    if value is None or isinstance(value, (int, float)):
        return False
    if isinstance(value, (list, tuple)):
        parts = [_expression_sensitive(v, doctype, report_name, patterns) for v in value]
        return True if True in parts else None if None in parts else False
    if isinstance(value, dict):
        operands = [(k, v) for k, v in value.items() if k.lower() != "as"]
        if len(operands) != 1 or operands[0][0] not in FUNCTION_MAPPING | OPERATOR_MAPPING:
            return None
        return _expression_sensitive(operands[0][1], doctype, report_name, patterns)
    return None


def _filter_compressed(data, args, fields, policy):
    if not data:
        return data

    doctype = args.doctype
    report_name = args.get("report_name")
    patterns = policy.get("patterns") or []
    candidates = {}
    unresolved = False
    for field in fields or ["name"]:
        key = None
        sensitive = None
        if isinstance(field, str):
            parts = re.split(r"\s+as\s+", field.strip(), flags=re.IGNORECASE)
            if len(parts) <= 2:
                reference = _FIELD_REFERENCE.fullmatch(parts[0].strip())
                if len(parts) == 2:
                    key = parts[1].strip(" `\"'")
                    if not _ALIAS.fullmatch(key):
                        key = None
                elif reference:
                    key = reference.group("name").strip("`")
                sensitive = _reference_sensitive(parts[0], doctype, report_name, patterns,
                                                   alias=key if len(parts) == 2 else None)
        elif isinstance(field, dict):
            aliases = [v for k, v in field.items() if k.lower() == "as"]
            if len(aliases) == 1 and isinstance(aliases[0], str) and _ALIAS.fullmatch(aliases[0]):
                key = aliases[0]
                sensitive = _expression_sensitive(field, doctype, report_name, patterns)
                if matches(key, patterns, doctype=doctype, report_name=report_name, ref_doctype=doctype):
                    sensitive = True
        if key is None:
            unresolved = True
        else:
            candidates.setdefault(key, []).append(sensitive)

    standard = set(reportview.default_fields + reportview.optional_fields + reportview.child_table_fields)
    standard.add("_comment_count")
    blocked = set()
    for index, key in enumerate(data["keys"]):
        choices = candidates.get(key, [])
        if len(choices) == 1 and choices[0] is False:
            continue
        if not choices and not unresolved and key in standard:
            if key == "_comment_count":
                if args.get("with_comment_count") and not matches(
                    key, patterns, doctype=doctype, report_name=report_name, ref_doctype=doctype
                ):
                    continue
            elif _reference_sensitive(key, doctype, report_name, patterns) is False:
                continue
        blocked.add(index)

    data["values"] = [[None if i in blocked or i >= len(data["keys"]) else value
                       for i, value in enumerate(row)] for row in data["values"]]
    return data


@frappe.whitelist()
@frappe.read_only()
def get():
    policy = get_policy()
    if not policy.get("restricted"):
        return reportview.get()

    args = reportview.get_form_params()
    fields = deepcopy(args.fields)
    if reportview.is_virtual_doctype(args.doctype):
        controller = reportview.get_controller(args.doctype)
        data = reportview.compress(frappe.call(controller.get_list, args=args, **args))
    else:
        data = reportview.compress(reportview.execute(**args), args=args)
    return _filter_compressed(data, args, fields, policy)


@frappe.whitelist()
@frappe.read_only()
def export_query():
    if not get_policy().get("restricted"):
        return reportview.export_query()

    from frappe.desk.utils import pop_csv_params

    form_params = reportview.get_form_params()
    form_params["limit_page_length"] = None
    form_params["as_list"] = True
    csv_params = pop_csv_params(form_params)
    export_in_background = int(form_params.pop("export_in_background", 0))

    if export_in_background:
        user_email = frappe.get_cached_value("User", frappe.session.user, "email")
        frappe.enqueue(
            "cost_visibility.report_builder.run_report_view_export_job",
            user_email=user_email, form_params=form_params, csv_params=csv_params,
            queue="long", now=frappe.flags.in_test,
        )
        frappe.msgprint(_(
            "Your report is being generated in the background. You will receive an email on {0} with a download link once it is ready."
        ).format(user_email))
        return
    return _export_query(form_params, csv_params)


def run_report_view_export_job(user_email, form_params, csv_params):
    from frappe.desk.utils import send_report_email

    title, extension, content = _export_query(form_params, csv_params, populate_response=False)
    send_report_email(user_email, title, extension, content, attached_to_name=title)


def _sensitive(field, info, doctype, report_name, patterns):
    def check(value, owner=doctype):
        return matches(value, patterns, doctype=owner, report_name=report_name, ref_doctype=doctype)

    # The displayed alias can differ from both the SQL operand and its DocField.
    expressions = []
    info_owner = doctype
    if isinstance(field, dict):
        if check(field.get("as")):
            return True
        expressions.extend(v for k, v in field.items() if k.lower() != "as" and isinstance(v, str))
    elif isinstance(field, str):
        parts = re.split(r"\s+as\s+", field, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2 and check(parts[1].strip(" `\"'")):
            return True
        expressions.append(parts[0])

    for expression in expressions:
        for reference in reportview.extract_fieldnames(expression):
            if reference == "*":
                continue
            parts = reference.split(".", 1)
            owner = doctype
            if len(parts) == 2:
                table, name = (part.strip("`\" ") for part in parts)
                if table.startswith("tab"):
                    owner = table[3:]
                else:
                    table_field = frappe.get_meta(doctype).get_field(table)
                    owner = table_field.options if table_field and table_field.options else doctype
            else:
                name = parts[0].strip("`\" ")
            info_owner = owner
            df = frappe.get_meta(owner).get_field(name)
            if check({"fieldname": name, "df": df, "doctype": owner}, owner):
                return True
    return check({**info, "doctype": info_owner}, info_owner)


def _export_query(form_params, csv_params, populate_response=True):
    policy = get_policy()
    if not policy.get("restricted"):
        return reportview._export_query(form_params, csv_params, populate_response=populate_response)

    from frappe.core.doctype.access_log.access_log import make_access_log
    from frappe.desk.utils import get_csv_bytes, provide_binary_file
    from frappe.utils.xlsxutils import get_default_xlsx_styles, handle_html, make_xlsx

    # Keep this in sync with reportview._export_query through the permission and
    # formatting steps; the query and user-settings inputs are never filtered.
    doctype = form_params.pop("doctype")
    owner_field = f"`tab{doctype}`.`owner`"
    if isinstance(form_params["fields"], list):
        form_params["fields"].append(owner_field)
    elif isinstance(form_params["fields"], tuple):
        form_params["fields"] = form_params["fields"] + (owner_field,)
    file_format_type = form_params.pop("file_format_type")
    title = form_params.pop("title", doctype)
    add_totals_row = 1 if form_params.pop("add_totals_row", None) == "1" else None
    translate_values = 1 if form_params.pop("translate_values", None) == "1" else None
    if selection := form_params.pop("selected_items", None):
        form_params["filters"] = {"name": ("in", json.loads(selection))}

    make_access_log(doctype=doctype, file_type=file_format_type,
                    report_name=form_params.report_name, filters=form_params.filters)
    db_query = reportview.DatabaseQuery(doctype)
    ret = db_query.execute(**form_params)
    if not frappe.permissions.can_export(doctype):
        if frappe.permissions.can_export(doctype, is_owner=True):
            for row in ret:
                if row[-1] != frappe.session.user:
                    raise frappe.PermissionError(_("You are not allowed to export {} doctype").format(doctype))
        else:
            raise frappe.PermissionError(_("You are not allowed to export {} doctype").format(doctype))

    if add_totals_row:
        ret = reportview.append_totals_row(ret)
    fields_info = reportview.get_field_info(db_query.fields, doctype)
    # A positional mismatch cannot safely be filtered by column index.
    if len(fields_info) != len(db_query.fields) or any(len(row) != len(fields_info) for row in ret):
        frappe.throw(_("Unable to safely align Report Builder export columns"))

    sr_label = _("Sr")
    data = [[sr_label, *(info["label"] for info in fields_info)]]
    if frappe.local.lang == "en" or not translate_values:
        data.extend([i + 1, *list(row)] for i, row in enumerate(ret))
    else:
        translatable_fields = [field["translatable"] for field in fields_info]
        data.extend([i + 1, *(_(value) if translatable_fields[idx] else value
                              for idx, value in enumerate(row))] for i, row in enumerate(ret))
    data = reportview.handle_duration_fieldtype_values(doctype, data, db_query.fields)

    patterns = policy.get("patterns") or []
    report_name = form_params.get("report_name") or title
    keep = [i for i, (field, info) in enumerate(zip(db_query.fields, fields_info))
            if not _sensitive(field, info, doctype, report_name, patterns)]
    fields_info = [fields_info[i] for i in keep]
    data = [[row[0], *(row[i + 1] for i in keep)] for row in data]

    if file_format_type == "CSV":
        file_extension = "csv"
        content = get_csv_bytes([[handle_html(v) if isinstance(v, str) else v for v in row]
                                 for row in data], csv_params)
    elif file_format_type == "Excel":
        file_extension = "xlsx"
        styles = get_default_xlsx_styles(
            columns=[{"fieldname": "sr", "label": sr_label, "fieldtype": "Int"}, *fields_info],
            data=data[1:], has_total_row=bool(add_totals_row),
        )
        content = make_xlsx(data, doctype, styles=styles).getvalue()

    if not populate_response:
        return title, file_extension, content
    provide_binary_file(_(title), file_extension, content)
