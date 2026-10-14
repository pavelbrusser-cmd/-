"""Conservative display categories and demo controls; no calendar operations."""

import re
import unicodedata

from .selection import DigestEvent


CATEGORY_LABELS = {
    "birthdays": "Дни рождения",
    "holidays": "Праздники",
    "meetings": "Встречи",
    "all_day": "На весь день",
    "events": "События",
}

_EXPLICIT_CATEGORIES = {
    "birthday": "birthdays", "birthdays": "birthdays",
    "день рождения": "birthdays", "дни рождения": "birthdays",
    "holiday": "holidays", "holidays": "holidays",
    "праздник": "holidays", "праздники": "holidays",
    "meeting": "meetings", "meetings": "meetings",
    "встреча": "meetings", "встречи": "meetings",
    "совещание": "meetings", "совещания": "meetings",
    "all_day": "all_day", "на весь день": "all_day",
    "event": "events", "events": "events", "событие": "events", "события": "events",
}

_HOLIDAY_TITLES = {
    "новый год", "рождество", "рождество христово", "пасха",
    "день победы", "день россии", "день народного единства",
    "международный женский день", "день защитника отечества",
    "праздник весны и труда", "день труда",
    "christmas", "christmas day", "easter", "new year's day",
    "new year’s day", "thanksgiving", "thanksgiving day",
}

_BIRTHDAY_TITLE = re.compile(r"^(?:день\s+рождения|birthday)\b|\b(?:'s|’s)\s+birthday\b")
_MEETING_TITLE = re.compile(
    r"^(?:встреча|совещание|созвон|переговоры|собеседование|meeting|interview|call)\b"
)
_HOLIDAY_PREFIX = re.compile(r"^(?:праздник|holiday)\s*:")


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").split())


def _metadata_categories(event: DigestEvent) -> set[str]:
    categories = set()
    # DigestEvent currently has no classification fields. Recognize explicit
    # metadata if a future enriched object supplies these optional attributes.
    for name in ("category", "categories"):
        values = getattr(event, name, ())
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple, set, frozenset)):
            continue
        for value in values:
            if isinstance(value, str):
                category = _EXPLICIT_CATEGORIES.get(_normalized(value))
                if category:
                    categories.add(category)
    return categories


def classify_event(event: DigestEvent) -> str:
    """Choose a display label; missing or conflicting semantic clues stay neutral.

    All-day is a timing label, never evidence that an event is a birthday or
    holiday. Title matching deliberately uses explicit phrases, not names.
    """
    fallback = "all_day" if event.all_day else "events"
    metadata = _metadata_categories(event)
    if metadata:
        return next(iter(metadata)) if len(metadata) == 1 else fallback

    title = _normalized(event.title or "")
    categories = set()
    if _BIRTHDAY_TITLE.search(title):
        categories.add("birthdays")
    if title in _HOLIDAY_TITLES or _HOLIDAY_PREFIX.search(title):
        categories.add("holidays")
    if _MEETING_TITLE.search(title):
        categories.add("meetings")

    # A calendar explicitly named for a category can provide a useful clue.
    # Generic or mixed calendar names do not classify their contents.
    calendar_category = _EXPLICIT_CATEGORIES.get(_normalized(event.calendar_name or ""))
    if calendar_category in {"birthdays", "holidays", "meetings"}:
        categories.add(calendar_category)
    return next(iter(categories)) if len(categories) == 1 else fallback


def actions_for(event: DigestEvent) -> tuple[str, ...]:
    """Return demo button codes, without implying organizer or write permissions.

    Calendar STATUS and the account's ATTENDEE PARTSTAT are separate signals.
    Unknown participation alone does not mean an invitation awaits acceptance.
    """
    status = (event.event_status or "UNSPECIFIED").strip().upper()
    participation = (event.participation or "UNKNOWN").strip().upper()
    if status == "CANCELLED":
        return ()
    if participation == "ACCEPTED":
        return ("reschedule", "cancel")
    if participation in {"TENTATIVE", "NEEDS-ACTION", "DECLINED"}:
        return ("confirm",)
    if participation == "UNKNOWN":
        if status == "CONFIRMED":
            return ("reschedule", "cancel")
        if status == "TENTATIVE":
            return ("confirm",)
    return ()
