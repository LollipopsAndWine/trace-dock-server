import re
import io
import os
import unittest
from unittest.mock import patch

from trace_dock_server.token_security import issue_token, recover_email
from trace_dock_server import token_email


class TokenSecurityTests(unittest.TestCase):
    def test_token_is_opaque_and_recovers_email_without_database(self):
        email = "person@example.com"
        token = issue_token(email, "p" * 48)

        self.assertRegex(token, re.compile(r"^[A-Za-z0-9_-]{16,4096}$"))
        self.assertNotIn(email, token)
        self.assertEqual(recover_email(token, "p" * 48), email)

    def test_token_has_independent_randomness_per_issue(self):
        first = issue_token("person@example.com", "p" * 48)
        second = issue_token("person@example.com", "p" * 48)

        self.assertNotEqual(first, second)
        self.assertEqual(recover_email(first, "p" * 48), "person@example.com")
        self.assertEqual(recover_email(second, "p" * 48), "person@example.com")

    def test_wrong_pepper_tampering_and_legacy_token_are_rejected(self):
        token = issue_token("person@example.com", "p" * 48)

        for value in (token[:-1] + ("A" if token[-1] != "A" else "B"), "old-random-token"):
            with self.subTest(token=value[:12]), self.assertRaises(ValueError):
                recover_email(value, "p" * 48)
        with self.assertRaisesRegex(ValueError, "TOKEN_PEPPER"):
            recover_email(token, "q" * 48)

    def test_recovery_command_uses_pepper_without_database_settings(self):
        token = issue_token("person@example.com", "p" * 48)
        output = io.StringIO()
        with patch.dict(os.environ, {"TOKEN_PEPPER": "p" * 48}, clear=True), \
                patch("sys.stdin", io.StringIO(token + "\n")), \
                patch("sys.stdout", output):
            token_email.main()
        self.assertEqual(output.getvalue(), "person@example.com\n")


if __name__ == "__main__":
    unittest.main()
