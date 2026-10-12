"""Validating, saving and logging client changes.

The edit screen and the CSV importer both go through ``validate_client`` and
``save_client`` so the rules live in exactly one place.
"""
from . import validators as v
from .activity import log_event
from .constants import (
    CIN_ENTITY_TYPES,
    ENTITY_TYPES,
    FEE_BASES,
    ONBOARDING_SOURCES,
    SERVICES,
    STATES,
    STATUSES,
)
from .models import Client, ClientGroup, Counter, Gstin, User, db
from .permissions import ASSIGNABLE_ROLES, FIELDS

REQUIRED_FIELDS = {"name", "entity_type", "pan", "dob_incorporation", "onboarding_date", "status"}

CHOICES = {
    "entity_type": ENTITY_TYPES,
    "state": list(STATES),
    "onboarding_source": ONBOARDING_SOURCES,
    "fee_basis": FEE_BASES,
    "status": STATUSES,
}


def _match_choice(value, choices):
    """Case-insensitive match against a dropdown list; returns the canonical spelling."""
    lookup = {c.lower(): c for c in choices}
    return lookup.get(value.strip().lower())


def _display_date(d):
    return d.strftime("%d-%m-%Y") if d else ""


def snapshot(client, export=False):
    """Every field of a client as display text. Used for diffs, profile and export.

    With ``export=True`` staff are written as e-mail addresses and dates as ISO,
    so an exported file can be re-imported.
    """
    def staff(user):
        if not user:
            return ""
        return user.email if export else user.name

    def day(d):
        if not d:
            return ""
        return d.isoformat() if export else _display_date(d)

    return {
        "code": client.code or "",
        "name": client.name or "",
        "trade_name": client.trade_name or "",
        "entity_type": client.entity_type or "",
        "pan": client.pan or "",
        "gstins": "; ".join(g.gstin for g in client.gstins),
        "tan": client.tan or "",
        "cin_llpin": client.cin_llpin or "",
        "udyam": client.udyam or "",
        "iec": client.iec or "",
        "dob_incorporation": day(client.dob_incorporation),
        "contact_person": client.contact_person or "",
        "mobile": client.mobile or "",
        "email": client.email or "",
        "address": client.address or "",
        "district": client.district or "",
        "state": client.state or "",
        "group": client.group.name if client.group else "",
        "services": "; ".join(client.services) if client.id else "",
        "primary_staff": staff(client.primary_staff),
        "reviewer_staff": staff(client.reviewer_staff),
        "onboarding_source": client.onboarding_source or "",
        "onboarding_date": day(client.onboarding_date),
        "fee_basis": client.fee_basis or "",
        "status": client.status or "",
        "notes": client.notes or "",
    }


def _find_staff(value):
    """Resolve a staff reference (user id from a form, or e-mail/name from a CSV)."""
    value = str(value).strip()
    query = db.select(User).where(User.active.is_(True), User.role.in_(ASSIGNABLE_ROLES))
    if value.isdigit():
        return db.session.execute(query.where(User.id == int(value))).scalar_one_or_none()
    by_email = db.session.execute(query.where(db.func.lower(User.email) == value.lower())).scalar_one_or_none()
    if by_email:
        return by_email
    matches = db.session.execute(query.where(db.func.lower(User.name) == value.lower())).scalars().all()
    return matches[0] if len(matches) == 1 else None


def validate_client(raw, fields, client=None, show_codes=True):
    """Validate the ``fields`` of ``raw`` (form or CSV row) for a new or existing client.

    ``raw`` maps field key -> text (``services`` may also be a list).
    Returns ``(clean, errors)``: ``clean`` maps key -> Python value ready to save,
    ``errors`` maps key -> message. Keys outside ``fields`` are ignored entirely,
    which is what stops a user editing fields their role does not allow.
    """
    clean, errors = {}, {}
    creating = client is None

    def text(key):
        value = raw.get(key)
        if isinstance(value, list):
            value = "; ".join(value)
        return (value or "").strip()

    for key in fields:
        if key in ("code",):
            continue
        value = text(key)
        if not value:
            if key in REQUIRED_FIELDS:
                if key == "status" and creating:
                    clean[key] = "Active"
                else:
                    errors[key] = f"{FIELDS[key]} is required"
            else:
                clean[key] = [] if key in ("gstins", "services") else None
            continue

        if key in CHOICES:
            match = _match_choice(value, CHOICES[key])
            if match is None:
                errors[key] = f"{FIELDS[key]} '{value}' is not one of the allowed values"
            else:
                clean[key] = match
        elif key == "pan":
            pan = v.clean_code(value)
            if not v.is_valid_pan(pan):
                errors[key] = f"PAN {pan} is not in the format AAAAA9999A"
            else:
                clean[key] = pan
        elif key == "gstins":
            clean[key] = list(dict.fromkeys(v.clean_code(g) for g in v.split_list(value)))
        elif key == "tan":
            tan = v.clean_code(value)
            if not v.is_valid_tan(tan):
                errors[key] = f"TAN {tan} is not in the format AAAA99999A"
            else:
                clean[key] = tan
        elif key == "cin_llpin":
            clean[key] = v.clean_code(value)
        elif key == "udyam":
            udyam = v.clean_code(value)
            if not v.is_valid_udyam(udyam):
                errors[key] = f"Udyam number {udyam} is not in the format UDYAM-XX-00-0000000"
            else:
                clean[key] = udyam
        elif key == "iec":
            iec = v.clean_code(value)
            if not v.is_valid_iec(iec):
                errors[key] = f"IEC {iec} must be 10 letters or digits"
            else:
                clean[key] = iec
        elif key in ("dob_incorporation", "onboarding_date"):
            d = v.parse_date(value)
            if d is None:
                errors[key] = f"{FIELDS[key]} '{value}' is not a date (use DD-MM-YYYY)"
            elif v.is_future(d):
                errors[key] = f"{FIELDS[key]} cannot be in the future"
            else:
                clean[key] = d
        elif key == "mobile":
            mobile = v.clean_mobile(value)
            if not v.is_valid_mobile(mobile):
                errors[key] = f"Mobile '{value}' must be 10 digits"
            else:
                clean[key] = mobile
        elif key == "email":
            email = value.lower()
            if not v.is_valid_email(email):
                errors[key] = f"Email '{value}' is not a valid address"
            else:
                clean[key] = email
        elif key == "services":
            items = raw.get(key)
            items = items if isinstance(items, list) else v.split_list(value)
            chosen, bad = [], []
            for item in items:
                match = _match_choice(item, SERVICES)
                (chosen if match else bad).append(match or item)
            if bad:
                errors[key] = "Unknown service: " + ", ".join(bad)
            else:
                clean[key] = sorted(set(chosen))
        elif key in ("primary_staff", "reviewer_staff"):
            user = _find_staff(value)
            if user is None:
                errors[key] = f"{FIELDS[key]} '{value}' is not an active staff member"
            else:
                clean[key] = user
        else:
            clean[key] = value

    # Cross-field rules use the new value if supplied, otherwise the stored one.
    def current(key):
        if key in clean:
            return clean[key]
        return getattr(client, key, None) if client else None

    pan = current("pan")
    if "pan" in clean and clean["pan"]:
        query = db.select(Client).where(Client.pan == clean["pan"])
        if client is not None:
            query = query.where(Client.id != client.id)
        other = db.session.execute(query).scalar_one_or_none()
        if other:
            where = f" ({other.code})" if show_codes else ""
            errors["pan"] = f"PAN {clean['pan']} already belongs to another client{where}"

    gst_list = clean.get("gstins")
    if gst_list is None and client is not None and "pan" in clean:
        gst_list = [g.gstin for g in client.gstins]  # PAN changed: recheck existing GSTINs
    if gst_list and pan and "pan" not in errors:
        problems = [msg for msg in (v.gstin_error(g, pan) for g in gst_list) if msg]
        if not problems and "gstins" in clean:
            query = db.select(Gstin).where(Gstin.gstin.in_(gst_list))
            if client is not None:
                query = query.where(Gstin.client_id != client.id)
            for taken in db.session.execute(query).scalars():
                where = f" ({taken.client.code})" if show_codes else ""
                problems.append(f"GSTIN {taken.gstin} already belongs to another client{where}")
        if problems:
            errors["gstins"] = "; ".join(problems)

    entity_type = current("entity_type")
    cin = current("cin_llpin")
    if cin and fields & {"cin_llpin", "entity_type"} and "entity_type" not in errors:
        if entity_type not in CIN_ENTITY_TYPES:
            errors["cin_llpin"] = "CIN / LLPIN applies only to companies and LLPs"
        elif entity_type == "LLP" and not v.LLPIN_RE.match(cin):
            errors["cin_llpin"] = f"LLPIN {cin} is not in the format AAA-9999"
        elif entity_type != "LLP" and not v.CIN_RE.match(cin):
            errors["cin_llpin"] = f"CIN {cin} is not a valid 21-character CIN"

    primary, reviewer = current("primary_staff"), current("reviewer_staff")
    if primary is not None and reviewer is not None and primary.id == reviewer.id:
        errors["reviewer_staff"] = "Reviewer must be a different person from the primary staff"

    return clean, errors


def _apply(client, clean):
    for key, value in clean.items():
        if key == "gstins":
            existing = {g.gstin: g for g in client.gstins}
            client.gstins = [existing.get(g) or Gstin(gstin=g) for g in value]
        elif key == "services":
            continue  # needs the client id; handled after flush
        elif key == "group":
            if value:
                group = db.session.execute(
                    db.select(ClientGroup).where(db.func.lower(ClientGroup.name) == value.lower())
                ).scalar_one_or_none()
                if group is None:
                    group = ClientGroup(name=value)
                    db.session.add(group)
                    db.session.flush()
                client.group = group
            else:
                client.group = None
        else:
            setattr(client, key, value)


def save_client(clean, actor, client=None, action=None, summary=None):
    """Create or update a client from validated data and log every changed field.

    The caller commits.
    """
    creating = client is None
    if creating:
        number = Counter.next("client_code")
        client = Client(code=f"GMH-{number:04d}")
        before = {key: "" for key in FIELDS}
    else:
        before = snapshot(client)

    with db.session.no_autoflush:
        _apply(client, clean)
    if creating:
        db.session.add(client)
    db.session.flush()
    if "services" in clean:
        client.set_services(clean["services"])
    db.session.flush()
    db.session.refresh(client)

    after = snapshot(client)
    changes = [(key, before[key], after[key]) for key in FIELDS if before[key] != after[key]]
    if changes:
        log_event(
            actor,
            action or ("client.create" if creating else "client.update"),
            client=client,
            summary=summary or ("Created client" if creating else "Edited client"),
            changes=changes,
        )
    return client, changes
