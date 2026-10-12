"""DSC register and registration deadlines (Udyam / IEC annual update, GST Rule 10A bank account)."""
from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .activity import log_event
from .models import Client, Dsc, RegisterDeadline, db
from .permissions import BILLING, MANAGER, PARTNER, STAFF, get_visible_client_or_404, is_assigned, visible_clients
from .timeutil import today
from .validators import parse_date

bp = Blueprint("registers", __name__, url_prefix="/registers")

DSC_CLASSES = ["Class 3 (signing)", "Class 3 (signing + encryption)", "Class 3 organisation", "DGFT"]
ISSUERS = ["eMudhra", "Capricorn", "(n)Code", "Sify", "Pantasign", "IDSign", "VSign", "XtraTrust", "ProDigiSign"]
HELD_BY = ["Office", "Client"]
DSC_WARNING_DAYS = 30

DEADLINE_KINDS = {
    "udyam_update": "Udyam annual update",
    "iec_update": "IEC annual update",
    "gst_bank_10a": "GST bank account (Rule 10A)",
}
ANNUAL_KINDS = {"udyam_update", "iec_update"}
RULE_10A_DAYS = 30


def can_view(user):
    return user.role != BILLING


def can_edit(user, client):
    return user.role in (PARTNER, MANAGER) or (user.role == STAFF and is_assigned(user, client))


def _visible_ids(user):
    return visible_clients(user).with_only_columns(Client.id)


def days_left(d):
    return (d - today()).days


def next_iec_due(on):
    """IEC must be updated every April-June: due 30 June of this year, or next year once past."""
    due = date(on.year, 6, 30)
    return due if on <= due else date(on.year + 1, 6, 30)


@bp.before_request
def viewers_only():
    if not can_view(g.user):
        abort(403)


@bp.app_context_processor
def helpers():
    return {"days_left": days_left, "DEADLINE_KINDS": DEADLINE_KINDS, "DSC_WARNING_DAYS": DSC_WARNING_DAYS}


@bp.route("/")
def index():
    user = g.user
    client_id = request.args.get("client", type=int)
    within = request.args.get("within", type=int)
    dscs = db.select(Dsc).where(Dsc.client_id.in_(_visible_ids(user)))
    deadlines = db.select(RegisterDeadline).where(RegisterDeadline.client_id.in_(_visible_ids(user)))
    if request.args.get("show") != "all":
        dscs = dscs.where(Dsc.active.is_(True))
        deadlines = deadlines.where(RegisterDeadline.done_on.is_(None))
    if client_id:
        dscs = dscs.where(Dsc.client_id == client_id)
        deadlines = deadlines.where(RegisterDeadline.client_id == client_id)
    if within:
        limit = today() + timedelta(days=within)
        dscs = dscs.where(Dsc.expiry_date <= limit)
        deadlines = deadlines.where(RegisterDeadline.due_date <= limit)
    clients = db.session.execute(visible_clients(user).order_by(Client.code)).scalars().all()
    return render_template(
        "registers/index.html", args=request.args, clients=clients,
        dscs=db.session.execute(dscs.order_by(Dsc.expiry_date)).scalars().all(),
        deadlines=db.session.execute(deadlines.order_by(RegisterDeadline.due_date)).scalars().all(),
        editable={c.id for c in clients if can_edit(user, c)},
    )


def _clients_for_form():
    return [c for c in db.session.execute(visible_clients(g.user).order_by(Client.code)).scalars() if can_edit(g.user, c)]


def _dsc_form(dsc, values):
    return render_template("registers/dsc_form.html", dsc=dsc, values=values, clients=_clients_for_form(),
                           classes=DSC_CLASSES, issuers=ISSUERS, held_by=HELD_BY)


def _apply_dsc(dsc, form):
    errors = []
    client = db.session.get(Client, form.get("client_id", type=int) or 0)
    if client is None or client not in _clients_for_form():
        errors.append("Choose one of your clients")
    holder = form.get("holder_name", "").strip()
    expiry = parse_date(form.get("expiry_date", ""))
    if not holder:
        errors.append("Holder name is required")
    if form.get("dsc_class") not in DSC_CLASSES:
        errors.append("Choose the DSC class")
    issuer = form.get("issuer", "").strip()
    if not issuer:
        errors.append("Issuing authority is required")
    if expiry is None:
        errors.append("Expiry date is required")
    if form.get("held_by") not in HELD_BY:
        errors.append("Say whether the office or the client holds the token")
    if errors:
        return errors
    dsc.client, dsc.holder_name, dsc.dsc_class, dsc.issuer = client, holder, form["dsc_class"], issuer[:60]
    dsc.expiry_date, dsc.held_by = expiry, form["held_by"]
    dsc.notes = form.get("notes", "").strip()[:300] or None
    return []


def _dsc_text(d):
    return f"{d.holder_name}, {d.dsc_class}, {d.issuer}, expires {d.expiry_date.strftime('%d-%m-%Y')}, held by {d.held_by}" \
        + ("" if d.active else " (inactive)")


@bp.route("/dsc/new", methods=["GET", "POST"])
def dsc_new():
    renew_of = db.session.get(Dsc, request.values.get("renew", type=int) or 0)
    if renew_of is not None:
        get_visible_client_or_404(g.user, renew_of.client_id)
    if request.method == "POST":
        dsc = Dsc(active=True)
        with db.session.no_autoflush:
            errors = _apply_dsc(dsc, request.form)
        if not errors:
            db.session.add(dsc)
            changes = [("dsc", "", _dsc_text(dsc))]
            if renew_of is not None and renew_of.active and can_edit(g.user, renew_of.client):
                renew_of.active = False
                changes.insert(0, ("dsc", _dsc_text(renew_of).replace(" (inactive)", ""), _dsc_text(renew_of)))
            log_event(g.user, "dsc", client=dsc.client,
                      summary=f"{'Renewed' if renew_of else 'Added'} DSC for {dsc.holder_name}", changes=changes)
            db.session.commit()
            return redirect(url_for("registers.index"))
        for e in errors:
            flash(e, "error")
        return _dsc_form(None, request.form)
    values = {}
    if renew_of is not None:
        values = {"client_id": str(renew_of.client_id), "holder_name": renew_of.holder_name,
                  "dsc_class": renew_of.dsc_class, "issuer": renew_of.issuer, "held_by": renew_of.held_by}
    elif request.args.get("client_id"):
        values = {"client_id": request.args["client_id"]}
    return _dsc_form(None, values)


@bp.route("/dsc/<int:dsc_id>/edit", methods=["GET", "POST"])
def dsc_edit(dsc_id):
    dsc = db.session.get(Dsc, dsc_id) or abort(404)
    client = get_visible_client_or_404(g.user, dsc.client_id)
    if not can_edit(g.user, client):
        abort(403)
    if request.method == "POST":
        before = _dsc_text(dsc)
        with db.session.no_autoflush:
            errors = _apply_dsc(dsc, request.form)
        if not errors:
            dsc.active = bool(request.form.get("active"))
            if _dsc_text(dsc) != before:
                log_event(g.user, "dsc", client=dsc.client, summary=f"Edited DSC for {dsc.holder_name}",
                          changes=[("dsc", before, _dsc_text(dsc))])
            db.session.commit()
            return redirect(url_for("registers.index"))
        db.session.rollback()
        for e in errors:
            flash(e, "error")
        return _dsc_form(dsc, request.form)
    values = {"client_id": str(dsc.client_id), "holder_name": dsc.holder_name, "dsc_class": dsc.dsc_class,
              "issuer": dsc.issuer, "expiry_date": dsc.expiry_date.isoformat(), "held_by": dsc.held_by,
              "notes": dsc.notes or "", "active": "1" if dsc.active else ""}
    return _dsc_form(dsc, values)


def _deadline_text(d):
    text = f"{DEADLINE_KINDS[d.kind]} due {d.due_date.strftime('%d-%m-%Y')}"
    if d.done_on:
        text += f", done {d.done_on.strftime('%d-%m-%Y')}" + (f" ({d.reference})" if d.reference else "")
    return text


@bp.route("/deadlines/new", methods=["GET", "POST"])
def deadline_new():
    if request.method == "POST":
        form = request.form
        errors = []
        client = db.session.get(Client, form.get("client_id", type=int) or 0)
        kind = form.get("kind")
        if client is None or client not in _clients_for_form():
            errors.append("Choose one of your clients")
        if kind not in DEADLINE_KINDS:
            errors.append("Choose what is due")
        basis = parse_date(form.get("basis_date", ""))
        due = parse_date(form.get("due_date", ""))
        if kind == "gst_bank_10a":
            if basis is None:
                errors.append("Enter the GST registration grant date")
            else:
                due = basis + timedelta(days=RULE_10A_DAYS)
        elif kind == "iec_update" and due is None:
            due = next_iec_due(today())
        if due is None and not errors:
            errors.append("Due date is required")
        if not errors:
            deadline = RegisterDeadline(client=client, kind=kind, basis_date=basis if kind == "gst_bank_10a" else None,
                                        due_date=due, notes=form.get("notes", "").strip()[:300] or None)
            db.session.add(deadline)
            log_event(g.user, "deadline", client=client, summary=f"Added {DEADLINE_KINDS[kind]}",
                      changes=[("deadline", "", _deadline_text(deadline))])
            db.session.commit()
            return redirect(url_for("registers.index"))
        for e in errors:
            flash(e, "error")
    values = request.form if request.method == "POST" else {"client_id": request.args.get("client_id", "")}
    return render_template("registers/deadline_form.html", values=values, clients=_clients_for_form(),
                           kinds=DEADLINE_KINDS, rule_10a_days=RULE_10A_DAYS)


@bp.route("/deadlines/<int:deadline_id>/done", methods=["POST"])
def deadline_done(deadline_id):
    deadline = db.session.get(RegisterDeadline, deadline_id) or abort(404)
    client = get_visible_client_or_404(g.user, deadline.client_id)
    if not can_edit(g.user, client):
        abort(403)
    done_on = parse_date(request.form.get("done_on", "")) or today()
    if done_on > today():
        flash("Done date cannot be in the future.", "error")
        return redirect(url_for("registers.index"))
    before = _deadline_text(deadline)
    deadline.done_on, deadline.reference = done_on, request.form.get("reference", "").strip()[:100] or None
    changes = [("deadline", before, _deadline_text(deadline))]
    if deadline.kind in ANNUAL_KINDS:
        # Annual updates come round again: queue next year's straight away.
        try:
            nxt = deadline.due_date.replace(year=deadline.due_date.year + 1)
        except ValueError:  # 29 February
            nxt = deadline.due_date.replace(year=deadline.due_date.year + 1, day=28)
        db.session.add(RegisterDeadline(client=client, kind=deadline.kind, due_date=nxt, notes=deadline.notes))
        changes.append(("deadline", "", f"{DEADLINE_KINDS[deadline.kind]} due {nxt.strftime('%d-%m-%Y')}"))
    log_event(g.user, "deadline", client=client, summary=f"{DEADLINE_KINDS[deadline.kind]} done", changes=changes)
    db.session.commit()
    flash(f"{DEADLINE_KINDS[deadline.kind]} marked done.", "success")
    return redirect(url_for("registers.index"))
