import unittest

from qijia_video.accounts import (
    hash_password,
    normalize_username,
    username_key,
    verify_password,
)
from qijia_video.errors import QualityGateFailed


class AccountSecurityTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        first = hash_password("a-secure-password")
        second = hash_password("a-secure-password")

        self.assertNotEqual(first, second)
        self.assertNotIn("a-secure-password", first)
        self.assertTrue(verify_password("a-secure-password", first))
        self.assertFalse(verify_password("wrong-password", first))

    def test_username_is_normalized_and_restricted(self):
        self.assertEqual(normalize_username("  小齐_01  "), "小齐_01")
        self.assertEqual(username_key("Editor"), "editor")
        with self.assertRaises(QualityGateFailed):
            normalize_username("bad account")


if __name__ == "__main__":
    unittest.main()
