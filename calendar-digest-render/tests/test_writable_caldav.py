"""Real DAV discovery/parsing with an in-memory HTTP boundary; no network."""

import base64
from dataclasses import replace
import logging
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from icalendar import Calendar
import niquests

from calendar_digest.event_actions import EventTarget
from calendar_digest.writable_caldav import (
    CalDAVActionBackend, CalDAVActionError, CalDAVConflict, CalDAVUncertain,
    ScopedWriteSession, _provider_etag, validate_change_capability,
)
from test_icloud import calendar_properties, dav_response, multistatus, response_for


PASSWORD = 'fictional-action-password'
UID = 'fictional-action@example.invalid'
PATTERN = r'(?:caldav|p[0-9]+-caldav)\.icloud\.com'
TARGET_URL = 'https://p42-caldav.icloud.com/home/family/event.ics'


def raw_event(*, uid=UID, status='TENTATIVE', email='fictional-icloud@example.invalid'):
    return '\r\n'.join([
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Action Tests//EN',
        'BEGIN:VEVENT', f'UID:{uid}', 'DTSTAMP:20261001T000000Z',
        'DTSTART:20261014T090000Z', 'DTEND:20261014T100000Z',
        'SUMMARY:Fictional event', f'STATUS:{status}',
        f'ORGANIZER:mailto:{email}', 'END:VEVENT', 'END:VCALENDAR', '',
    ])


def target(source='icloud'):
    return EventTarget(source, 'family', UID,
        '2026-10-14T12:00:00+03:00', '2026-10-14T13:00:00+03:00',
        expected_event_status='TENTATIVE')


class ScopedTransportTests(unittest.TestCase):
    def make_session(self):
        session = ScopedWriteSession(hostname_pattern=PATTERN,
            resource_url=TARGET_URL, etag='"v1"')
        self.addCleanup(session.close)
        return session

    def test_only_exact_conditional_put_can_receive_authentication(self):
        session = self.make_session()
        session.auth = Mock()
        with patch.object(niquests.Session, 'send', autospec=True) as transport:
            request = niquests.Request('PUT', TARGET_URL, headers={'If-Match': '"v1"'}).prepare()
            session.send(request, verify=False, allow_redirects=True)
            session.auth.assert_called_once_with(request)
            self.assertFalse(transport.call_args.kwargs['allow_redirects'])
            self.assertTrue(transport.call_args.kwargs['verify'])
            self.assertEqual(transport.call_args.kwargs['timeout'], 20)
            self.assertIs(session.adapters['https://'].max_retries, False)

    def test_untrusted_or_unscoped_operations_stop_before_auth_and_transport(self):
        attempts = [
            ('PUT', TARGET_URL, {}), ('PUT', TARGET_URL, {'If-Match': '*'}),
            ('PUT', TARGET_URL, {'If-Match': '"v2"'}),
            ('PUT', TARGET_URL + '/another', {'If-Match': '"v1"'}),
            ('DELETE', TARGET_URL, {'If-Match': '"v1"'}),
            ('POST', TARGET_URL, {}),
            ('GET', 'https://foreign.invalid/event.ics', {}),
            ('GET', TARGET_URL.replace('https:', 'http:'), {}),
            ('GET', TARGET_URL.replace('p42-', 'user:password@p42-'), {}),
            ('GET', TARGET_URL.replace('.com/', '.com:8443/'), {}),
            ('GET', TARGET_URL + '?other=1', {}),
        ]
        for method, url, headers in attempts:
            with self.subTest(method=method, kind=attempts.index((method,url,headers))):
                session = self.make_session()
                session.auth = Mock()
                with patch.object(niquests.Session, 'send') as transport:
                    with self.assertRaises(CalDAVActionError):
                        session.send(niquests.Request(method, url, headers=headers).prepare())
                    session.auth.assert_not_called()
                    transport.assert_not_called()

    def test_weak_wildcard_and_injected_etags_are_rejected(self):
        for etag in ('W/"v1"', '*', '"v1", "v2"', '"v1"\r\nInjected: yes', None):
            with self.subTest(etag=etag):
                with self.assertRaises(CalDAVActionError):
                    ScopedWriteSession(hostname_pattern=PATTERN, resource_url=TARGET_URL, etag=etag)

    def test_discovered_url_is_pinned_after_normal_http_preparation(self):
        url = TARGET_URL.replace('event.ics', 'event%41.ics')
        with ScopedWriteSession(hostname_pattern=PATTERN, resource_url=url, etag='"v1"') as session:
            request = niquests.Request('PUT',url,headers={'If-Match':'"v1"'}).prepare()
            with patch.object(niquests.Session,'send') as transport:
                session.send(request)
                transport.assert_called_once()

    def test_only_observed_mailru_bare_hex_token_is_normalized(self):
        token = 'abcdef0123456789' * 2 + 'abcdef01'
        self.assertEqual(_provider_etag(token,'mailru_1'),'"'+token+'"')
        for source,etag in [('icloud',token),('mailru_1','W/'+token),
                            ('mailru_1','abc'),('mailru_1','g'*40)]:
            with self.subTest(source=source,etag_kind=len(etag)):
                with self.assertRaises(CalDAVActionError):
                    _provider_etag(etag,source)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.accounts = {}
        environment = {}
        for source, prefix in [('icloud','ICLOUD'), *[(f'mailru_{i}',f'MAILRU_{i}') for i in (1,2,3)]]:
            email = f'fictional-{source}@example.invalid'
            environment[prefix + '_USERNAME'] = email
            environment[prefix + '_APP_PASSWORD'] = PASSWORD
            auth = 'Basic ' + base64.b64encode(f'{email}:{PASSWORD}'.encode()).decode()
            self.accounts[auth] = source
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.transport = self.enterContext(patch.object(niquests.Session, 'send', autospec=True))
        self.transport.side_effect = self.send
        self.backend = CalDAVActionBackend()
        self.data = raw_event()
        self.etag = '"v1"'
        self.requests = []
        self.put_status = 204
        self.put_timeout = False
        self.accept_put = True
        self.read_failure_after_put = False
        self.acl = 'write-content'
        self.address_matches = True
        self.report_href = None
        self.ambiguous = False
        self.substring_record = False
        self.acl_href = None
        self.transform_saved = None
        self.put_unexpected_error = False
        self.icloud_uid_unsupported = False
        self.resource_name = 'event.ics'
        self.metadata_href = None
        self.projection_count = 0

    def send(self, session, request, **kwargs):
        self.assertTrue(kwargs['verify'])
        source = self.accounts[request.headers['Authorization']]
        if source == 'icloud':
            root = 'https://caldav.icloud.com/'
            principal = 'https://caldav.icloud.com/123/principal/'
            home = 'https://caldav.icloud.com/123/calendars/'
        else:
            root = 'https://calendar.mail.ru/principals/'
            principal = 'https://calendar.mail.ru/principals/users/example/'
            home = 'https://calendar.mail.ru/calendars/users/example/'
        calendar_url = home + 'family/'
        event_url = calendar_url + self.resource_name
        self.requests.append((request.method, request.url))
        if request.method == 'PROPFIND':
            body = request.body or b''
            body = body.encode() if isinstance(body,str) else body
            query = ET.fromstring(body)
            if request.headers.get('Depth')=='1' and query.find('.//{DAV:}getetag') is not None:
                payload=dav_response(self.metadata_href or event_url,
                    '<d:getetag>"v1"</d:getetag><d:resourcetype/>')
            elif query.find('.//{DAV:}current-user-privilege-set') is not None:
                if self.acl is None:
                    payload = dav_response(request.url, '<d:current-user-privilege-set/>', '404 Not Found')
                else:
                    payload = dav_response(self.acl_href or request.url,
                        '<d:current-user-privilege-set><d:privilege>'
                        f'<d:{self.acl}/></d:privilege></d:current-user-privilege-set>')
            elif query.find('.//{urn:ietf:params:xml:ns:caldav}calendar-user-address-set') is not None:
                address = f'fictional-{source}@example.invalid' if self.address_matches else 'other@example.invalid'
                payload = dav_response(principal,
                    '<c:calendar-user-address-set><d:href>'
                    f'mailto:{address}</d:href></c:calendar-user-address-set>')
            elif request.url == root:
                payload = dav_response(root, '<d:current-user-principal><d:href>'
                    f'{principal}</d:href></d:current-user-principal>')
            elif request.url == principal:
                payload = dav_response(principal,
                    f'<c:calendar-home-set><d:href>{home}</d:href></c:calendar-home-set>')
            elif request.url in {home, calendar_url}:
                payload = dav_response(calendar_url, calendar_properties('Family') +
                    '<c:supported-calendar-component-set><c:comp name="VEVENT"/>'
                    '</c:supported-calendar-component-set>')
            else:
                self.fail('Unexpected discovery request')
            return response_for(request, multistatus(payload))
        if request.method == 'OPTIONS':
            result = response_for(request, '', status=200)
            result.headers['DAV'] = '1, calendar-access, calendar-auto-schedule'
            return result
        if request.method == 'REPORT':
            query = ET.fromstring(request.body)
            uid_filter=query.findtext('.//{urn:ietf:params:xml:ns:caldav}text-match')
            if self.icloud_uid_unsupported and uid_filter is not None:
                return response_for(request,'',status=412)
            if uid_filter is None:
                self.projection_count+=1
                self.assertIsNotNone(query.find('.//{urn:ietf:params:xml:ns:caldav}calendar-data/{urn:ietf:params:xml:ns:caldav}comp'))
            else:
                self.assertEqual(uid_filter, UID)
            self.assertIsNone(query.find('.//{urn:ietf:params:xml:ns:caldav}time-range'))
            payload = dav_response(self.report_href or event_url,
                f'<c:calendar-data>{escape(self.data)}</c:calendar-data>')
            payload += dav_response(calendar_url, '<c:calendar-data/>', '404 Not Found')
            if self.ambiguous:
                payload += dav_response(calendar_url+'duplicate.ics',
                    f'<c:calendar-data>{escape(self.data)}</c:calendar-data>')
            if self.substring_record:
                other = raw_event(uid='prefix-'+UID+'-suffix')
                payload += dav_response(calendar_url+'other.ics',
                    f'<c:calendar-data>{escape(other)}</c:calendar-data>')
            return response_for(request, multistatus(payload))
        if request.method == 'GET':
            self.assertEqual(request.url, event_url)
            if self.read_failure_after_put and any(method=='PUT' for method,_ in self.requests):
                raise niquests.exceptions.Timeout('private response ' + PASSWORD)
            result = response_for(request, self.data, status=200)
            result.headers['ETag'] = self.etag
            return result
        if request.method == 'PUT':
            self.assertEqual(request.url, event_url)
            self.assertEqual(request.headers['If-Match'], self.etag)
            self.assertFalse(kwargs['allow_redirects'])
            if self.accept_put:
                self.data = request.body.decode()
                if self.transform_saved is not None:
                    self.data = self.transform_saved(self.data)
                self.etag = '"v2"'
            if self.put_unexpected_error:
                raise RuntimeError('private runtime ' + PASSWORD)
            if self.put_timeout:
                raise niquests.exceptions.Timeout('private response ' + PASSWORD)
            return response_for(request, '', status=self.put_status)
        self.fail('Unexpected mutation method')

    def put_count(self):
        return sum(method == 'PUT' for method,_ in self.requests)

    def test_reads_fresh_raw_data_and_strong_etag_for_each_distinct_account(self):
        for source in ('icloud','mailru_1','mailru_2','mailru_3'):
            with self.subTest(source=source):
                self.data = raw_event(email=f'fictional-{source}@example.invalid')
                version = self.backend.read(target(source))
                self.assertEqual(version.data, self.data)
                self.assertEqual(version.etag, '"v1"')
                self.assertEqual(version.account_email, f'fictional-{source}@example.invalid')
                self.assertTrue(version.can_write)
                self.assertTrue(version.can_organize)
                self.assertTrue(version.automatic_scheduling)
                self.assertEqual(version.status_cancellation_supported,source=='icloud')
                for private in (version.data, version.etag, version.account_email, version.resource_url):
                    self.assertNotIn(private, repr(version))
        self.assertEqual(self.put_count(), 0)

    def test_exact_uid_filter_ignores_substring_results_and_icloud_collection(self):
        self.substring_record = True
        self.assertEqual(self.backend.read(target()).uid, UID)

    def test_icloud_412_uses_only_server_listed_exact_basename_then_checks_uid(self):
        self.icloud_uid_unsupported=True
        self.resource_name=UID+'.ics'
        self.assertEqual(self.backend.read(target()).uid,UID)
        self.assertEqual(self.projection_count,0)
        self.data=raw_event(uid='different@example.invalid')
        with self.assertRaises(CalDAVActionError):
            self.backend.read(target())

    def test_icloud_opaque_filename_uses_bounded_uid_projection(self):
        self.icloud_uid_unsupported=True
        self.assertEqual(self.backend.read(target()).uid,UID)
        self.assertEqual(self.projection_count,1)
        self.resource_name='prefix-'+UID+'.ics'
        self.data=raw_event(uid='prefix-'+UID)
        with self.assertRaises(CalDAVActionError):
            self.backend.read(target())

    def test_icloud_fallback_rejects_an_external_metadata_href(self):
        self.icloud_uid_unsupported=True
        self.metadata_href='https://foreign.invalid/'+UID+'.ics'
        with self.assertRaises(CalDAVActionError):
            self.backend.read(target())
        self.assertFalse(any(method=='GET' for method,_ in self.requests))

    def test_ambiguous_or_external_resource_is_rejected_without_get(self):
        for href, ambiguous in [('https://foreign.invalid/event.ics',False),
                                ('https://caldav.icloud.com/elsewhere/event.ics',False),
                                (None,True)]:
            with self.subTest(ambiguous=ambiguous, external=href is not None):
                self.report_href, self.ambiguous = href, ambiguous
                self.requests.clear()
                with self.assertRaises(CalDAVActionError):
                    self.backend.read(target())
                self.assertFalse(any(method=='GET' for method,_ in self.requests))

    def test_put_is_conditional_single_and_verified_by_get(self):
        version = self.backend.read(target())
        new_data = raw_event(status='CONFIRMED')
        stored = self.backend.write(target(), version, new_data)
        self.assertEqual(stored.etag, '"v2"')
        self.assertEqual(Calendar.from_ical(stored.data).walk('VEVENT')[0]['STATUS'], 'CONFIRMED')
        self.assertEqual(self.put_count(), 1)
        self.assertEqual(self.requests[-1][0], 'GET')

    def test_changed_form_revision_rejects_before_put(self):
        version = self.backend.read(target())
        self.etag = '"other-client"'
        with self.assertRaises(CalDAVConflict):
            self.backend.write(target(), version, raw_event(status='CONFIRMED'))
        self.assertEqual(self.put_count(), 0)

    def test_stable_revision_allows_only_dynamic_read_timestamp(self):
        version=self.backend.read(target())
        self.data=self.data.replace('DTSTAMP:20261001T000000Z','DTSTAMP:20261002T000000Z')
        stored=self.backend.write(target(),version,raw_event(status='CONFIRMED'))
        self.assertEqual(stored.etag,'"v2"')
        self.assertEqual(self.put_count(),1)

    def test_stable_etag_cannot_hide_changed_event_fields(self):
        version=self.backend.read(target())
        self.data=self.data.replace('SUMMARY:Fictional event','SUMMARY:Changed elsewhere')
        with self.assertRaises(CalDAVConflict):
            self.backend.write(target(),version,raw_event(status='CONFIRMED'))
        self.assertEqual(self.put_count(),0)

    def test_precondition_conflict_is_not_retried(self):
        version = self.backend.read(target())
        self.put_status, self.accept_put = 412, False
        with self.assertRaises(CalDAVConflict):
            self.backend.write(target(), version, raw_event(status='CONFIRMED'))
        self.assertEqual(self.put_count(), 1)

    def test_timeout_after_commit_is_resolved_by_get_without_second_put(self):
        version = self.backend.read(target())
        self.put_timeout = True
        stored = self.backend.write(target(), version, raw_event(status='CONFIRMED'))
        self.assertEqual(stored.etag, '"v2"')
        self.assertEqual(self.put_count(), 1)

    def test_unexpected_transport_failure_after_commit_also_verifies_without_retry(self):
        version = self.backend.read(target())
        self.put_unexpected_error = True
        stored = self.backend.write(target(),version,raw_event(status='CONFIRMED'))
        self.assertEqual(stored.etag,'"v2"')
        self.assertEqual(self.put_count(),1)

    def test_server_creation_status_for_conditional_update_is_uncertain(self):
        version = self.backend.read(target())
        self.put_status = 201
        with self.assertRaises(CalDAVUncertain):
            self.backend.write(target(),version,raw_event(status='CONFIRMED'))
        self.assertEqual(self.put_count(),1)

    def test_semantic_verifier_does_not_hide_loss_of_other_event_fields(self):
        version = self.backend.read(target())
        self.transform_saved = lambda data:data.replace('SUMMARY:Fictional event\r\n','')
        with self.assertRaises(CalDAVUncertain):
            self.backend.write(target(),version,raw_event(status='CONFIRMED'),verify=lambda data:True)

    def test_semantic_verifier_can_accept_server_timestamp_normalization(self):
        version = self.backend.read(target())
        self.transform_saved = lambda data:data.replace('DTSTAMP:20261001T000000Z','DTSTAMP:20261002T000000Z')
        stored = self.backend.write(target(),version,raw_event(status='CONFIRMED'),
            verify=lambda data:Calendar.from_ical(data).walk('VEVENT')[0]['STATUS']=='CONFIRMED')
        self.assertEqual(stored.etag,'"v2"')

    def test_mailru_201_update_and_provider_alarm_ack_timestamp_require_verified_data(self):
        email='fictional-mailru_1@example.invalid'
        self.data=raw_event(email=email).replace('END:VEVENT','X-MOZ-LASTACK:20261001T000000Z\r\nEND:VEVENT')
        version=self.backend.read(target('mailru_1'))
        new_data=self.data.replace('STATUS:TENTATIVE','STATUS:CONFIRMED')
        self.put_status=201
        self.transform_saved=lambda data:data.replace('X-MOZ-LASTACK:20261001T000000Z',
            'X-MOZ-LASTACK:20261002T000000Z')
        stored=self.backend.write(target('mailru_1'),version,new_data,
            verify=lambda data:Calendar.from_ical(data).walk('VEVENT')[0]['STATUS']=='CONFIRMED')
        self.assertEqual(stored.etag,'"v2"')
        self.assertEqual(self.put_count(),1)

    def test_mailru_status_cancellation_is_rejected_before_any_write(self):
        self.data=raw_event(email='fictional-mailru_1@example.invalid',status='CONFIRMED')
        version=self.backend.read(target('mailru_1'))
        new_data=self.data.replace('STATUS:CONFIRMED','STATUS:CANCELLED')
        self.requests.clear()
        with self.assertRaisesRegex(CalDAVActionError,'Изменения не отправлены'):
            self.backend.write(target('mailru_1'),version,new_data,verify=lambda data:True)
        self.assertEqual(self.requests,[])

    def test_existing_cancelled_override_does_not_block_another_change(self):
        version=self.backend.read(target('mailru_1'))
        series=raw_event(status='CONFIRMED').replace('STATUS:CONFIRMED',
            'RRULE:FREQ=DAILY;COUNT=2\r\nSTATUS:CONFIRMED')
        override='\r\n'.join(['BEGIN:VEVENT','UID:'+UID,'RECURRENCE-ID:20261015T090000Z',
            'DTSTART:20261015T090000Z','DTEND:20261015T100000Z','STATUS:CANCELLED','END:VEVENT',''])
        series=series.replace('END:VCALENDAR',override+'END:VCALENDAR')
        version=replace(version,data=series)
        self.requests.clear()
        validate_change_capability(version,SimpleNamespace(data=series.replace(
            'SUMMARY:Fictional event','SUMMARY:Preserved cancellation with another edit')))
        with self.assertRaises(CalDAVActionError):
            validate_change_capability(version,SimpleNamespace(data=series.replace(
                'STATUS:CONFIRMED','STATUS:CANCELLED')))
        self.assertEqual(self.requests,[])

    def test_timeout_without_verified_change_has_safe_uncertain_result(self):
        for accept, fail_get in ((False,False),(True,True)):
            with self.subTest(accept=accept):
                self.data, self.etag = raw_event(), '"v1"'
                self.requests.clear()
                self.read_failure_after_put = False
                version = self.backend.read(target())
                self.put_timeout, self.accept_put = True, accept
                self.read_failure_after_put = fail_get
                with self.assertRaises(CalDAVUncertain) as error:
                    self.backend.write(target(), version, raw_event(status='CONFIRMED'))
                self.assertEqual(self.put_count(), 1)
                self.assertNotIn(PASSWORD, str(error.exception))

    def test_explicit_readonly_and_unknown_acl_block_write(self):
        for acl in ('read',None):
            with self.subTest(acl=acl):
                self.acl = acl
                version = self.backend.read(target())
                self.assertFalse(version.can_organize)
                with self.assertRaises(CalDAVActionError):
                    self.backend.write(target(), version, raw_event(status='CONFIRMED'))
                self.assertEqual(self.put_count(), 0)

    def test_organizer_claim_requires_authenticated_principal_address(self):
        self.address_matches = False
        self.assertFalse(self.backend.read(target()).can_organize)

    def test_acl_of_an_unrelated_href_is_not_authority_for_this_resource(self):
        self.acl_href = 'https://caldav.icloud.com/another-calendar/'
        with self.assertRaises(CalDAVActionError):
            self.backend.read(target())
        self.assertEqual(self.put_count(),0)

    def test_weak_revision_and_wrong_uid_are_rejected(self):
        self.etag = 'W/"weak"'
        with self.assertRaises(CalDAVActionError):
            self.backend.read(target())
        self.etag = '"v1"'
        version = self.backend.read(target())
        with self.assertRaises(CalDAVActionError):
            self.backend.write(target(), version, raw_event(uid='wrong@example.invalid'))
        with self.assertRaises(CalDAVActionError):
            self.backend.write(target(), replace(version,resource_url='https://foreign.invalid/x'), raw_event(status='CONFIRMED'))
        self.assertEqual(self.put_count(), 0)

    def test_library_failures_suppress_private_diagnostics_and_restore_logging(self):
        before = logging.root.manager.disable
        self.transport.side_effect = RuntimeError('private URL https://foreign.invalid/' + PASSWORD)
        with self.assertRaises(CalDAVActionError) as error:
            self.backend.read(target())
        for private in ('https://',PASSWORD,'private URL'):
            self.assertNotIn(private,str(error.exception))
        self.assertEqual(logging.root.manager.disable,before)


if __name__ == '__main__':
    unittest.main()
