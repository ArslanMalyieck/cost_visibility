import frappe
from frappe import _
from frappe.model.document import Document

from cost_visibility.api import DEFAULT_PATTERNS


class CostVisibilitySettings(Document):
    def validate(self):
        users = set()
        for row in self.restricted_users:
            if row.user == "Administrator":
                frappe.throw(_("Administrator cannot be restricted."))
            if row.user in users:
                frappe.throw(_("Duplicate restricted user: {0}").format(row.user))
            users.add(row.user)

        patterns = set(DEFAULT_PATTERNS)
        for row in self.extra_patterns:
            pattern = (row.pattern or "").strip().lower()
            if not pattern:
                frappe.throw(_("Additional cost phrases cannot be blank."))
            if pattern in patterns:
                frappe.throw(_("Duplicate cost phrase: {0}").format(pattern))
            patterns.add(pattern)
            row.pattern = pattern
