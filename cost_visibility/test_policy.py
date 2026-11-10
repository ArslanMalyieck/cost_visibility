import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cost_visibility import api, hooks
from cost_visibility.cost_visibility.doctype.cost_visibility_settings import cost_visibility_settings


def settings(enabled=1, users=(), patterns=()):
    return SimpleNamespace(
        enabled=enabled,
        restricted_users=[SimpleNamespace(user=user) for user in users],
        extra_patterns=[SimpleNamespace(pattern=pattern) for pattern in patterns],
    )


class TestPolicy(unittest.TestCase):
    def policy(self, user, config):
        with patch.object(api.frappe, "session", SimpleNamespace(user=user)):
            with patch.object(api.frappe, "get_doc", return_value=config):
                return api.get_policy()

    def test_no_users_or_disabled_has_no_effect(self):
        self.assertEqual(self.policy("user@example.com", settings()), {"restricted": False, "patterns": []})
        self.assertEqual(
            self.policy("user@example.com", settings(enabled=0, users=["user@example.com"])),
            {"restricted": False, "patterns": []},
        )
        self.assertEqual(
            self.policy("other@example.com", settings(users=["user@example.com"])),
            {"restricted": False, "patterns": []},
        )

    def test_selected_user_gets_lowercase_literal_patterns(self):
        result = self.policy(
            "user@example.com", settings(users=["user@example.com"], patterns=["  Supplier Cost  "])
        )
        self.assertEqual(result, {"restricted": True, "patterns": [*api.DEFAULT_PATTERNS, "supplier cost"]})
        self.assertNotIn("rate", result["patterns"])
        self.assertNotIn("price", result["patterns"])

    def test_administrator_and_guest_are_never_restricted(self):
        for user in ("Administrator", "Guest"):
            with self.subTest(user=user):
                self.assertEqual(
                    self.policy(user, settings(users=[user])), {"restricted": False, "patterns": []}
                )

    def test_bootinfo_uses_current_session_policy(self):
        self.assertEqual(hooks.extend_bootinfo, "cost_visibility.api.extend_bootinfo")
        config = settings(users=["user@example.com"], patterns=[" Supplier Cost "])
        for user in ("user@example.com", "other@example.com"):
            with self.subTest(user=user):
                bootinfo = SimpleNamespace()
                with patch.object(api.frappe, "session", SimpleNamespace(user=user)):
                    with patch.object(api.frappe, "get_doc", return_value=config):
                        api.extend_bootinfo(bootinfo)
                        self.assertEqual(bootinfo.cost_visibility, api.get_policy())


class TestSettingsValidation(unittest.TestCase):
    def validate(self, config):
        with patch.object(cost_visibility_settings, "_", side_effect=lambda text: text):
            with patch.object(cost_visibility_settings.frappe, "throw", side_effect=ValueError):
                cost_visibility_settings.CostVisibilitySettings.validate(config)

    def test_invalid_users(self):
        for users in (["Administrator"], ["user@example.com", "user@example.com"]):
            with self.subTest(users=users), self.assertRaises(ValueError):
                self.validate(settings(users=users))

    def test_patterns_are_normalized_and_duplicates_rejected(self):
        config = settings(patterns=["  Supplier Cost  "])
        self.validate(config)
        self.assertEqual(config.extra_patterns[0].pattern, "supplier cost")
        for patterns in ([" "], ["Supplier Cost", " supplier cost "], ["VALUATION RATE"]):
            with self.subTest(patterns=patterns), self.assertRaises(ValueError):
                self.validate(settings(patterns=patterns))


if __name__ == "__main__":
    unittest.main()
