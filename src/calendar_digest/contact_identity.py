"""Stable iCloud address-book identity independent of DAV server aliases.

Only the validated, decoded path identifies an iCloud book: it retains account
and collection scope while a redirect may change the serving hostname. Opaque
IDs are retained for already normalized records and non-URL caller fixtures.
This module must stay lightweight for the queue/scheduler processes.
"""

import hashlib
import re
from urllib.parse import unquote, urlsplit


_PREFIX = "icloud-carddav-book-v1:"
_HOST = re.compile(r"(?:contacts|p[0-9]+-contacts)\.icloud\.com")


def canonical_contact_book_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Некорректный идентификатор адресной книги iCloud.")
    if value.startswith(_PREFIX):
        if re.fullmatch(re.escape(_PREFIX) + r"[0-9a-f]{64}", value) is None:
            raise ValueError("Некорректный идентификатор адресной книги iCloud.")
        return value
    if "://" not in value:
        return value
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not _HOST.fullmatch(url.hostname or "")
                or url.port not in (None, 443) or url.username is not None
                or url.password is not None or url.query or url.fragment
                or re.search(r"[\x00-\x20\x7f\\]", value)
                or re.search(r"%(?![0-9a-fA-F]{2})", url.path)):
            raise ValueError
        path = unquote(url.path, errors="strict")
        if path.count("/") != url.path.count("/"):
            raise ValueError
        checked = path
        for _ in range(4):
            if (re.search(r"[\x00-\x1f\x7f\\]", checked)
                    or any(part in {".", ".."} for part in checked.split("/"))):
                raise ValueError
            again = unquote(checked, errors="strict")
            if again == checked:
                break
            if again.count("/") != checked.count("/"):
                raise ValueError
            checked = again
        else:
            raise ValueError
        path = path.rstrip("/")
        if not path or not path.startswith("/"):
            raise ValueError
        return _PREFIX + hashlib.sha256(path.encode("utf-8")).hexdigest()
    except (ValueError, UnicodeError):
        raise ValueError("Некорректный идентификатор адресной книги iCloud.") from None
