import subprocess
import sys
import unittest

from calendar_digest.contact_identity import canonical_contact_book_id as identity


class ContactIdentityTests(unittest.TestCase):
    def test_host_port_trailing_slash_and_equivalent_encoding_do_not_change_book(self):
        aliases = (
            "https://contacts.icloud.com/123/carddavhome/book/",
            "https://p34-contacts.icloud.com/123/carddavhome/book/",
            "https://p99-contacts.icloud.com:443/123/carddavhome/book",
            "https://p34-contacts.icloud.com/123/carddavhome/%62ook/",
        )
        self.assertEqual(len({identity(value) for value in aliases}), 1)

    def test_account_and_collection_scope_are_preserved(self):
        books = (
            "https://contacts.icloud.com/123/carddavhome/book/",
            "https://contacts.icloud.com/456/carddavhome/book/",
            "https://contacts.icloud.com/123/carddavhome/other/",
            "https://contacts.icloud.com/123/carddavhome/Book/",
        )
        self.assertEqual(len({identity(value) for value in books}), len(books))

    def test_already_normalized_and_opaque_ids_are_idempotent(self):
        canonical = identity("https://contacts.icloud.com/123/book/")
        self.assertEqual(identity(canonical), canonical)
        self.assertEqual(identity("synthetic-book"), "synthetic-book")
        self.assertNotIn("/123/", canonical)

    def test_invalid_or_untrusted_url_is_rejected_without_echoing_it(self):
        values = (
            "https://private.example/123/book/", "http://contacts.icloud.com/123/book/",
            "https://user:private@contacts.icloud.com/123/book/",
            "https://contacts.icloud.com:444/123/book/",
            "https://contacts.icloud.com/123/book/?private=1",
            "https://contacts.icloud.com/123/book/#private", "https://contacts.icloud.com/",
            "https://contacts.icloud.com/123/../book/",
            "https://contacts.icloud.com/123/%252e%252e/book/",
            "https://contacts.icloud.com/123/book%2fother/",
            "https://contacts.icloud.com/123/book%00/",
            "https://contacts.icloud.com/123/book%ZZ/",
            "icloud-carddav-book-v1:private", "", None,
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError) as error:
                identity(value)
            self.assertNotIn("private", str(error.exception))

    def test_import_does_not_load_calendar_or_network_clients(self):
        result = subprocess.run([sys.executable, "-c",
            "import sys; import calendar_digest.contact_identity; "
            "assert not {'caldav','icalendar','niquests'} & sys.modules.keys()"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
