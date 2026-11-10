"""Optional Frappe v16 ``pdf_body_html`` renderer for document print bodies.

Register ``pdf_body_html = "cost_visibility.print_filter.pdf_body_html"`` in
the app's hooks, with this app last in hook resolution. Frappe selects only
the last renderer; this replaces, rather than chains, any other renderer.

This is presentation filtering, not an authorization boundary. Letterheads and
footers are rendered before this hook; custom Jinja can independently query the
database or expose values through globals, metadata, computed fields, other
objects, or controller helpers. The WeasyPrint beta path bypasses this hook.
Raw printing and other print/PDF paths must be evaluated separately; some raw
formats do call this renderer, but are not guaranteed covered. Restrict those
paths and sensitive templates separately if confidentiality is required.

Restricted templates receive a document view, not the original controller.
It supports normal field access, tables, formatting and serialization, but
arbitrary controller methods (including refetch/reload) are unavailable. A
custom template that relies on them may fail and must be reviewed. Values
already copied into unrelated fields or pre-rendered HTML are not discoverable
by field matching. Controller overrides and properties can fetch independently;
documents containing objects that cannot be deep-copied can also fail to print.
Unrestricted rendering delegates unchanged to Frappe.
"""

from collections.abc import Mapping
from copy import copy, deepcopy

import frappe
from frappe.utils.pdf import pdf_body_html as frappe_pdf_body_html

from cost_visibility.api import get_policy
from cost_visibility.matching import matches


class _PrintDocument:
    """Template-facing view over an isolated, redacted document."""

    def __init__(self, doc, patterns, parent=None):
        self._doc = doc
        self._patterns = patterns
        self._parent = parent
        self._children = {}
        self._hidden = set()
        self._fields = set()
        for df in doc.meta.fields:
            name = df.get("fieldname")
            if name:
                self._fields.add(name)
                if self._matches(df):
                    self._hidden.add(name)

        # Clear actual values as well as filtering the view, so ordinary Frappe
        # formatting and serialization cannot recover the original values.
        for name in self._hidden:
            if name in doc.__dict__:
                doc.__dict__[name] = None

        for name in doc._table_fieldnames:
            rows = doc.get(name) or []  # Load lazy child tables on the copy.
            for row in rows:
                row.parent_doc = doc
            self._children[name] = [_PrintDocument(row, patterns, self) for row in rows]

    def _matches(self, field):
        return matches(
            field, self._patterns, doctype=self._doc.doctype,
            ref_doctype=self._parent.doctype if self._parent else None,
            document=self._doc,
        )

    def _hidden_field(self, name):
        if name in self._fields:
            return name in self._hidden
        return isinstance(name, str) and self._matches(name)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name == "parent_doc":
            return self._parent
        if self._hidden_field(name):
            return None
        if isinstance(name, str) and name in self._children:
            return self._children[name]
        value = getattr(self._doc, name)
        # Do not expose arbitrary controller methods: they can reload or fetch
        # an unfiltered document, even when invoked on the isolated copy.
        if callable(value):
            raise AttributeError(f"{name} is unavailable in a filtered print")
        return value

    def __getitem__(self, name):
        return self.get(name)

    def get(self, name, *args, **kwargs):
        if isinstance(name, str) and self._hidden_field(name):
            return None
        if isinstance(name, dict) or (isinstance(name, str) and name in self._children):
            rows = self._doc.get(name, *args, **kwargs) or []
            views = {id(row._doc): row for table in self._children.values() for row in table}
            return [views[id(row)] for row in rows]
        return self._doc.get(name, *args, **kwargs)

    def get_formatted(self, fieldname, *args, **kwargs):
        if self._hidden_field(fieldname):
            return ""
        return self._doc.get_formatted(fieldname, *args, **kwargs)

    def is_print_hide(self, fieldname, df=None, *args, **kwargs):
        return self._hidden_field(fieldname) or self._doc.is_print_hide(fieldname, df, *args, **kwargs)

    def has_permlevel_access_to(self, *args, **kwargs):
        return self._doc.has_permlevel_access_to(*args, **kwargs)

    def in_format_data(self, fieldname):
        return self._doc.in_format_data(fieldname)

    def as_dict(self, *args, **kwargs):
        result = self._doc.as_dict(*args, **kwargs)
        for name in list(result):
            if self._hidden_field(name):
                result[name] = None
            elif name in self._children:
                result[name] = [row.as_dict(*args, **kwargs) for row in self._children[name]]
        return result


def _filtered_layout(layout, doc, patterns):
    """Copy layout containers and table fields; never edit cached DocFields."""
    result = []
    for page in layout:
        new_page = []
        for section in page:
            new_section = dict(section)
            new_section["columns"] = []
            for column in section["columns"]:
                new_column = dict(column)
                new_column["fields"] = []
                for df in column["fields"]:
                    if doc._matches(df):
                        continue
                    if df.get("fieldtype") == "Table" and df.get("options"):
                        df = copy(df)
                        rows = doc.get(df.get("fieldname")) or []
                        child_meta = frappe.get_meta(df.get("options"))
                        hidden = list(df.get("hide_in_print_layout") or [])
                        for child_df in child_meta.fields:
                            name = child_df.get("fieldname")
                            if name and (any(row._matches(child_df) for row in rows) or matches(
                                child_df, patterns, doctype=df.get("options"), ref_doctype=doc.doctype
                            )) and name not in hidden:
                                hidden.append(name)
                        if isinstance(df, Mapping):
                            df["hide_in_print_layout"] = hidden
                        else:
                            df.hide_in_print_layout = hidden
                    new_column["fields"].append(df)
                new_section["columns"].append(new_column)
            new_section["has_data"] = any(col["fields"] for col in new_section["columns"])
            new_page.append(new_section)
        result.append(new_page)
    return result


def pdf_body_html(template, args, **kwargs):
    """Render the print body using Frappe's ``template, args, **kwargs`` hook."""
    policy = get_policy()
    if not policy.get("restricted"):
        return frappe_pdf_body_html(template, args, **kwargs)

    patterns = policy.get("patterns") or ()
    doc = _PrintDocument(deepcopy(args["doc"]), patterns)
    filtered_args = dict(args)
    filtered_args["doc"] = doc
    if "layout" in args:
        filtered_args["layout"] = _filtered_layout(args["layout"], doc, patterns)
    return frappe_pdf_body_html(template, filtered_args, **kwargs)
