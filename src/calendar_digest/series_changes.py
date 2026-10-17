"""Read saved series notices independently of actionable event records."""

from dataclasses import fields
from datetime import date, datetime

from .selection import SeriesChangeNotice, SeriesInterval


def _interval(start, end, all_day):
    if type(all_day) is not bool or not isinstance(start, str) or (end is not None and not isinstance(end, str)):
        raise ValueError("Некорректный интервал изменения серии.")
    if all_day:
        begin = date.fromisoformat(start)
        finish = date.fromisoformat(end) if end is not None else begin
    else:
        begin = datetime.fromisoformat(start)
        finish = datetime.fromisoformat(end) if end is not None else begin
        if begin.tzinfo is None or finish.tzinfo is None:
            raise ValueError("Для времени изменения серии требуется часовой пояс.")
    if finish < begin:
        raise ValueError("Некорректный порядок времени изменения серии.")


def _parse_series_changes(values) -> list[SeriesChangeNotice]:
    """Validate nested saved data before any action links or email are created."""
    if not isinstance(values, list):
        raise ValueError("Сохранённые изменения серий должны быть списком.")
    notice_fields = {field.name for field in fields(SeriesChangeNotice)}
    interval_fields = {field.name for field in fields(SeriesInterval)}
    result = []
    identities = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != notice_fields:
            raise ValueError("Некорректная сохранённая заметка об изменении серии.")
        item = dict(value)
        for name in ("source_id", "calendar_id", "calendar_name", "uid", "title"):
            if not isinstance(item[name], str):
                raise ValueError("Некорректные данные изменения серии.")
        identity = item["source_id"], item["calendar_id"], item["uid"]
        if not all(identity) or identity in identities:
            raise ValueError("Неоднозначная заметка об изменении серии.")
        identities.add(identity)
        for side in ("old", "new"):
            if type(item[f"{side}_recurring"]) is not bool:
                raise ValueError("Некорректный признак повторения серии.")
            start, end = item[f"{side}_anchor_start"], item[f"{side}_anchor_end"]
            _interval(start, end, isinstance(start, str) and "T" not in start)
            intervals = item[f"{side}_intervals"]
            if not isinstance(intervals, list):
                raise ValueError("Некорректные интервалы изменения серии.")
            parsed = []
            for interval in intervals:
                if not isinstance(interval, dict) or set(interval) != interval_fields:
                    raise ValueError("Некорректный сохранённый интервал серии.")
                _interval(interval["start"], interval["end"], interval["all_day"])
                parsed.append(SeriesInterval(**interval))
            item[f"{side}_intervals"] = parsed
        result.append(SeriesChangeNotice(**item))
    return result


def parse_series_changes(values) -> list[SeriesChangeNotice]:
    try:
        return _parse_series_changes(values)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ValueError("Не удалось прочитать сохранённые изменения повторяющихся событий.") from None
