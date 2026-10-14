"""Calendar diagnostics, previews and explicit email delivery commands."""

import argparse
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
import json
import logging
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from .icloud import Credentials, discover_calendars, probe_calendars
from .mailru import (
    MailRuCredentials,
    discover_calendars as discover_mailru,
    probe_calendars as probe_mailru,
)
from .readonly_caldav import CalendarConnectionError
from .local_state import StateError
from .preview import PreviewError, PreviewStage, create_preview, render_saved_preview
from .selection import SelectionError
from .action_links import issue_email
from .action_store import StoreError
from .calendar_operation_lock import CalendarLockError
from .delivery import DeliveryError, delivery_status, prepare_delivery, submit_delivery
from .smtp_delivery import MailSettings, SMTPDeliveryError, probe_smtp
from .runtime import RuntimeConfigurationError, schedule_enabled


def _preview_progress(stage: PreviewStage) -> None:
    if isinstance(stage, PreviewStage):
        print(stage.value, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Подключение календарей для дайджеста")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe-smtp", help="Проверить вход на почтовый сервер без отправки письма")
    commands.add_parser("discover-icloud", help="Показать доступные календари iCloud")
    mailru = commands.add_parser("discover-mailru", help="Показать доступные календари Mail.ru")
    mailru.add_argument("--account", type=int, choices=(1, 2, 3), default=1,
                        help="Номер учётной записи Mail.ru (по умолчанию 1)")
    probe = commands.add_parser("probe-icloud", help="Проверить чтение событий без вывода их содержимого")
    probe.add_argument("--days", type=int, default=30, help="Число дней начиная с завтра (1–31)")
    probe_mail = commands.add_parser("probe-mailru", help="Проверить чтение событий Mail.ru")
    probe_mail.add_argument("--account", type=int, choices=(1, 2, 3), default=1,
                           help="Номер учётной записи Mail.ru (по умолчанию 1)")
    probe_mail.add_argument("--days", type=int, default=30, help="Число дней начиная с завтра (1–31)")
    preview = commands.add_parser("preview", help="Собрать локальный образец дайджеста без отправки письма")
    preview.add_argument("--date", type=date.fromisoformat, metavar="YYYY-MM-DD",
                         help="Дата по Москве (по умолчанию завтра)")
    render = commands.add_parser("render-preview", help="Оформить сохранённый образец без чтения календарей")
    render.add_argument("--date", type=date.fromisoformat, metavar="YYYY-MM-DD", required=True)
    actions = commands.add_parser("issue-actions", help="Создать письмо со ссылками на размещённый сервис действий")
    actions.add_argument("--date", type=date.fromisoformat, metavar="YYYY-MM-DD", required=True)
    prepare = commands.add_parser("prepare-delivery", help="Подготовить полное письмо для доставки без отправки")
    submit = commands.add_parser("send-ready", help="Отправить ранее подготовленное письмо один раз")
    test = commands.add_parser("send-test", help="Собрать и отправить проверочное письмо получателю из настроек")
    status = commands.add_parser("delivery-status", help="Показать результаты доставки без содержимого писем")
    for command in (prepare, submit, test, status):
        command.add_argument("--date", type=date.fromisoformat, metavar="YYYY-MM-DD",
                             required=command in (prepare, submit))
    for command in (prepare, submit):
        command.add_argument("--kind", choices=("daily", "test"), default="daily")
    for command in (preview, render, actions, prepare, submit, test, status):
        command.add_argument("--directory", type=Path,
                             default=Path(os.environ.get("DIGEST_STATE_DIR", ".local")),
                             help="Приватная папка данных (по умолчанию DIGEST_STATE_DIR или .local)")
    args = parser.parse_args(argv)
    if args.command in {"probe-icloud", "probe-mailru"} and not 1 <= args.days <= 31:
        parser.error("--days должно быть в диапазоне от 1 до 31")

    # Library diagnostics can contain private DAV URLs or server response bodies.
    for name in ("caldav", "niquests", "urllib3", "urllib3_future"):
        logging.getLogger(name).addHandler(logging.NullHandler())
        logging.getLogger(name).propagate = False

    try:
        if args.command == "probe-smtp":
            result = probe_smtp(MailSettings.from_environment())
        elif args.command == "delivery-status":
            from .scheduler import scheduler_status

            try:
                result = delivery_status(args.directory, day=args.date)
            except DeliveryError as error:
                if not error.retryable:
                    raise
                result = {"deliveries": [], "busy": True}
            result["schedule"] = {
                "enabled": schedule_enabled(), **scheduler_status(args.directory),
            }
        elif args.command == "prepare-delivery":
            result = prepare_delivery(args.date, args.directory, kind=args.kind)
        elif args.command == "send-ready":
            result = submit_delivery(args.date, args.directory, kind=args.kind)
        elif args.command == "send-test":
            day = args.date or (datetime.now(ZoneInfo("Europe/Moscow")).date() + timedelta(days=1))
            print("Готовим проверочное письмо из актуальных календарей.", file=sys.stderr, flush=True)
            result = prepare_delivery(day, args.directory, kind="test")
            if result["state"] == "ready":
                print("Передаём проверочное письмо почтовому серверу.", file=sys.stderr, flush=True)
                result = submit_delivery(day, args.directory, kind="test")
        elif args.command == "issue-actions":
            result = issue_email(args.date, args.directory)
        elif args.command == "render-preview":
            result = render_saved_preview(args.date, args.directory)
        elif args.command == "preview":
            day = args.date or (datetime.now(ZoneInfo("Europe/Moscow")).date() + timedelta(days=1))
            result = create_preview(day, args.directory, progress=_preview_progress)
        elif args.command == "discover-mailru":
            credentials = MailRuCredentials.from_environment(args.account)
            calendars = discover_mailru(credentials)
            result = {"source": f"mailru_{args.account}", "calendars": [asdict(c) for c in calendars]}
        elif args.command == "discover-icloud":
            calendars = discover_calendars(Credentials.from_environment())
            result = {"source": "icloud", "calendars": [asdict(c) for c in calendars]}
        else:
            zone = ZoneInfo("Europe/Moscow")
            tomorrow = datetime.now(zone).date() + timedelta(days=1)
            start = datetime.combine(tomorrow, time.min, tzinfo=zone)
            end = start + timedelta(days=args.days)
            if args.command == "probe-mailru":
                credentials = MailRuCredentials.from_environment(args.account)
                calendars = probe_mailru(credentials, start=start, end=end)
                source = f"mailru_{args.account}"
            else:
                calendars = probe_calendars(Credentials.from_environment(), start=start, end=end)
                source = "icloud"
            result = {
                "source": source, "window_start": start.isoformat(),
                "window_end_exclusive": end.isoformat(),
                "calendars": [asdict(c) for c in calendars],
            }
    except (CalendarConnectionError, SelectionError, StateError, PreviewError, StoreError,
            CalendarLockError, DeliveryError, SMTPDeliveryError, RuntimeConfigurationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        # Do not expose raw exceptions containing request/response details.
        print("Не удалось обработать ответ календаря; требуется диагностика адаптера.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command in {"prepare-delivery", "send-ready", "send-test"}:
        return 0 if result.get("state") in {"ready", "accepted"} else 1
    return 0
