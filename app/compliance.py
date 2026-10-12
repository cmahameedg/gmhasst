"""Registrations, period generation and the filing status flow (maker -> checker -> closure)."""
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from . import periods as p
from . import validators as v
from .activity import log_event
from .models import (
    Client,
    Filing,
    FilingComment,
    FilingHistory,
    FilingMention,
    Registration,
    ReturnType,
    User,
    db,
)
from .permissions import ARTICLE, MANAGER, PARTNER, STAFF, is_assigned, visible_clients
from .returns import OPEN_STATUSES, compute_due
from .timeutil import today

# --- Registrations -----------------------------------------------------------

# kind -> (label, identifier label, schemes)
KINDS = {
    "GST": ("GST registration", "GSTIN", ["Monthly", "QRMP", "Composition"]),
    "TAN": ("TDS (TAN)", "TAN", []),
    "IT": ("Income tax", "PAN", ["Non-audit", "Audit"]),
    "COMPANY": ("Company (MCA)", "CIN", []),
    "LLP": ("LLP (MCA)", "LLPIN", []),
    "DIN": ("Director (DIN)", "DIN", []),
}


def matching_return_types(kind, scheme):
    """Active return types that apply to a registration of this kind/scheme."""
    tag = f"{kind}:{scheme}" if scheme else kind
    return [rt for rt in db.session.execute(
        db.select(ReturnType).where(ReturnType.active.is_(True)).order_by(ReturnType.sort_order)
    ).scalars() if tag in rt.applies_list or kind in rt.applies_list]


def validate_registration(client, kind, identifier, scheme, active_from, active_to, registration=None):
    """Returns (clean dict, errors list)."""
    errors = []
    if kind not in KINDS:
        return {}, ["Choose a registration type"]
    schemes = KINDS[kind][2]
    scheme = scheme if schemes else None
    if schemes and scheme not in schemes:
        errors.append(f"Choose one of: {', '.join(schemes)}")
    identifier = v.clean_code(identifier)
    if kind == "GST":
        err = v.gstin_error(identifier, client.pan)
        if err:
            errors.append(err)
        elif identifier not in {g.gstin for g in client.gstins}:
            errors.append(f"GSTIN {identifier} is not on this client's master; add it there first")
    elif kind == "TAN" and not v.is_valid_tan(identifier):
        errors.append("TAN must be in the format AAAA99999A")
    elif kind == "IT" and identifier != client.pan:
        errors.append("Income tax registration must use the client's PAN")
    elif kind == "COMPANY" and not v.CIN_RE.match(identifier):
        errors.append("CIN must be 21 characters")
    elif kind == "LLP" and not v.LLPIN_RE.match(identifier):
        errors.append("LLPIN must be in the format AAA-9999")
    elif kind == "DIN" and not re.fullmatch(r"[0-9]{8}", identifier):
        errors.append("DIN must be 8 digits")
    start, end = v.parse_date(active_from or ""), v.parse_date(active_to or "")
    if active_from and start is None or active_to and end is None:
        errors.append("Dates must be DD-MM-YYYY")
    if start and end and end < start:
        errors.append("Active-to date is before active-from date")
    query = db.select(Registration).where(
        Registration.client_id == client.id, Registration.kind == kind, Registration.identifier == identifier,
        Registration.active_to.is_(None),
    )
    if registration is not None:
        query = query.where(Registration.id != registration.id)
    if end is None and db.session.execute(query).first():
        errors.append(f"An open {KINDS[kind][1]} registration {identifier} already exists; end it first")
    return {"kind": kind, "identifier": identifier, "scheme": scheme, "active_from": start, "active_to": end}, errors


def registrations_from_master(client):
    """Registrations implied by the Phase 1 client master (GSTINs, TAN, services)."""
    services = set(client.services)
    plans = []
    gst_scheme = "Monthly" if "GST Monthly" in services else "QRMP" if "GST QRMP" in services else None
    if gst_scheme:
        plans += [("GST", g.gstin, gst_scheme) for g in client.gstins]
    if client.tan:
        plans.append(("TAN", client.tan, None))
    if "Income Tax" in services:
        plans.append(("IT", client.pan, "Audit" if "Tax Audit" in services else "Non-audit"))
    if "ROC" in services and client.cin_llpin:
        if client.entity_type in ("Private Limited", "Public Limited"):
            plans.append(("COMPANY", client.cin_llpin, None))
        elif client.entity_type == "LLP":
            plans.append(("LLP", client.cin_llpin, None))
    existing = {(r.kind, r.identifier) for r in client.registrations}
    created = []
    for kind, identifier, scheme in plans:
        if (kind, identifier) in existing:
            continue
        defaults = [rt for rt in matching_return_types(kind, scheme) if rt.default_on]
        reg = Registration(client_id=client.id, kind=kind, identifier=identifier, scheme=scheme, return_types=defaults)
        db.session.add(reg)
        created.append(reg)
    return created


# --- Generation --------------------------------------------------------------

def generate_period(year, month, actor):
    """Create one filing per active registration x linked return due for this month.

    Safe to run twice: filings that already exist are left alone.
    """
    key = p.month_key(year, month)
    month_start, month_end = p._span(year, month, 1)
    registrations = db.session.execute(
        db.select(Registration)
        .join(Client)
        .where(
            Client.status == "Active",
            (Registration.active_from.is_(None)) | (Registration.active_from <= month_end),
            (Registration.active_to.is_(None)) | (Registration.active_to >= month_start),
        )
    ).scalars().all()
    existing = set(db.session.execute(
        db.select(Filing.registration_id, Filing.return_type_id, Filing.period_end)
    ).all())
    created, by_return = 0, Counter()
    for reg in registrations:
        for rt in reg.return_types:
            if not rt.active:
                continue
            period = p.period_for(rt.frequency, year, month)
            if period is None or (reg.id, rt.id, period.end) in existing:
                continue
            client = reg.client
            filing = Filing(
                client_id=client.id, registration_id=reg.id, return_type_id=rt.id, gen_month=key,
                period_start=period.start, period_end=period.end, period_label=period.label,
                period_code=period.code, due_date=compute_due(rt, period.end, period.code),
                status="not_started", maker_id=client.primary_staff_id, reviewer_id=client.reviewer_staff_id,
            )
            filing.history.append(FilingHistory(user_id=actor.id, to_status="not_started",
                                                note=f"Generated for {p.month_label(key)}"))
            db.session.add(filing)
            created += 1
            by_return[rt.name] += 1
    log_event(actor, "filings.generate",
              summary=f"Generated {p.month_label(key)}: {created} filing(s)"
              + (" (" + ", ".join(f"{n} {k}" for k, n in sorted(by_return.items())) + ")" if by_return else ""))
    return created, by_return


# --- Status flow -------------------------------------------------------------

STATUSES = {
    "not_started": "Not started",
    "data_awaited": "Data awaited",
    "data_received": "Data received",
    "in_preparation": "In preparation",
    "ready_for_review": "Ready for review",
    "review_queries": "Review queries",
    "approved": "Approved",
    "filed": "Filed",
    "closed": "Closed",
    "nil_na": "Nil / Not applicable",
}
DONE_STATUSES = ("filed", "closed", "nil_na")

# from -> {to: who may make the move}
TRANSITIONS = {
    "not_started": {"data_awaited": "maker", "data_received": "maker", "in_preparation": "maker", "nil_na": "filer"},
    "data_awaited": {"data_received": "maker", "in_preparation": "maker", "nil_na": "filer"},
    "data_received": {"data_awaited": "maker", "in_preparation": "maker", "nil_na": "filer"},
    "in_preparation": {"data_awaited": "maker", "ready_for_review": "maker", "nil_na": "filer"},
    "ready_for_review": {"approved": "reviewer", "review_queries": "reviewer"},
    "review_queries": {"in_preparation": "maker", "ready_for_review": "maker"},
    # Filed is reachable only from Approved: there is no other path to it.
    "approved": {"filed": "filer", "review_queries": "reviewer"},
    "filed": {"closed": "reviewer"},
    "closed": {},
    "nil_na": {"not_started": "reviewer"},
}


def is_overdue(filing, on=None):
    return (filing.status in OPEN_STATUSES and filing.due_date is not None
            and filing.due_date < (on or today()))


def colour(filing, on=None):
    """Board colour: red overdue, grey not started, blue ready for review, green done, amber otherwise."""
    if is_overdue(filing, on):
        return "red"
    if filing.status == "not_started":
        return "grey"
    if filing.status == "ready_for_review":
        return "blue"
    if filing.status in ("filed", "closed"):
        return "green"
    if filing.status == "nil_na":
        return "nil"
    return "amber"


def can_see_filing(user, filing):
    return db.session.execute(
        visible_clients(user).where(Client.id == filing.client_id)
    ).scalar_one_or_none() is not None


def _is_maker(user, filing):
    if user.role in (PARTNER, MANAGER):
        return True
    return user.role in (STAFF, ARTICLE) and (filing.maker_id == user.id or is_assigned(user, filing.client))


def _is_filer(user, filing):
    return user.role in (PARTNER, MANAGER) or (user.role == STAFF and _is_maker(user, filing))


def _is_reviewer(user, filing):
    return user.role in (PARTNER, MANAGER) or (user.role == STAFF and filing.reviewer_id == user.id)


ACTOR_CHECKS = {"maker": _is_maker, "filer": _is_filer, "reviewer": _is_reviewer}


def can_supervise(user):
    return user.role in (PARTNER, MANAGER)


def submitted_by(filing):
    """Who last sent the filing for review."""
    for h in reversed(filing.history):
        if h.to_status == "ready_for_review":
            return h.user_id
    return None


def allowed_transitions(user, filing):
    allowed = []
    for to, actor in TRANSITIONS.get(filing.status, {}).items():
        if not ACTOR_CHECKS[actor](user, filing):
            continue
        # Checker must be a different person from the maker (a Partner may do both).
        if to == "approved" and user.role != PARTNER and submitted_by(filing) == user.id:
            continue
        allowed.append(to)
    return allowed


def parse_rupees(text):
    """'1,23,456.50' -> 12345650 paise; None if blank; raises ValueError if invalid."""
    text = (text or "").replace(",", "").replace("₹", "").strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(text)
    if amount < 0 or amount.as_tuple().exponent < -2:
        raise ValueError(text)
    return int(amount * 100)


def format_inr(paise):
    """12345650 -> '₹1,23,456.50' (Indian digit grouping)."""
    if paise is None:
        return ""
    rupees, fraction = divmod(abs(paise), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, groups = digits[:-3], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + digits[-3:]
    return f"{'-' if paise < 0 else ''}₹{digits}.{fraction:02d}"


def _money_field(form, name, label, errors, required):
    try:
        value = parse_rupees(form.get(name))
    except ValueError:
        errors.append(f"{label} must be an amount in rupees, e.g. 12,500.00")
        return None
    if value is None and required:
        errors.append(f"{label} is required")
    return value


def apply_transition(filing, to, user, form):
    """Move a filing to ``to``. Returns a list of errors (empty on success). Caller commits."""
    if to not in allowed_transitions(user, filing):
        return [f"You cannot move this filing from {STATUSES[filing.status]} to {STATUSES.get(to, to)}"]
    errors, note = [], (form.get("note") or "").strip()
    changes = [("filing_status", STATUSES[filing.status], STATUSES[to])]
    rt = filing.return_type

    if to == "review_queries" and not note:
        errors.append("Write the review queries so the maker knows what to fix")

    if to == "filed":
        arn = (form.get("arn") or "").strip().upper()
        filed_on = v.parse_date(form.get("filed_on") or "")
        if not arn:
            errors.append("ARN / acknowledgement number is required")
        elif not re.fullmatch(r"[A-Z0-9][A-Z0-9/\-]{4,59}", arn):
            errors.append("ARN should be letters and digits only")
        if filed_on is None:
            errors.append("Filing date is required")
        elif filed_on > today():
            errors.append("Filing date cannot be in the future")
        elif filed_on < filing.period_start:
            errors.append("Filing date is before the period starts")
        tax = _money_field(form, "tax_paid", "Tax paid", errors, required=True)
        late = filed_on is not None and filing.due_date is not None and filed_on > filing.due_date
        late_fee = _money_field(form, "late_fee", "Late fee", errors, required=late)
        interest = _money_field(form, "interest", "Interest", errors, required=late)
        if not errors:
            filing.arn, filing.filed_on = arn, filed_on
            filing.tax_paid_paise = tax
            filing.late_fee_paise = late_fee or 0
            filing.interest_paise = interest or 0
            changes += [("arn", "", arn), ("filed_on", "", filed_on.strftime("%d-%m-%Y")),
                        ("tax_paid", "", format_inr(tax))]
            if late:
                changes += [("late_fee", "", format_inr(late_fee)), ("interest", "", format_inr(interest))]

    if to == "closed":
        if not form.get("ack_saved"):
            errors.append("Confirm the acknowledgement is saved")
        if rt.requires_everify and not form.get("everified"):
            errors.append("Confirm the return is e-verified")
        challan = (form.get("challan_ref") or filing.challan_ref or "").strip()
        if filing.tax_paid_paise and not challan:
            errors.append("Note the challan (CIN / BRN) for the tax paid")
        if not errors:
            filing.ack_saved = True
            filing.everified = bool(form.get("everified"))
            filing.challan_ref = challan or None
            if challan:
                changes.append(("challan_ref", "", challan))

    if to == "nil_na":
        kind = form.get("nil_kind")
        if kind not in ("Nil", "Not applicable"):
            errors.append("Choose Nil return filed or Not applicable")
        else:
            filing.nil_kind = kind
            arn = (form.get("arn") or "").strip().upper()
            if arn:
                filing.arn = arn
            changes.append(("nil_kind", "", kind))

    if to == "not_started" and filing.status == "nil_na":
        changes.append(("nil_kind", filing.nil_kind or "", ""))
        filing.nil_kind = None

    if errors:
        return errors

    old = filing.status
    filing.status = to
    filing.history.append(FilingHistory(user_id=user.id, from_status=old, to_status=to, note=note or None))
    if note and to == "review_queries":
        add_comment(filing, user, f"Review queries: {note}")
    log_event(user, "filing.status", client=filing.client,
              summary=f"{rt.name} {filing.period_label}: {STATUSES[old]} → {STATUSES[to]}", changes=changes)
    return []


# --- Comments and @mentions -------------------------------------------------

MENTION_RE = re.compile(r"@([A-Za-z0-9._-]+)")


def handle_of(user):
    return user.email.split("@", 1)[0].lower()


def add_comment(filing, user, body):
    comment = FilingComment(user_id=user.id, body=body)
    filing.comments.append(comment)
    handles = {h.lower().rstrip(".") for h in MENTION_RE.findall(body)}
    if handles:
        for other in db.session.execute(db.select(User).where(User.active.is_(True))).scalars():
            if handle_of(other) in handles and other.id != user.id:
                comment.mentions.append(FilingMention(user_id=other.id))
    return comment


# --- Queries -----------------------------------------------------------------

def visible_filings(user):
    visible_ids = visible_clients(user).with_only_columns(Client.id)
    return db.select(Filing).where(Filing.client_id.in_(visible_ids))


def board_filings(user, key):
    """This month's filings plus everything still open from earlier months (carried forward)."""
    return db.session.execute(
        visible_filings(user).where(
            (Filing.gen_month == key) | ((Filing.gen_month < key) & Filing.status.in_(OPEN_STATUSES))
        )
    ).scalars().all()
