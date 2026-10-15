"""Private birthday projection through authenticated clients and fake HTTP."""

import base64
from dataclasses import fields
import logging
import os
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import niquests

from calendar_digest.contact_birthdays_source import (
    ContactBirthday, _birthday, collect_contact_birthdays,
)
from calendar_digest.contact_identity import canonical_contact_book_id
from calendar_digest.icloud import Credentials
from calendar_digest.readonly_caldav import CalendarConnectionError
from test_icloud import response_for


ROOT = "https://contacts.icloud.com/"
PRINCIPAL = "https://p42-contacts.icloud.com/123/principal/"
HOME = "https://p42-contacts.icloud.com/123/carddavhome/"
BOOK = HOME + "book/"
USER = "fictional-contacts@example.invalid"
PASSWORD = "fictional-private-contacts-password"
CARD = "{urn:ietf:params:xml:ns:carddav}"
DAV = "{DAV:}"


def record(href, properties, status="200 OK"):
    return (f"<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>"
            f"{properties}</d:prop><d:status>HTTP/1.1 {status}</d:status></d:propstat></d:response>")


def multistatus(*records):
    return ('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
            + "".join(records) + "</d:multistatus>")


def card(birthday="1990-10-18", *, uid="fictional-uid", name="Вымышленное имя", extra="", version="3.0"):
    return (f"BEGIN:VCARD\r\nVERSION:{version}\r\nUID:{uid}\r\nFN:{name}\r\n"
            f"BDAY:{birthday}\r\n{extra}END:VCARD\r\n")


class ContactBirthdaySourceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"ICLOUD_USERNAME": USER, "ICLOUD_APP_PASSWORD": PASSWORD}, clear=True))
        self.transport = self.enterContext(patch.object(niquests.Session, "send", autospec=True))
        self.transport.side_effect = self.send
        self.books = {BOOK: {"one.vcf": card()}}
        self.calls = []
        self.mutate = lambda stage, body: body
        self.fail_stage = None
        self.http_status = 207
        self.final_book_url = None
        self.expected_authorization = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

    def send(self, session, request, **kwargs):
        self.calls.append((request.method, request.url))
        self.assertTrue(kwargs["verify"])
        self.assertTrue(kwargs["stream"])
        self.assertEqual(request.headers["Authorization"], self.expected_authorization)
        self.assertEqual(session.hostname_pattern, r"(?:contacts|p[0-9]+-contacts)\.icloud\.com")
        query = ET.fromstring(request.body)
        if request.url == ROOT:
            stage = "principal"
            self.assertEqual(request.method, "PROPFIND")
            self.assertEqual(request.headers["Depth"], "0")
            self.assertIsNotNone(query.find(DAV + "prop/" + DAV + "current-user-principal"))
            body = multistatus(record(ROOT,
                f"<d:current-user-principal><d:href>{PRINCIPAL}</d:href></d:current-user-principal>"))
        elif request.url == PRINCIPAL:
            stage = "home"
            self.assertEqual(request.method, "PROPFIND")
            self.assertIsNotNone(query.find(DAV + "prop/" + CARD + "addressbook-home-set"))
            body = multistatus(record(PRINCIPAL,
                f"<c:addressbook-home-set><d:href>{HOME}</d:href></c:addressbook-home-set>"))
        elif request.url == HOME:
            stage = "books"
            self.assertEqual(request.method, "PROPFIND")
            self.assertEqual(request.headers["Depth"], "1")
            body = multistatus(record(HOME, "<d:resourcetype><d:collection/></d:resourcetype>"),
                *[record(url, "<d:resourcetype><d:collection/><c:addressbook/></d:resourcetype>")
                  for url in self.books])
        elif request.url in self.books:
            stage = "contacts"
            self.assertEqual(request.method, "REPORT")
            self.assertEqual(query.tag, CARD + "addressbook-query")
            self.assertEqual(request.headers["Depth"], "1")
            projected = query.findall(DAV + "prop/" + CARD + "address-data/" + CARD + "prop")
            self.assertEqual({element.attrib["name"] for element in projected}, {"VERSION", "UID", "FN", "BDAY"})
            filters = query.findall(CARD + "filter/" + CARD + "prop-filter")
            self.assertEqual(len(filters), 1)
            self.assertEqual(filters[0].attrib, {"name": "BDAY"})
            self.assertEqual(len(filters[0]), 0)
            final_book = self.final_book_url or request.url
            body = multistatus(*[record(final_book.rstrip("/") + "/" + filename,
                f"<c:address-data>{escape(data)}</c:address-data>")
                for filename, data in self.books[request.url].items()])
        else:
            self.fail("Unexpected HTTP destination")
        if stage == self.fail_stage:
            raise RuntimeError(f"private server response {PASSWORD} {request.url}")
        response = response_for(request, self.mutate(stage, body), status=self.http_status)
        if stage == "contacts" and self.final_book_url is not None:
            # Simulate the validated final HTTP response following a redirect;
            # all resource hrefs belong to that final collection.
            response.url = self.final_book_url
            response.history = [response_for(request, "", status=307)]
        return response

    def test_projects_only_required_fields_and_returns_no_birth_year_or_raw_card(self):
        result = collect_contact_birthdays()
        self.assertEqual(result.addressbooks, 1)
        self.assertEqual(result.contacts, [ContactBirthday(canonical_contact_book_id(BOOK),
                                                          "fictional-uid", "Вымышленное имя", 10, 18)])
        self.assertEqual({field.name for field in fields(result.contacts[0])}, {"book_id", "uid", "name", "month", "day"})
        for private in ("BEGIN:VCARD", "1990", "Вымышленное имя", "fictional-uid", PASSWORD):
            self.assertNotIn(private, repr(result))
        self.assertEqual([method for method, _ in self.calls], ["PROPFIND", "PROPFIND", "PROPFIND", "REPORT"])

    def test_final_report_host_aliases_and_url_spellings_keep_book_identity(self):
        original = collect_contact_birthdays().contacts
        for destination in (
            "https://contacts.icloud.com/123/carddavhome/book/",
            "https://p43-contacts.icloud.com/123/carddavhome/book/",
            "https://p42-contacts.icloud.com/123/carddavhome/book",
            "https://p43-contacts.icloud.com/123/carddavhome/%62ook/",
        ):
            with self.subTest(destination=destination):
                self.final_book_url = destination
                self.assertEqual(collect_contact_birthdays().contacts, original)

    def test_final_report_collection_path_is_used_after_redirect(self):
        destination = "https://p43-contacts.icloud.com/123/carddavhome/relocated/"
        self.final_book_url = destination
        contact = collect_contact_birthdays().contacts[0]
        self.assertEqual(contact.book_id, canonical_contact_book_id(destination))
        self.assertNotEqual(contact.book_id, canonical_contact_book_id(BOOK))

    def test_same_contact_uid_in_distinct_addressbooks_stays_separate(self):
        self.books[HOME + "second/"] = {"two.vcf": card()}
        contacts = collect_contact_birthdays().contacts
        self.assertEqual(len(contacts), 2)
        self.assertEqual(contacts[0].uid, contacts[1].uid)
        self.assertNotEqual(contacts[0].book_id, contacts[1].book_id)

    def test_credentials_can_be_supplied_without_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(len(collect_contact_birthdays(Credentials(USER, PASSWORD)).contacts), 1)

    def test_empty_birthdays_and_no_addressbooks_are_valid_complete_results(self):
        self.books[BOOK] = {}
        self.assertEqual(collect_contact_birthdays().contacts, [])
        self.books = {}
        result = collect_contact_birthdays()
        self.assertEqual((result.contacts, result.addressbooks), ([], 0))

    def test_multiple_books_are_all_read_and_scoped(self):
        self.books[HOME + "second/"] = {"two.vcf": card("--0229", uid="second-uid")}
        result = collect_contact_birthdays()
        self.assertEqual((result.addressbooks, len(result.contacts)), (2, 2))
        self.assertEqual((result.contacts[1].month, result.contacts[1].day), (2, 29))

    def test_unknown_raw_fields_are_not_returned_even_if_server_ignores_projection(self):
        self.books[BOOK]["one.vcf"] = card(extra="EMAIL:private@example.invalid\r\nTEL:+123456789\r\nNOTE:private note\r\n")
        contact = collect_contact_birthdays().contacts[0]
        for private in ("private@example.invalid", "+123456789", "private note"):
            self.assertNotIn(private, repr(contact))
            self.assertNotIn(private, contact.name)

    def test_icloud_can_ignore_birthday_filter_without_losing_real_birthdays(self):
        without_birthday = card(extra="EMAIL:private@example.invalid\r\nNOTE:private note\r\n").replace(
            "BDAY:1990-10-18\r\n", "")
        self.books[BOOK] = {"not-a-birthday.vcf": without_birthday,
                            "birthday.vcf": card("--10-20", uid="birthday-uid")}
        result = collect_contact_birthdays()
        self.assertEqual(len(result.contacts), 1)
        self.assertEqual((result.contacts[0].uid, result.contacts[0].month, result.contacts[0].day),
                         ("birthday-uid", 10, 20))
        self.assertNotIn("private@example.invalid", repr(result))
        with patch("calendar_digest.contact_birthdays_source.MAX_CONTACTS", 1):
            with self.assertRaises(CalendarConnectionError):
                collect_contact_birthdays()

    def test_nonbirthday_contacts_in_other_books_also_consume_card_limit(self):
        without_birthday = card().replace("BDAY:1990-10-18\r\n", "")
        self.books[BOOK] = {"one.vcf": without_birthday}
        self.books[HOME + "second/"] = {"two.vcf": without_birthday}
        with patch("calendar_digest.contact_birthdays_source.MAX_CONTACTS", 1):
            with self.assertRaises(CalendarConnectionError):
                collect_contact_birthdays()

    def test_late_errors_do_not_return_partial_contacts_or_private_diagnostics(self):
        for stage in ("principal", "home", "books", "contacts"):
            with self.subTest(stage=stage):
                self.fail_stage = stage
                before_logging = logging.root.manager.disable
                with self.assertRaises(CalendarConnectionError) as error:
                    collect_contact_birthdays()
                self.assertEqual(logging.root.manager.disable, before_logging)
                for private in (PASSWORD, "private server response", "https://", USER):
                    self.assertNotIn(private, str(error.exception))

    def test_http_failures_are_safe(self):
        for status in (401, 403, 404, 500, 200):
            with self.subTest(status=status):
                self.http_status = status
                with self.assertRaises(CalendarConnectionError):
                    collect_contact_birthdays()

    def test_partial_propstats_and_missing_address_data_are_rejected(self):
        transforms = (
            lambda body: body.replace("200 OK", "403 Forbidden"),
            lambda body: body.replace("200 OK", "404 Not Found"),
            lambda body: body.replace("c:address-data", "c:unknown"),
        )
        for transform in transforms:
            with self.subTest(transform=transforms.index(transform)):
                self.mutate = lambda stage, body: transform(body) if stage == "contacts" else body
                with self.assertRaises(CalendarConnectionError):
                    collect_contact_birthdays()

    def test_duplicate_hrefs_and_uid_conflicts_fail_safely(self):
        self.books[BOOK]["other.vcf"] = card("2000-01-01")
        with self.assertRaises(CalendarConnectionError):
            collect_contact_birthdays()
        self.books[BOOK].pop("other.vcf")
        self.mutate = lambda stage, body: body.replace("</d:multistatus>",
            body[body.index("<d:response>"):body.index("</d:multistatus>")] + "</d:multistatus>") \
            if stage == "contacts" else body
        with self.assertRaises(CalendarConnectionError):
            collect_contact_birthdays()

    def test_discovery_and_member_destinations_are_restricted(self):
        for destination in ("https://evil.example/", "http://contacts.icloud.com/", "https://example.icloud.com/",
                            "https://contacts.icloud.com:444/", "https://user:pass@contacts.icloud.com/"):
            with self.subTest(destination=destination):
                self.mutate = lambda stage, body: body.replace(PRINCIPAL, destination) if stage == "principal" else body
                with self.assertRaises(CalendarConnectionError):
                    collect_contact_birthdays()
        for destination in (HOME + "other/one.vcf", "https://p43-contacts.icloud.com/123/carddavhome/book/one.vcf",
                            BOOK + "%2e%2e/other/one.vcf", BOOK + "%252e%252e%252fprivate.vcf"):
            with self.subTest(destination=destination):
                self.mutate = lambda stage, body: body.replace(BOOK + "one.vcf", destination) if stage == "contacts" else body
                with self.assertRaises(CalendarConnectionError):
                    collect_contact_birthdays()

    def test_home_marker_and_discovered_resource_types_are_required(self):
        for transform in (lambda body: body.replace("<d:resourcetype>", "<d:unknown>").replace("</d:resourcetype>", "</d:unknown>"),
                          lambda body: multistatus()):
            self.mutate = lambda stage, body: transform(body) if stage == "books" else body
            with self.assertRaises(CalendarConnectionError):
                collect_contact_birthdays()

    def test_count_and_streaming_byte_limits_fail_closed(self):
        for constant, limit in (("MAX_CONTACTS", 0), ("MAX_ADDRESSBOOKS", 0),
                                ("MAX_RESPONSE_BYTES", 10), ("MAX_TOTAL_BYTES", 10)):
            with self.subTest(constant=constant), patch("calendar_digest.contact_birthdays_source." + constant, limit):
                with self.assertRaises(CalendarConnectionError):
                    collect_contact_birthdays()

    def test_xml_entities_and_error_markers_are_rejected(self):
        for transform in (lambda body: '<!DOCTYPE root [<!ENTITY secret "private">]>' + body,
                          lambda body: body.replace("</d:multistatus>", "<d:error/></d:multistatus>"),
                          lambda body: "private malformed XML " + PASSWORD):
            self.mutate = lambda stage, body: transform(body) if stage == "contacts" else body
            with self.assertRaises(CalendarConnectionError) as error:
                collect_contact_birthdays()
            self.assertNotIn(PASSWORD, str(error.exception))


class VCardBirthdayTests(unittest.TestCase):
    def test_supported_dates_ignore_year_and_keep_leap_day(self):
        for value in ("1980-02-29", "19800229", "1604-02-29", "--02-29", "--0229"):
            with self.subTest(value=value):
                item = _birthday(card(value), BOOK)
                self.assertEqual((item.month, item.day), (2, 29))
        apple = card("1604-10-18").replace("BDAY:", "item1.BDAY;X-APPLE-OMIT-YEAR=1604:")
        self.assertEqual((_birthday(apple, BOOK).month, _birthday(apple, BOOK).day), (10, 18))

    def test_folded_unicode_escaped_names_uid_and_quoted_parameters(self):
        raw = card(name='Иван\\, Петров\\;\r\n ич\\nВторая строка\\\\', uid='urn:uuid:example\\,uid')
        raw = raw.replace("FN:", 'FN;X-LABEL="prefix:label":')
        item = _birthday(raw, BOOK)
        self.assertEqual(item.name, 'Иван, Петров;ич Вторая строка\\')
        self.assertEqual(item.uid, "urn:uuid:example,uid")

    def test_invalid_or_partial_cards_do_not_silently_drop_birthdays(self):
        variants = [card("--0230"), card("2020-13-01"), card("unknown"), card().replace("FN:Вымышленное имя\r\n", ""),
                    card().replace("UID:fictional-uid\r\n", ""),
                    card().replace("END:VCARD", "BDAY:2000-01-01\r\nEND:VCARD"),
                    card().replace("END:VCARD", "FN:Another\r\nEND:VCARD"),
                    card(name=""), card(uid=""), card(version="2.1"), card(name="bad\\xescape"),
                    card().replace("FN:", "FN;ENCODING=QUOTED-PRINTABLE:"), card() + card(), "not a vCard"]
        variants.extend(card().replace("BDAY:", header) for header in (
            "BDAY;VALUE=text:", 'BDAY;VALUE="text":', "BDAY;VALUE=uri:", "BDAY;VALUE=date;VALUE=text:",
        ))
        for value in variants:
            with self.subTest(index=variants.index(value)):
                with self.assertRaises(CalendarConnectionError):
                    _birthday(value, BOOK)

    def test_valid_contact_without_birthday_is_skipped_but_malformed_one_is_not(self):
        without_birthday = card().replace("BDAY:1990-10-18\r\n", "")
        self.assertIsNone(_birthday(without_birthday, BOOK))
        self.assertIsNone(_birthday(without_birthday.replace("UID:fictional-uid\r\n", ""), BOOK))
        with self.assertRaises(CalendarConnectionError):
            _birthday(without_birthday.replace("END:VCARD", ""), BOOK)


if __name__ == "__main__":
    unittest.main()
