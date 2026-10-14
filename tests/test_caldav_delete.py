"""Conditional personal-event deletion against an in-memory DAV provider."""

from dataclasses import replace
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

import niquests

from calendar_digest.personal_cancellation import (
    prepare_personal_cancellation, verify_personal_cancellation,
)
from calendar_digest.writable_caldav import (
    CalDAVActionBackend, CalDAVActionError, CalDAVConflict, CalDAVUncertain,
    ScopedDeleteSession,
)
import test_writable_caldav as fixtures


MAIL_URL = 'https://calendar.mail.ru/calendars/users/example/family/event.ics'
MAIL_PATTERN = r'calendar\.mail\.ru'


def personal_event(source='mailru_1'):
    data = fixtures.raw_event(email=f'fictional-{source}@example.invalid')
    return data.replace(f'ORGANIZER:mailto:fictional-{source}@example.invalid\r\n', '')


class ScopedDeleteTests(unittest.TestCase):
    def test_exact_delete_and_verifying_get_force_tls_and_disable_redirects_and_retries(self):
        with ScopedDeleteSession(hostname_pattern=MAIL_PATTERN,
                resource_url=MAIL_URL, etag='"v1"') as session:
            session.auth = Mock()
            with patch.object(niquests.Session, 'send', autospec=True) as transport:
                for method in ('DELETE', 'GET'):
                    request = niquests.Request(method, MAIL_URL,
                        headers={'If-Match': '"v1"'} if method == 'DELETE' else {}).prepare()
                    session.send(request, verify=False, allow_redirects=True, timeout=100)
                    session.auth.assert_called_with(request)
                    self.assertTrue(transport.call_args.kwargs['verify'])
                    self.assertFalse(transport.call_args.kwargs['allow_redirects'])
                    self.assertEqual(transport.call_args.kwargs['timeout'], 20)
                    self.assertIs(session.adapters['https://'].max_retries, False)

    def test_other_resources_methods_and_etags_are_blocked_before_authentication(self):
        attempts = [
            ('DELETE', MAIL_URL, {}),
            ('DELETE', MAIL_URL, {'If-Match': '*'}),
            ('DELETE', MAIL_URL, {'If-Match': '"other"'}),
            ('DELETE', MAIL_URL + '-other', {'If-Match': '"v1"'}),
            ('GET', MAIL_URL + '-other', {}),
            ('GET', MAIL_URL.replace('https:', 'http:'), {}),
            ('GET', 'https://foreign.invalid/event.ics', {}),
            ('PUT', MAIL_URL, {'If-Match': '"v1"'}),
            ('POST', MAIL_URL, {}), ('PROPFIND', MAIL_URL, {}),
        ]
        for method, url, headers in attempts:
            with self.subTest(method=method, url_kind=url == MAIL_URL):
                with ScopedDeleteSession(hostname_pattern=MAIL_PATTERN,
                        resource_url=MAIL_URL, etag='"v1"') as session:
                    session.auth = Mock()
                    with patch.object(niquests.Session, 'send') as transport:
                        with self.assertRaises(CalDAVActionError):
                            session.send(niquests.Request(method, url, headers=headers).prepare())
                        session.auth.assert_not_called()
                        transport.assert_not_called()


class DeleteBackendTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing real CalDAV discovery boundary without inheriting
        # its unrelated PUT tests or bypassing the new ACL discovery.
        fixtures.BackendTests.setUp(self)
        self.data = personal_event()
        self.resource_acl = ('write-content',)
        self.collection_acl = ('write-content', 'unbind')
        self.exists = True
        self.accept_delete = True
        self.delete_status = 204
        self.delete_timeout = False
        self.read_failure_after_delete = False
        self.verification_status = None

    def send(self, session, request, **kwargs):
        if request.method == 'PROPFIND':
            body = request.body or b''
            if ET.fromstring(body).find('.//{DAV:}current-user-privilege-set') is not None:
                self.requests.append((request.method, request.url))
                names = self.resource_acl if request.url.endswith('.ics') else self.collection_acl
                if names is None:
                    payload = fixtures.dav_response(request.url,
                        '<d:current-user-privilege-set/>', '404 Not Found')
                else:
                    privileges = ''.join(f'<d:privilege><d:{name}/></d:privilege>' for name in names)
                    payload = fixtures.dav_response(request.url,
                        f'<d:current-user-privilege-set>{privileges}</d:current-user-privilege-set>')
                return fixtures.response_for(request, fixtures.multistatus(payload))
        if request.method == 'DELETE':
            self.requests.append((request.method, request.url))
            self.assertEqual(request.url, MAIL_URL)
            self.assertEqual(request.headers.get('If-Match'), self.etag)
            self.assertTrue(kwargs['verify'])
            self.assertFalse(kwargs['allow_redirects'])
            self.assertEqual(kwargs['timeout'], 20)
            self.assertIs(session.adapters['https://'].max_retries, False)
            if self.accept_delete:
                self.exists = False
            if self.delete_timeout:
                raise niquests.exceptions.Timeout('private diagnostic ' + fixtures.PASSWORD)
            result = fixtures.response_for(request, '', status=self.delete_status)
            if self.delete_status in {301, 302, 307, 308}:
                result.headers['Location'] = 'https://foreign.invalid/event.ics'
            return result
        if request.method == 'GET':
            if self.read_failure_after_delete and self.delete_count():
                self.requests.append((request.method, request.url))
                raise niquests.exceptions.Timeout('private diagnostic ' + fixtures.PASSWORD)
            if not self.exists or (self.delete_count() and self.verification_status):
                self.requests.append((request.method, request.url))
                self.assertFalse(kwargs['allow_redirects'])
                self.assertTrue(kwargs['verify'])
                return fixtures.response_for(request, '', status=self.verification_status or 404)
        return fixtures.BackendTests.send(self, session, request, **kwargs)

    def delete_count(self):
        return sum(method == 'DELETE' for method, _ in self.requests)

    def test_each_mail_account_deletes_once_and_proves_absence_at_same_url(self):
        for source in ('mailru_1', 'mailru_2', 'mailru_3'):
            for status in (200, 204):
                with self.subTest(source=source, status=status):
                    self.requests.clear()
                    self.exists = True
                    self.delete_status = status
                    self.data = personal_event(source)
                    target = fixtures.target(source)
                    version = self.backend.read(target)
                    self.assertTrue(version.can_delete)
                    self.assertIsNone(self.backend.delete(target, version))
                    self.assertEqual(self.delete_count(), 1)
                    self.assertEqual(self.requests[-2:], [('DELETE', MAIL_URL), ('GET', MAIL_URL)])

    def test_collection_unbind_is_required_and_resource_write_content_is_insufficient(self):
        for privileges in (None, (), ('read',), ('write-content',)):
            with self.subTest(privileges=privileges):
                self.collection_acl = privileges
                target = fixtures.target('mailru_1')
                version = self.backend.read(target)
                self.assertFalse(version.can_delete)
                self.backend.verified_acl_fallback_sources = frozenset({'mailru_1'})
                with self.assertRaises(CalDAVActionError):
                    self.backend.delete(target, version)
                self.assertEqual(self.delete_count(), 0)

    def test_parent_all_and_write_aggregate_unbind_but_resource_write_does_not(self):
        for privileges in (('all',), ('write',)):
            with self.subTest(privileges=privileges):
                self.collection_acl = privileges
                version = self.backend.read(fixtures.target('mailru_1'))
                self.assertTrue(version.can_delete)
        self.resource_acl = ('all',)
        self.collection_acl = ('write-content',)
        self.assertFalse(self.backend.read(fixtures.target('mailru_1')).can_delete)

    def test_read_only_resource_and_revoked_collection_privilege_stop_before_delete(self):
        target = fixtures.target('mailru_1')
        version = self.backend.read(target)
        self.collection_acl = ('write-content',)
        with self.assertRaises(CalDAVActionError):
            self.backend.delete(target, version)
        self.collection_acl = ('all',)
        self.resource_acl = ('read',)
        version = self.backend.read(target)
        with self.assertRaises(CalDAVActionError):
            self.backend.delete(target, version)
        self.assertEqual(self.delete_count(), 0)

    def test_changed_version_content_identity_and_resource_url_stop_before_delete(self):
        target = fixtures.target('mailru_1')
        version = self.backend.read(target)
        changes = [
            ('etag', '"other"'),
            ('data', self.data.replace('Fictional event', 'Changed event')),
            ('resource_url', MAIL_URL + '-other'),
        ]
        for name, value in changes:
            with self.subTest(name=name):
                with self.assertRaises(CalDAVConflict):
                    self.backend.delete(target, replace(version, **{name: value}))
        with self.assertRaises(CalDAVActionError):
            self.backend.delete(target, replace(version, uid='other@example.invalid'))
        self.assertEqual(self.delete_count(), 0)

    def test_missing_before_delete_is_conflict_and_sends_no_delete(self):
        target = fixtures.target('mailru_1')
        version = self.backend.read(target)
        self.exists = False
        with self.assertRaises(CalDAVConflict):
            self.backend.delete(target, version)
        self.assertEqual(self.delete_count(), 0)

    def test_412_or_missing_during_delete_is_conflict_and_never_retried(self):
        for status in (412, 404):
            with self.subTest(status=status):
                self.requests.clear()
                self.exists = True
                self.accept_delete = False
                self.delete_status = status
                target = fixtures.target('mailru_1')
                version = self.backend.read(target)
                with self.assertRaises(CalDAVConflict):
                    self.backend.delete(target, version)
                self.assertEqual(self.delete_count(), 1)
                self.assertEqual(self.requests[-1][0], 'DELETE')

    def test_timeout_redirect_async_or_server_error_never_report_success_or_retry(self):
        for status, timeout in ((204, True), (202, False), (301, False),
                                (307, False), (500, False), (201, False)):
            with self.subTest(status=status, timeout=timeout):
                self.requests.clear()
                self.exists = True
                self.delete_status, self.delete_timeout = status, timeout
                target = fixtures.target('mailru_1')
                version = self.backend.read(target)
                with self.assertRaises(CalDAVUncertain) as error:
                    self.backend.delete(target, version)
                self.assertEqual(self.delete_count(), 1)
                self.assertEqual(self.requests[-1][0], 'DELETE')
                self.assertNotIn(fixtures.PASSWORD, str(error.exception))

    def test_success_status_without_fresh_404_remains_uncertain(self):
        for retained, timeout, status in ((True, False, None), (False, True, None),
                                         (False, False, 403), (False, False, 302),
                                         (False, False, 410)):
            with self.subTest(retained=retained, timeout=timeout, status=status):
                self.requests.clear()
                self.exists = True
                self.accept_delete = not retained
                self.read_failure_after_delete = timeout
                self.verification_status = status
                target = fixtures.target('mailru_1')
                version = self.backend.read(target)
                with self.assertRaises(CalDAVUncertain):
                    self.backend.delete(target, version)
                self.assertEqual(self.delete_count(), 1)
                self.assertEqual(self.requests[-1], ('GET', MAIL_URL))

    def test_non_mail_invitation_foreign_organizer_and_recurrence_are_never_deleted(self):
        variants = [
            ('icloud', personal_event('icloud'), None),
            ('mailru_1', personal_event().replace('END:VEVENT',
                'ATTENDEE:mailto:someone@example.invalid\r\nEND:VEVENT'), None),
            ('mailru_1', personal_event().replace('END:VEVENT',
                'ORGANIZER:mailto:someone@example.invalid\r\nEND:VEVENT'), None),
            ('mailru_1', personal_event().replace('END:VEVENT',
                'RRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT'), '2026-10-14T12:00:00+03:00'),
        ]
        for source, data, recurrence in variants:
            with self.subTest(source=source, recurrence=recurrence):
                self.data = data
                target = replace(fixtures.target(source), recurrence_id=recurrence)
                version = self.backend.read(target)
                with self.assertRaises(CalDAVActionError):
                    self.backend.delete(target, version)
                self.assertEqual(self.delete_count(), 0)

    def test_owned_organizer_requires_fresh_provider_principal_match(self):
        self.data = fixtures.raw_event(email='fictional-mailru_1@example.invalid')
        target = fixtures.target('mailru_1')
        version = self.backend.read(target)
        self.assertTrue(version.can_organize)
        self.address_matches = False
        with self.assertRaises(CalDAVActionError):
            self.backend.delete(target, version)
        self.assertEqual(self.delete_count(), 0)

    def test_recurring_exclusion_uses_conditional_put_and_complete_readback(self):
        self.data = personal_event().replace('END:VEVENT',
            'RRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT')
        target = replace(fixtures.target('mailru_1'), recurrence_id='2026-10-14T12:00:00+03:00')
        version = self.backend.read(target)
        plan = prepare_personal_cancellation(version.data, target, version.account_email)
        self.assertEqual(plan.method, 'PUT')
        stored = self.backend.write(target, version, plan.data,
            verify=lambda data: verify_personal_cancellation(data, target, plan, version.account_email))
        self.assertIn('EXDATE:20261014T090000Z', stored.data)
        self.assertEqual(self.delete_count(), 0)
        self.assertEqual(sum(method == 'PUT' for method, _ in self.requests), 1)
        self.assertEqual(self.requests[-1], ('GET', MAIL_URL))


if __name__ == '__main__':
    unittest.main()
