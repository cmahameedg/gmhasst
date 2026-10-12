"""Return Master: return types, due-date rules, overrides and one-off extensions."""
import re

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import or_

from . import periods as p
from .activity import log_event
from .models import DueExtension, DueOverride, Filing, ReturnType, db
from .permissions import PARTNER
from .validators import parse_date

bp = Blueprint("returns", __name__, url_prefix="/returns")

CATEGORIES = ["GST", "TDS", "Income Tax", "ROC", "LLP"]
OPEN_STATUSES = (
    "not_started", "data_awaited", "data_received", "in_preparation",
    "ready_for_review", "review_queries", "approved",
)

GST_PORTAL = "https://services.gst.gov.in/services/login"
IT_PORTAL = "https://eportal.incometax.gov.in/iec/foservices/#/login"
TRACES = "https://www.tdscpc.gov.in/app/login.xhtml"
MCA_PORTAL = "https://www.mca.gov.in/content/mca/global/en/home.html"

# Starting values from the Phase 2 brief. They are loaded once into the database;
# from then on the Return Master screen is the only source of truth.
# (code, name, old_name, category, frequency, day, offset, applies_to, default_on, portal, extra)
SEED = [
    ("GSTR1", "GSTR-1", None, "GST", p.MONTHLY, 11, 1, "GST:Monthly", True, GST_PORTAL, {}),
    ("IFF", "IFF", None, "GST", p.MONTHLY_Q12, 13, 1, "GST:QRMP", False, GST_PORTAL,
     {"notes": "Optional for QRMP taxpayers"}),
    ("GSTR1Q", "GSTR-1 (QRMP)", None, "GST", p.QUARTERLY, 13, 1, "GST:QRMP", True, GST_PORTAL, {}),
    ("GSTR3B", "GSTR-3B", None, "GST", p.MONTHLY, 20, 1, "GST:Monthly", True, GST_PORTAL, {}),
    ("PMT06", "PMT-06", None, "GST", p.MONTHLY_Q12, 25, 1, "GST:QRMP", True, GST_PORTAL, {}),
    ("GSTR3BQ", "GSTR-3B (QRMP)", None, "GST", p.QUARTERLY, 22, 1, "GST:QRMP", True, GST_PORTAL,
     {"notes": "22nd is the Tamil Nadu group date; states in the 24th group need their own row"}),
    ("CMP08", "CMP-08", None, "GST", p.QUARTERLY, 18, 1, "GST:Composition", True, GST_PORTAL, {}),
    ("GSTR4", "GSTR-4", None, "GST", p.ANNUAL, 30, 1, "GST:Composition", True, GST_PORTAL, {}),
    ("GSTR9", "GSTR-9 / 9C", None, "GST", p.ANNUAL, 31, 9, "GST:Monthly,GST:QRMP", False, GST_PORTAL,
     {"notes": "Only for clients above the turnover thresholds"}),
    ("TDSPAY", "TDS payment", "Challan 281", "TDS", p.MONTHLY, 7, 1, "TAN", True, IT_PORTAL,
     {"overrides": [("M03", 30, 1)]}),
    ("TDS24Q", "TDS return (salary)", "Form 24Q", "TDS", p.QUARTERLY, 31, 1, "TAN", True, TRACES,
     {"overrides": [("Q4", 31, 2)]}),
    ("TDS26Q", "TDS return (non-salary)", "Form 26Q", "TDS", p.QUARTERLY, 31, 1, "TAN", True, TRACES,
     {"overrides": [("Q4", 31, 2)]}),
    ("ADVTAX", "Advance tax", "Challan 280", "Income Tax", p.QUARTERLY, 15, 0, "IT:Non-audit,IT:Audit", True,
     IT_PORTAL, {}),
    ("TAR", "Tax audit report", "Form 3CA/3CB-3CD", "Income Tax", p.ANNUAL, 30, 6, "IT:Audit", True, IT_PORTAL, {}),
    ("ITR", "ITR (non-audit)", "ITR", "Income Tax", p.ANNUAL, 31, 4, "IT:Non-audit", True, IT_PORTAL,
     {"requires_everify": True}),
    ("ITRA", "ITR (audit)", "ITR", "Income Tax", p.ANNUAL, 31, 7, "IT:Audit", True, IT_PORTAL,
     {"requires_everify": True}),
    ("DIR3KYC", "DIR-3 KYC", None, "ROC", p.ANNUAL, 30, 6, "DIN", True, MCA_PORTAL, {}),
    ("DPT3", "DPT-3", None, "ROC", p.ANNUAL, 30, 3, "COMPANY", True, MCA_PORTAL, {}),
    ("MSME1", "MSME-1", None, "ROC", p.HALF_YEARLY, 31, 1, "COMPANY", False, MCA_PORTAL,
     {"notes": "Only for companies with dues to MSME suppliers"}),
    ("AOC4", "AOC-4", None, "ROC", p.ANNUAL, None, None, "COMPANY", True, MCA_PORTAL, {"event_days": 30}),
    ("MGT7", "MGT-7", None, "ROC", p.ANNUAL, None, None, "COMPANY", True, MCA_PORTAL, {"event_days": 60}),
    ("ADT1", "ADT-1", None, "ROC", p.ANNUAL, None, None, "COMPANY", True, MCA_PORTAL, {"event_days": 15}),
    ("LLP11", "LLP Form 11", None, "LLP", p.ANNUAL, 30, 2, "LLP", True, MCA_PORTAL, {}),
    ("LLP8", "LLP Form 8", None, "LLP", p.ANNUAL, 30, 7, "LLP", True, MCA_PORTAL, {}),
]

VERIFY_NOTE = "Starting value from the Phase 2 brief: verify against the current notification before go-live."


def seed_return_master():
    """Load the starting Return Master once, into an empty table."""
    if db.session.execute(db.select(ReturnType.id).limit(1)).first():
        return
    for order, (code, name, old, category, freq, day, offset, applies, default_on, portal, extra) in enumerate(SEED):
        rt = ReturnType(
            code=code, name=name, old_name=old, category=category, frequency=freq,
            due_day=day, due_month_offset=offset, applies_to=applies, default_on=default_on,
            portal_url=portal, sort_order=(order + 1) * 10,
            requires_everify=extra.get("requires_everify", False),
            notes="; ".join(n for n in (extra.get("notes"), VERIFY_NOTE) if n),
        )
        if "event_days" in extra:
            rt.due_basis, rt.event_label, rt.event_days = "event", "AGM date", extra["event_days"]
        for period_code, o_day, o_offset in extra.get("overrides", []):
            rt.overrides.append(DueOverride(period_code=period_code, due_day=o_day, due_month_offset=o_offset))
        db.session.add(rt)
    db.session.commit()


def compute_due(rt, period_end, period_code, event_date=None):
    """Due date for one period of a return type, from the Return Master only."""
    if rt.due_basis == "event":
        return p.add_days(event_date, rt.event_days) if event_date and rt.event_days is not None else None
    for ext in rt.extensions:
        if ext.period_end == period_end:
            return ext.due_date
    for ov in rt.overrides:
        if ov.period_code == period_code:
            return p.due_on(period_end, ov.due_day, ov.due_month_offset)
    return p.due_on(period_end, rt.due_day, rt.due_month_offset)


def recompute_open_filings(rt):
    """Apply the current rule to every open filing of this type. Returns how many changed."""
    changed = 0
    filings = db.session.execute(
        db.select(Filing).where(Filing.return_type_id == rt.id, Filing.status.in_(OPEN_STATUSES))
    ).scalars()
    for filing in filings:
        due = compute_due(rt, filing.period_end, filing.period_code, filing.event_date)
        if due != filing.due_date:
            filing.due_date = due
            changed += 1
    return changed


def describe(rt):
    if rt.due_basis == "event":
        return f"{rt.event_days} days after {rt.event_label or 'event'}"
    text = p.describe_rule(rt.due_day, rt.due_month_offset)
    labels = dict(p.override_codes(rt.frequency))
    for ov in rt.overrides:
        text += f"; {labels.get(ov.period_code, ov.period_code)}: {p.describe_rule(ov.due_day, ov.due_month_offset).lower()}"
    return text


def search_return_types(q):
    query = db.select(ReturnType).order_by(ReturnType.sort_order)
    if q:
        like = f"%{q}%"
        query = query.where(or_(ReturnType.name.ilike(like), ReturnType.old_name.ilike(like),
                                ReturnType.new_name.ilike(like), ReturnType.code.ilike(like)))
    return db.session.execute(query).scalars().all()


def _partner_only():
    if g.user.role != PARTNER:
        abort(403)


@bp.route("/")
def index():
    q = request.args.get("q", "").strip()
    return render_template("returns/index.html", return_types=search_return_types(q), q=q,
                           describe=describe, frequencies=p.FREQUENCIES)


SNAPSHOT_FIELDS = ["name", "old_name", "new_name", "category", "frequency", "due_basis", "due_day",
                   "due_month_offset", "event_label", "event_days", "applies_to", "default_on",
                   "requires_everify", "portal_url", "notes", "active", "sort_order"]


def _snapshot(rt):
    data = {f: "" if getattr(rt, f) is None else str(getattr(rt, f)) for f in SNAPSHOT_FIELDS}
    data["due_rule"] = describe(rt) if rt.frequency else ""
    return data


def _int(value, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _apply_form(rt, form, has_filings):
    errors = []
    rt.name = form.get("name", "").strip()
    rt.old_name = form.get("old_name", "").strip() or None
    rt.new_name = form.get("new_name", "").strip() or None
    rt.category = form.get("category", "")
    frequency = form.get("frequency", "")
    if has_filings and frequency != rt.frequency:
        errors.append("Frequency cannot change once filings exist; create a new return type instead")
    else:
        rt.frequency = frequency
    rt.due_basis = "event" if form.get("due_basis") == "event" else "period"
    rt.applies_to = ",".join(a.strip() for a in form.get("applies_to", "").split(",") if a.strip())
    rt.default_on = bool(form.get("default_on"))
    rt.requires_everify = bool(form.get("requires_everify"))
    rt.portal_url = form.get("portal_url", "").strip() or None
    rt.notes = form.get("notes", "").strip() or None
    rt.active = bool(form.get("active"))
    rt.sort_order = _int(form.get("sort_order"), 0, 10000) or 100

    if not rt.name:
        errors.append("Name is required")
    if rt.category not in CATEGORIES:
        errors.append("Choose a category")
    if rt.frequency not in p.FREQUENCIES:
        errors.append("Choose a frequency")
    if not rt.applies_to:
        errors.append("Applies to is required")
    if rt.portal_url and not rt.portal_url.startswith("https://"):
        errors.append("Portal link must start with https://")

    if rt.due_basis == "event":
        rt.event_label = form.get("event_label", "").strip() or "Event date"
        rt.event_days = _int(form.get("event_days"), 0, 1000)
        rt.due_day = rt.due_month_offset = None
        if rt.event_days is None:
            errors.append("Days after event must be a number")
    else:
        rt.due_day = _int(form.get("due_day"), 1, 31)
        rt.due_month_offset = _int(form.get("due_month_offset"), 0, 24)
        rt.event_label = rt.event_days = None
        if rt.due_day is None or rt.due_month_offset is None:
            errors.append("Due day must be 1-31 and months after period 0-24")

    valid_codes = {c for c, _ in p.override_codes(rt.frequency)} if rt.frequency in p.FREQUENCIES else set()
    overrides = []
    for code, day, offset in zip(form.getlist("ov_code"), form.getlist("ov_day"), form.getlist("ov_offset")):
        if not code:
            continue
        o_day, o_offset = _int(day, 1, 31), _int(offset, 0, 24)
        if code not in valid_codes or o_day is None or o_offset is None:
            errors.append(f"Override {code}: check the period, day and months")
        else:
            overrides.append((code, o_day, o_offset))
    if rt.due_basis == "period":
        rt.overrides = [DueOverride(period_code=c, due_day=d, due_month_offset=o) for c, d, o in overrides]
    else:
        rt.overrides = []
    return errors


def _edit(rt):
    creating = rt.id is None
    has_filings = not creating and db.session.execute(
        db.select(Filing.id).where(Filing.return_type_id == rt.id).limit(1)).first() is not None
    if request.method == "POST":
        before = {} if creating else _snapshot(rt)
        with db.session.no_autoflush:
            errors = _apply_form(rt, request.form, has_filings)
        if creating and not errors:
            base = re.sub(r"[^A-Z0-9]", "", rt.name.upper())[:30] or "RT"
            rt.code, n = base, 1
            while db.session.execute(db.select(ReturnType.id).where(ReturnType.code == rt.code)).first():
                n += 1
                rt.code = f"{base}{n}"
        if errors:
            db.session.rollback()
            for e in errors:
                flash(e, "error")
        else:
            if creating:
                db.session.add(rt)
            db.session.flush()
            changed = recompute_open_filings(rt)
            after = _snapshot(rt)
            changes = [(f"rm_{k}", before.get(k, ""), after[k]) for k in after if before.get(k, "") != after[k]]
            log_event(g.user, "returns.create" if creating else "returns.update",
                      summary=f"{'Added' if creating else 'Edited'} Return Master: {rt.name}"
                      + (f"; due date updated on {changed} open filing(s)" if changed else ""),
                      changes=changes)
            db.session.commit()
            flash(f"Saved {rt.name}." + (f" Due date updated on {changed} open filing(s)." if changed else ""),
                  "success")
            return redirect(url_for("returns.index"))
    return render_template("returns/edit.html", rt=rt, creating=creating, has_filings=has_filings,
                           categories=CATEGORIES, frequencies=p.FREQUENCIES,
                           override_codes={f: p.override_codes(f) for f in p.FREQUENCIES},
                           describe=describe)


@bp.route("/new", methods=["GET", "POST"])
def new():
    _partner_only()
    rt = ReturnType(frequency=p.MONTHLY, due_basis="period", due_day=20, due_month_offset=1,
                    active=True, default_on=True, category="GST", applies_to="")
    return _edit(rt)


@bp.route("/<int:rt_id>/edit", methods=["GET", "POST"])
def edit(rt_id):
    _partner_only()
    rt = db.session.get(ReturnType, rt_id) or abort(404)
    return _edit(rt)


@bp.route("/<int:rt_id>/extensions", methods=["POST"])
def add_extension(rt_id):
    """Record a notified extension for one period, and move the open filings for it."""
    _partner_only()
    rt = db.session.get(ReturnType, rt_id) or abort(404)
    period_month = request.form.get("period_month", "")
    due = parse_date(request.form.get("due_date", ""))
    try:
        year, month = p.parse_month_key(period_month)
    except ValueError:
        year = month = None
    period = p.period_for(rt.frequency, year, month) if year else None
    if period is None or due is None or rt.due_basis != "period":
        flash("Choose the month the period ends in (a valid period for this return) and a new due date.", "error")
        return redirect(url_for("returns.edit", rt_id=rt.id))
    existing = next((e for e in rt.extensions if e.period_end == period.end), None)
    old = existing.due_date.isoformat() if existing else compute_due(rt, period.end, period.code).isoformat()
    if existing:
        existing.due_date, existing.note = due, request.form.get("note", "").strip()[:200] or None
    else:
        rt.extensions.append(DueExtension(period_end=period.end, due_date=due,
                                          note=request.form.get("note", "").strip()[:200] or None))
    db.session.flush()
    changed = recompute_open_filings(rt)
    log_event(g.user, "returns.extension", summary=f"Extended {rt.name} {period.label}; {changed} open filing(s) moved",
              changes=[("due_date", old, due.isoformat())])
    db.session.commit()
    flash(f"{rt.name} {period.label} now due {due.strftime('%d-%m-%Y')}; {changed} open filing(s) updated.", "success")
    return redirect(url_for("returns.edit", rt_id=rt.id))


@bp.route("/<int:rt_id>/extensions/<int:ext_id>/delete", methods=["POST"])
def delete_extension(rt_id, ext_id):
    _partner_only()
    rt = db.session.get(ReturnType, rt_id) or abort(404)
    ext = next((e for e in rt.extensions if e.id == ext_id), None) or abort(404)
    rt.extensions.remove(ext)
    db.session.flush()
    changed = recompute_open_filings(rt)
    log_event(g.user, "returns.extension", summary=f"Removed extension on {rt.name}; {changed} open filing(s) moved",
              changes=[("due_date", ext.due_date.isoformat(), "")])
    db.session.commit()
    return redirect(url_for("returns.edit", rt_id=rt.id))
