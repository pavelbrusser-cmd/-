"""Independent participant-email cache, plan and durable delivery cycle.

Every batch follows a fresh read of the verified control calendar account.
The shared calendar gate serializes this work with digest and push collection;
its own queue never reuses their activation, snapshots or delivery receipts.
"""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

from .calendar_operation_lock import calendar_operation_lock
from .calendar_sync import CachedResource, sync_calendars, to_calendar_resources
from .control_email_delivery import CONTROL_ACCOUNT, submit_control_email
from .control_email_queue import (
    HORIZON_DAYS, OVERDUE_GRACE_SECONDS, ControlEmailQueueError, control_email_store,
)
from .control_email_rules import normalize_email, plan_control_emails
from .mailru import MailRuCredentials
from .push_timing import delivery_allowed
from .runtime import control_email_enabled
from .smtp_delivery import MailSettings, SMTPDeliveryError, SMTPDeliveryUncertain


CONTROL_SOURCE = "mailru_2"
MAX_BATCH = 50
POLICY_VERSION = "1"


class ControlEmailCycleError(Exception):
    """A safe participant-email diagnostic without event or address data."""


class _ClaimSkipped(Exception):
    """The send window or fresh-observation window closed before SMTP DATA."""


def _now():
    return datetime.now(timezone.utc)


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ControlEmailCycleError("Для писем участникам требуется время с часовым поясом.")
    return value.astimezone(timezone.utc)


def control_email_accounts() -> tuple[str, frozenset[str]]:
    """Validate the source and build owner exclusions without reading SMTP keys."""
    credentials = MailRuCredentials.from_environment(2)
    source = normalize_email(credentials.username)
    if source != CONTROL_ACCOUNT:
        raise ControlEmailCycleError("Письма участникам разрешены только для подключённого контрольного аккаунта Mail.ru 2.")
    candidates = [source, os.environ.get("GOOGLE_CALENDAR_EMAIL") or "pavel.brusser@gmail.com"]
    candidates.extend(os.environ.get(name, "") for name in (
        "ICLOUD_USERNAME", "MAILRU_1_USERNAME", "MAILRU_2_USERNAME", "MAILRU_3_USERNAME",
        "SMTP_USERNAME", "DIGEST_RECIPIENT",
    ))
    owned = set()
    for value in candidates:
        if not value.strip():
            continue
        address = normalize_email(value.strip(" \t"))
        if address is None:
            raise ControlEmailCycleError("Проверьте адреса собственных аккаунтов в защищённых настройках среды.")
        owned.add(address)
    return source, frozenset(owned)


def _policy_version(own_accounts):
    # Changing an owner exclusion must invalidate a plan even when CalDAV is
    # unchanged. The durable safe summary stores a hash, never these addresses.
    digest = hashlib.sha256(json.dumps(sorted(own_accounts), separators=(",", ":")).encode()).hexdigest()
    return POLICY_VERSION + ":" + digest[:60]


def _refresh(store, *, source, own_accounts, clock):
    previous = [CachedResource(**item) for item in store.load_cache()]
    observed_at = _instant(clock())
    synchronized = sync_calendars(previous, source_ids=(CONTROL_SOURCE,))
    moment = _instant(clock())
    summary = store.summary(moment)
    horizon = moment + timedelta(days=HORIZON_DAYS)
    planned_until = summary.get("planned_until")
    policy_version = _policy_version(own_accounts)
    needs_plan = (
        synchronized.changed != 0 or planned_until is None
        or summary.get("policy_version") != policy_version
        or datetime.fromisoformat(planned_until) < moment + timedelta(days=HORIZON_DAYS - 1)
    )
    counts = {"changed": synchronized.changed, "fetched": synchronized.fetched, "deleted": synchronized.deleted}
    if needs_plan:
        start = moment
        activated = summary.get("activated_at")
        if activated is not None:
            start = max(datetime.fromisoformat(activated), moment - timedelta(seconds=OVERDUE_GRACE_SECONDS))
        plan = plan_control_emails(
            to_calendar_resources(synchronized.resources, account_emails={CONTROL_SOURCE: source}),
            window_start=start, window_end=horizon, own_accounts=own_accounts,
        )
        store.commit_plan(
            [asdict(item) for item in synchronized.resources],
            [dict(asdict(item), stable_id=item.stable_id) for item in plan.reminders],
            now=moment, horizon=horizon, observed_at=observed_at,
            calendar_count=synchronized.calendar_count, sync_summary=counts, policy_version=policy_version,
        )
    else:
        store.mark_sync(moment, synchronized.calendar_count, counts, observed_at=observed_at)
    return counts


def _send_due(store, settings, *, clock):
    accepted = failed = uncertain = 0
    refresh_required = False
    if not delivery_allowed(_instant(clock())):
        return {"accepted": 0, "failed": 0, "uncertain": 0, "refresh_required": False}
    if not store.summary(_instant(clock()))["sync_fresh_for_delivery"]:
        return {"accepted": 0, "failed": 0, "uncertain": 0, "refresh_required": True}
    for reminder in store.due(_instant(clock()), limit=MAX_BATCH):
        identifier = reminder["stable_id"]
        claimed = False

        def before_data():
            nonlocal claimed
            # submit_message invokes this after authentication and envelope
            # validation. Claim uses the actual time, not the cycle start.
            if not store.claim(identifier, now=_instant(clock())):
                raise _ClaimSkipped()
            claimed = True

        try:
            submit_control_email(reminder, settings, generated_at=_instant(clock()), before_data=before_data)
            if not claimed:
                raise SMTPDeliveryUncertain("Не подтверждена защищённая запись отправки письма участнику.")
        except _ClaimSkipped:
            refresh_required = True
            break
        except SMTPDeliveryUncertain:
            store.complete(identifier, "uncertain", now=_instant(clock()))
            uncertain += 1
            break
        except SMTPDeliveryError as error:
            store.complete(identifier, "failed", now=_instant(clock()), retryable=error.retryable)
            failed += 1
            break
        except ControlEmailQueueError:
            raise
        except Exception:
            store.complete(identifier, "uncertain" if claimed else "failed",
                           now=_instant(clock()), retryable=not claimed)
            raise ControlEmailCycleError("Отправка писем участникам остановлена; проверьте control-email-status.") from None
        try:
            store.complete(identifier, "accepted", now=_instant(clock()))
        except Exception:
            raise ControlEmailCycleError("SMTP принял письмо участнику, но квитанция не сохранена; повтор запрещён.") from None
        accepted += 1
    return {"accepted": accepted, "failed": failed, "uncertain": uncertain, "refresh_required": refresh_required}


def _cycle(directory, *, dispatch, clock):
    directory = Path(directory)
    started = _instant(clock())
    source, own_accounts = control_email_accounts()
    settings = MailSettings.from_environment() if dispatch else None
    gate_directory = Path(os.environ.get("DIGEST_STATE_DIR") or directory)
    with calendar_operation_lock(gate_directory), control_email_store(directory, now=started) as store:
        if dispatch:
            store.activate(started)
        changes = _refresh(store, source=source, own_accounts=own_accounts, clock=clock)
        outcome = _send_due(store, settings, clock=clock) if dispatch else {
            "accepted": 0, "failed": 0, "uncertain": 0, "refresh_required": False,
        }
        return {"mode": "automatic" if dispatch else "prepare", "sent": outcome["accepted"] > 0,
                **outcome, "sync": changes, "queue": store.summary(_instant(clock()))}


def prepare_control_email(directory: Path, *, clock=_now) -> dict:
    """Warm only this cache/queue without SMTP settings, activation or sending."""
    return _cycle(directory, dispatch=False, clock=clock)


def run_control_email_cycle(directory: Path, *, clock=_now) -> dict:
    """The participant-email flag is independent of email digest and Pushover."""
    if not control_email_enabled():
        raise ControlEmailCycleError("Письма участникам выключены: CONTROL_EMAIL_ENABLED=false.")
    return _cycle(directory, dispatch=True, clock=clock)


def automatic_control_email_status(directory: Path, *, now=None) -> dict:
    """Return only safe queue counts and independent worker heartbeat."""
    from .control_email_scheduler import control_email_scheduler_status

    moment = _instant(now or _now())
    try:
        with control_email_store(directory, now=moment) as store:
            queue = store.summary(moment)
    except ControlEmailQueueError as error:
        if not error.retryable:
            raise
        queue = {"busy": True}
    return {"enabled": control_email_enabled(), **control_email_scheduler_status(directory, now=moment),
            "queue": queue}
