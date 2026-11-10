"""Integration tests; Frappe's normal runner may alter scheduler metadata on a live site."""

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests.classes.integration_test_case import IntegrationTestCase

from cost_visibility.api import DEFAULT_PATTERNS, get_policy


class TestCostVisibilityIntegration(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.original_settings = frappe.get_doc("Cost Visibility Settings").as_dict()
        self.users = []
        commit_guard = patch.object(frappe.db, "commit", side_effect=AssertionError("Unexpected commit"))
        commit_guard.start()
        self.addCleanup(commit_guard.stop)
        self.addCleanup(self.rollback_test_data)

        suffix = uuid4().hex
        for label, roles in (("restricted", []), ("other", []), ("manager", ["System Manager"])):
            email = f"cv-{label}-{suffix}@example.invalid"
            self.users.append(email)
            frappe.get_doc({
                "doctype": "User",
                "email": email,
                "first_name": "Cost Visibility Test",
                "send_welcome_email": 0,
                "roles": [{"role": role} for role in roles],
            }).insert(ignore_permissions=True)
        self.restricted, self.other, self.manager = self.users

    def rollback_test_data(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.db.value_cache.clear()
        for user in self.users:
            frappe.clear_cache(user=user)
        frappe.clear_document_cache("Cost Visibility Settings")
        self.assertEqual(frappe.get_doc("Cost Visibility Settings").as_dict(), self.original_settings)
        for user in self.users:
            self.assertFalse(frappe.db.exists("User", user))

    def configure(self, enabled=1):
        settings = frappe.get_doc("Cost Visibility Settings")
        settings.enabled = enabled
        settings.set("restricted_users", [{"user": self.restricted}])
        settings.set("extra_patterns", [{"pattern": "  Supplier Cost  "}])
        settings.save()
        return settings

    def test_single_child_tables_and_policy(self):
        self.assertTrue(frappe.get_meta("Cost Visibility Settings").issingle)
        self.configure()
        settings = frappe.get_doc("Cost Visibility Settings")
        self.assertEqual([row.user for row in settings.restricted_users], [self.restricted])
        self.assertEqual([row.pattern for row in settings.extra_patterns], ["supplier cost"])
        for field, doctype in (("restricted_users", "Restricted Cost Visibility User"),
                               ("extra_patterns", "Cost Visibility Pattern")):
            row = settings.get(field)[0]
            self.assertTrue(frappe.get_meta(doctype).istable)
            self.assertEqual((row.parent, row.parenttype, row.parentfield),
                             (settings.name, settings.doctype, field))
            self.assertTrue(frappe.db.exists(doctype, row.name))

        for user, expected in (
            (self.restricted, {"restricted": True, "patterns": [*DEFAULT_PATTERNS, "supplier cost"]}),
            (self.other, {"restricted": False, "patterns": []}),
            ("Administrator", {"restricted": False, "patterns": []}),
            ("Guest", {"restricted": False, "patterns": []}),
        ):
            with self.subTest(user=user):
                frappe.set_user(user)
                self.assertEqual(get_policy(), expected)

        frappe.set_user("Administrator")
        self.configure(enabled=0)
        frappe.set_user(self.restricted)
        self.assertEqual(get_policy(), {"restricted": False, "patterns": []})

    def test_settings_permissions(self):
        settings = self.configure()
        for user in (self.restricted, self.other):
            frappe.set_user(user)
            with self.subTest(user=user):
                self.assertFalse(frappe.has_permission(settings.doctype, "read", doc=settings))
                self.assertFalse(frappe.has_permission(settings.doctype, "write", doc=settings))
                with self.assertRaises(frappe.PermissionError):
                    settings.check_permission("read")
                with self.assertRaises(frappe.PermissionError):
                    settings.save()
        frappe.set_user(self.manager)
        self.assertTrue(frappe.has_permission(settings.doctype, "read", doc=settings))
        self.assertTrue(frappe.has_permission(settings.doctype, "write", doc=settings))
        settings.check_permission("read")
        settings.save()

    def test_settings_validation(self):
        for users, patterns in (
            (["Administrator"], []),
            ([self.restricted, self.restricted], []),
            ([], [" "]),
            ([], ["Supplier Cost", " supplier cost "]),
            ([], [" VALUATION RATE "]),
        ):
            with self.subTest(users=users, patterns=patterns):
                settings = frappe.get_doc("Cost Visibility Settings")
                settings.set("restricted_users", [{"user": user} for user in users])
                settings.set("extra_patterns", [{"pattern": pattern} for pattern in patterns])
                with self.assertRaises(frappe.ValidationError):
                    settings.save()

    def test_registered_boot_hook_uses_current_session(self):
        self.configure()
        hook = "cost_visibility.api.extend_bootinfo"
        self.assertIn(hook, frappe.get_hooks("extend_bootinfo"))
        for user in (self.restricted, self.other):
            with self.subTest(user=user):
                frappe.set_user(user)
                bootinfo = frappe._dict()
                frappe.get_attr(hook)(bootinfo=bootinfo)
                self.assertEqual(bootinfo.cost_visibility, get_policy())
                self.assertEqual(bootinfo.cost_visibility["restricted"], user == self.restricted)
