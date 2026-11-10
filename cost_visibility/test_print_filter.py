"""Site-free tests against Frappe documents and its standard print template."""

import logging
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.www.printview import get_visible_columns
from jinja2 import Environment, FileSystemLoader

with patch.object(frappe, "logger", return_value=logging.getLogger("cost_visibility_test")):
    from cost_visibility import print_filter
from cost_visibility.api import DEFAULT_PATTERNS


def field(name, fieldtype="Data", label=None, **kwargs):
    return frappe._dict(fieldname=name, fieldtype=fieldtype, label=label or name, **kwargs)


class Meta:
    def __init__(self, fields, tables=None, istable=False):
        self.fields = fields
        self._fields = {df.fieldname: df for df in fields}
        self._table_doctypes = tables or {}
        self._non_computed_table_doctypes = self._table_doctypes
        self.istable = istable
        self.is_submittable = False

    def get_field(self, name):
        return self._fields.get(name)

    def get_valid_fields(self):
        return list(self._fields)


class TestDocument(Document):
    def refetch(self):
        return "SECRET_FROM_DATABASE"


class LazyTestDocument(TestDocument):
    def get(self, key, *args, **kwargs):
        if key == "items" and "items" not in self.__dict__:
            self.__dict__["items"] = self.__dict__["_lazy_rows"]
        return super().get(key, *args, **kwargs)


def document(doctype, **values):
    doc = TestDocument.__new__(TestDocument)
    doc.__dict__.update(doctype=doctype, flags=frappe._dict(), **values)
    return doc


class TestPrintFilter(unittest.TestCase):
    def setUp(self):
        self.parent_fields = [
            field("name"), field("supplier"), field("grand_total", "Currency"),
            field("special", "Data", "Supplier Cost"),
            field("items", "Table", options="Purchase Order Item", start=0, end=None),
        ]
        self.child_fields = [field("name"), field("idx", "Int"), field("item_code"),
                             field("rate", "Currency"), field("amount", "Currency")]
        self.meta = {
            "Purchase Order": Meta(self.parent_fields, {"items": "Purchase Order Item"}),
            "Purchase Order Item": Meta(self.child_fields, istable=True),
        }
        self.row = document("Purchase Order Item", name="ROW-1", idx=1, item_code="ITEM-1",
                            rate=123.45, amount=246.90)
        self.doc = document("Purchase Order", name="PO-1", supplier="Vendor", grand_total=246.90,
                            special="SECRET_LABEL", items=[self.row])
        self.layout = [[{"columns": [{"fields": self.parent_fields[1:]}], "has_data": True}]]
        self.args = {"doc": self.doc, "meta": self.meta["Purchase Order"], "layout": self.layout}
        self.meta_patch = patch.object(frappe, "get_meta", side_effect=lambda dt: self.meta[dt])
        self.meta_patch.start()
        self.addCleanup(self.meta_patch.stop)

    def render(self, template, restricted=True, **kwargs):
        policy = {"restricted": restricted, "patterns": [*DEFAULT_PATTERNS, "supplier cost"]}
        with patch.object(print_filter, "get_policy", return_value=policy):
            return print_filter.pdf_body_html(template, self.args, jenv="jenv", print_format=None, **kwargs)

    def test_unrestricted_delegates_original_arguments(self):
        template = object()
        with patch.object(print_filter, "get_policy", return_value={"restricted": False}):
            with patch.object(print_filter, "frappe_pdf_body_html", return_value="unchanged") as renderer:
                self.assertEqual(print_filter.pdf_body_html(template, self.args, jenv=1, print_format=2),
                                 "unchanged")
        renderer.assert_called_once_with(template, self.args, jenv=1, print_format=2)
        self.assertIs(renderer.call_args.args[1], self.args)

    def test_jinja_views_and_original_documents_are_unchanged(self):
        source = ("{{ doc.supplier }}|{{ doc.grand_total }}|{{ doc['special'] }}|"
                  "{{ doc.get_formatted('grand_total') }}|{{ doc.as_dict().grand_total }}|"
                  "{% for row in doc.items %}{{ row.item_code }}:{{ row.rate }}:"
                  "{{ row.get_formatted('rate') }}:{{ row.as_dict().amount }}:"
                  "{{ row.parent_doc.grand_total }}{% endfor %}")
        template = Environment().from_string(source)
        before = deepcopy(self.doc.as_dict())
        before_layout = deepcopy(self.layout)
        output = self.render(template)
        self.assertIn("Vendor", output)
        self.assertIn("ITEM-1", output)
        self.assertNotIn("123.45", output)
        self.assertNotIn("246.9", output)
        self.assertNotIn("SECRET_LABEL", output)
        self.assertEqual(self.doc.as_dict(), before)
        self.assertEqual(self.layout, before_layout)
        self.assertEqual(self.row.rate, 123.45)
        self.assertIsNone(self.parent_fields[-1].get("hide_in_print_layout"))

    def test_standard_template_hides_numeric_columns_and_keeps_other_fields(self):
        env = Environment(loader=FileSystemLoader(str(Path(frappe.__file__).parent)))
        env.filters["len"] = len
        env.globals.update(
            _=lambda value, **kwargs: value or "", get_visible_columns=get_visible_columns,
            frappe=frappe._dict(get_meta=frappe.get_meta, utils=frappe.utils),
            resolve_class=lambda choices: " ".join(k for k, v in choices.items() if v),
        )
        self.args.update(no_letterhead=True, letter_head="", footer="", print_settings=frappe._dict(),
                         print_heading_template=None)
        output = self.render(env.get_template("templates/print_formats/standard.html"))
        self.assertIn("Vendor", output)
        self.assertIn("ITEM-1", output)
        self.assertNotIn("grand_total", output)
        self.assertNotIn("SECRET_LABEL", output)
        self.assertNotIn(">rate<", output)
        self.assertNotIn(">amount<", output)
        self.assertNotIn("123.45", output)

    def test_controller_refetch_is_not_available(self):
        template = Environment().from_string("{{ doc.refetch() }}")
        with patch.object(print_filter, "frappe_pdf_body_html", side_effect=lambda template, args, **kw: template.render(args)):
            with self.assertRaises(TypeError):
                self.render(template)

    def test_child_filtering_and_formatted_parent_context(self):
        with patch.object(print_filter, "get_policy", return_value={"restricted": True, "patterns": []}):
            with patch.object(print_filter, "frappe_pdf_body_html", side_effect=lambda template, args, **kw: args):
                args = print_filter.pdf_body_html(None, self.args)
        view = args["doc"]
        row = view.items[0]
        self.assertIs(row.parent_doc, view)
        self.assertIs(row._doc.parent_doc, view._doc)
        self.assertEqual(row.item_code, "ITEM-1")
        self.assertIsNone(row.rate)
        self.assertEqual(row.get_formatted("rate", view), "")
        self.assertIsNone(row.as_dict()["rate"])
        self.assertEqual(view.get("items", {"item_code": "ITEM-1"})[0].item_code, "ITEM-1")
        table = args["layout"][0][0]["columns"][0]["fields"][-1]
        self.assertIn("rate", table.hide_in_print_layout)
        self.assertIn("amount", table.hide_in_print_layout)

    def test_lazy_children_are_loaded_only_on_the_copy(self):
        doc = LazyTestDocument.__new__(LazyTestDocument)
        doc.__dict__.update(doctype="Purchase Order", flags=frappe._dict(), name="PO-1",
                            supplier="Vendor", grand_total=246.90, special="SECRET_LABEL",
                            _lazy_rows=[self.row])
        self.args["doc"] = doc
        template = Environment().from_string("{{ doc.items[0].item_code }}|{{ doc.items[0].rate }}")
        self.assertEqual(self.render(template), "ITEM-1|None")
        self.assertNotIn("items", doc.__dict__)
        self.assertEqual(self.row.rate, 123.45)

    def test_layout_docfield_objects_are_copied(self):
        df = document("DocField", fieldname="items", fieldtype="Table", options="Purchase Order Item",
                      start=0, end=None)
        self.layout[0][0]["columns"][0]["fields"] = [df]
        with patch.object(print_filter, "get_policy", return_value={"restricted": True, "patterns": []}):
            with patch.object(print_filter, "frappe_pdf_body_html", side_effect=lambda template, args, **kw: args):
                args = print_filter.pdf_body_html(None, self.args)
        result = args["layout"][0][0]["columns"][0]["fields"][0]
        self.assertIsNot(result, df)
        self.assertIn("rate", result.hide_in_print_layout)
        self.assertIsNone(df.get("hide_in_print_layout"))

    def test_field_metadata_selling_label_takes_precedence(self):
        self.meta["Purchase Order"].fields.append(field("rate", "Currency", "Selling Rate"))
        self.doc.rate = 321.0
        view = print_filter._PrintDocument(deepcopy(self.doc), [])
        self.assertEqual(view.rate, 321.0)
        self.assertEqual(view.get("rate"), 321.0)

    def test_item_price_buying_context_and_selling_precedence(self):
        self.meta["Item Price"] = Meta([field("buying", "Check"), field("price_list_rate", "Currency")])
        for buying, expected in ((1, None), (0, 87.5)):
            with self.subTest(buying=buying):
                doc = document("Item Price", buying=buying, price_list_rate=87.5)
                view = print_filter._PrintDocument(deepcopy(doc), [])
                self.assertEqual(view.price_list_rate, expected)
                self.assertEqual(doc.price_list_rate, 87.5)
        self.meta["Sales Order Item"] = Meta([field("rate", "Currency")], istable=True)
        row = document("Sales Order Item", rate=99.0)
        self.assertEqual(print_filter._PrintDocument(deepcopy(row), []).rate, 99.0)


if __name__ == "__main__":
    unittest.main()
