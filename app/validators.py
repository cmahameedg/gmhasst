"""Format checks for Indian tax identifiers and contact details.

Each normaliser takes raw user text and returns the cleaned value; the
matching ``is_valid_*`` function says whether the cleaned value is acceptable.
"""
import re
from datetime import date, datetime

from .constants import GSTIN_STATE_CODES

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
CIN_RE = re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
LLPIN_RE = re.compile(r"^[A-Z]{3}-[0-9]{4}$")
UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")
IEC_RE = re.compile(r"^[A-Z0-9]{10}$")
MOBILE_RE = re.compile(r"^[0-9]{10}$")
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")

_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y")


def clean_code(value):
    """Upper-case and drop all whitespace (PAN, GSTIN, TAN, CIN, ...)."""
    return re.sub(r"\s+", "", value or "").upper()


def is_valid_pan(pan):
    return bool(PAN_RE.match(pan))


def is_valid_tan(tan):
    return bool(TAN_RE.match(tan))


def gstin_check_char(first14):
    """Checksum character of a GSTIN (mod-36 weighted algorithm used by GSTN)."""
    total = 0
    for i, ch in enumerate(first14):
        product = _GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36]


def gstin_error(gstin, pan=None):
    """Return an error message for a GSTIN, or None if it is acceptable.

    ``pan`` is the client's PAN; when given, the PAN embedded in the GSTIN
    (characters 3-12) must match it.
    """
    if len(gstin) != 15:
        return f"GSTIN {gstin} must be 15 characters"
    if not GSTIN_RE.match(gstin):
        return f"GSTIN {gstin} is not in a valid format"
    if gstin[:2] not in GSTIN_STATE_CODES:
        return f"GSTIN {gstin} has unknown state code {gstin[:2]}"
    if gstin_check_char(gstin[:14]) != gstin[14]:
        return f"GSTIN {gstin} fails the checksum (likely a typo)"
    if pan and gstin[2:12] != pan:
        return f"GSTIN {gstin} contains PAN {gstin[2:12]}, not the client PAN {pan}"
    return None


def split_list(value):
    """Split a multi-value cell on ; | , or newlines."""
    return [part.strip() for part in re.split(r"[;|,\n]", value or "") if part.strip()]


def is_valid_cin_or_llpin(value):
    return bool(CIN_RE.match(value) or LLPIN_RE.match(value))


def is_valid_udyam(value):
    return bool(UDYAM_RE.match(value))


def is_valid_iec(value):
    return bool(IEC_RE.match(value))


def clean_mobile(value):
    digits = re.sub(r"[\s\-()]", "", value or "")
    if digits.startswith("+91"):
        digits = digits[3:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def is_valid_mobile(value):
    return bool(MOBILE_RE.match(value))


def is_valid_email(value):
    return bool(EMAIL_RE.match(value))


def parse_date(value):
    """Parse a date in ISO or Indian (DD-MM-YYYY / DD/MM/YYYY) form; None if invalid."""
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def is_future(d):
    return d > date.today()
