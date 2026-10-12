"""Compliance board, My work, Review queue, filing detail, bulk actions and registrations."""
from collections import defaultdict

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import or_

from . import compliance as c
from . import periods as p
from .activity import log_event
from .models import Client, Filing, FilingComment, FilingMention, Registration, ReturnType, User, db
from .permissions import (
    ASSIGNABLE_ROLES,
    BILLING,
    MANAGER,
    PARTNER,
    STAFF,
    get_visible_client_or_404,
    is_assigned,
)
from .returns import CATEGORIES, OPEN_STATUSES, compute_due
from .timeutil import today
from .validators import parse_date

bp = Blueprint("board", __name__)


def _month_arg(value):
    try:
        return p.parse_month_key(value)
    except (ValueError, AttributeError):
        now = today()
        return now.year, now.month


def _get_filing(filing_id):
    filing = db.session.get(Filing, filing_id)
    # Same rule as clients: someone else's filing is simply "not found".
    if filing is None or not c.can_see_filing(g.user, filing):
        abort(404)
    return filing


def _staff_choices():
    return db.session.execute(
        db.select(User).where(User.active.is_(True), User.role.in_(ASSIGNABLE_ROLES)).order_by(User.name)
    ).scalars().all()


def _can_bulk_nil(user):
    return user.role in (PARTNER, MANAGER, STAFF)


@bp.app_context_processor
def helpers():
    return {"STATUSES": c.STATUSES, "colour": c.colour, "is_overdue": c.is_overdue,
            "format_inr": c.format_inr, "month_label": p.month_label, "today": today}


# --- Board -------------------------------------------------------------------

@bp.route("/board")
def board():
    user = g.user
    year, month = _month_arg(request.args.get("month"))
    key = p.month_key(year, month)
    filings = c.board_filings(user, key)

    category = request.args.get("category", "")
    staff_id = request.args.get("staff", type=int)
    q = request.args.get("q", "").strip().lower()
    only_overdue = bool(request.args.get("overdue"))
    if category:
        filings = [f for f in filings if f.return_type.category == category]
    if staff_id:
        filings = [f for f in filings if staff_id in (f.maker_id, f.reviewer_id)]
    if q:
        filings = [f for f in filings if q in f.client.name.lower() or q in f.client.code.lower()
                   or q in f.client.pan.lower() or q in f.registration.identifier.lower()]
    if only_overdue:
        filings = [f for f in filings if c.is_overdue(f)]

    clients = sorted({f.client for f in filings}, key=lambda cl: cl.code)
    columns = sorted({f.return_type for f in filings}, key=lambda rt: rt.sort_order)
    cells = defaultdict(list)
    for f in sorted(filings, key=lambda f: (f.gen_month != key, f.period_end, f.registration.identifier)):
        cells[(f.client_id, f.return_type_id)].append(f)
    column_open = {rt.id: sum(1 for f in filings if f.return_type_id == rt.id and f.gen_month == key
                              and f.status in ("not_started", "data_awaited", "data_received", "in_preparation"))
                   for rt in columns}
    prev_key, next_key = p.month_key(*p.add_months(year, month, -1)), p.month_key(*p.add_months(year, month, 1))
    generated = db.session.execute(db.select(Filing.id).where(Filing.gen_month == key).limit(1)).first() is not None
    return render_template(
        "board/board.html", key=key, prev_key=prev_key, next_key=next_key, clients=clients, columns=columns,
        cells=cells, column_open=column_open, total=len(filings), generated=generated,
        overdue=sum(1 for f in filings if c.is_overdue(f)), categories=CATEGORIES, staff=_staff_choices(),
        args=request.args, can_bulk=_can_bulk_nil(user), can_supervise=c.can_supervise(user),
    )


@bp.route("/board/generate", methods=["POST"])
def generate():
    if g.user.role != PARTNER:
        abort(403)
    year, month = _month_arg(request.form.get("month"))
    created, by_return = c.generate_period(year, month, g.user)
    db.session.commit()
    label = p.month_label(p.month_key(year, month))
    if created:
        flash(f"Generated {created} filing(s) for {label}: " + ", ".join(f"{n} {k}" for k, n in sorted(by_return.items())), "success")
    else:
        flash(f"Nothing new to generate for {label}. Filings already generated are left as they are.", "success")
    return redirect(url_for("board.board", month=p.month_key(year, month)))


@bp.route("/board/bulk-nil", methods=["POST"])
def bulk_nil():
    """Mark a whole column (one return type, this month) Nil or Not applicable."""
    if not _can_bulk_nil(g.user):
        abort(403)
    key = p.month_key(*_month_arg(request.form.get("month")))
    rt = db.session.get(ReturnType, request.form.get("return_type_id", type=int) or 0) or abort(404)
    kind = request.form.get("nil_kind")
    if kind not in ("Nil", "Not applicable"):
        abort(400)
    filings = db.session.execute(c.visible_filings(g.user).where(
        Filing.gen_month == key, Filing.return_type_id == rt.id,
        Filing.status.in_(("not_started", "data_awaited", "data_received", "in_preparation")),
    )).scalars().all()
    done = 0
    for filing in filings:
        if not c.apply_transition(filing, "nil_na", g.user, {"nil_kind": kind, "note": f"Bulk: whole column marked {kind}"}):
            done += 1
    db.session.commit()
    flash(f"Marked {done} {rt.name} filing(s) {kind}." + (f" {len(filings) - done} skipped (not yours)." if done < len(filings) else ""), "success")
    return redirect(url_for("board.board", month=key))


@bp.route("/board/reassign", methods=["POST"])
def reassign():
    """Move open filings from one person to another (e.g. someone on leave)."""
    if not c.can_supervise(g.user):
        abort(403)
    slot = request.form.get("slot")
    from_id = request.form.get("from_user", type=int)
    to_user = db.session.get(User, request.form.get("to_user", type=int) or 0)
    if slot not in ("maker", "reviewer") or not from_id or to_user is None or not to_user.active \
            or to_user.role not in ASSIGNABLE_ROLES or to_user.id == from_id:
        flash("Choose who the filings move from and to.", "error")
        return redirect(request.referrer or url_for("board.board"))
    column = Filing.maker_id if slot == "maker" else Filing.reviewer_id
    query = c.visible_filings(g.user).where(column == from_id, Filing.status.in_(OPEN_STATUSES))
    rt_id = request.form.get("return_type_id", type=int)
    if rt_id:
        query = query.where(Filing.return_type_id == rt_id)
    moved = skipped = 0
    old_user = db.session.get(User, from_id)
    for filing in db.session.execute(query).scalars():
        other = filing.reviewer_id if slot == "maker" else filing.maker_id
        if other == to_user.id:
            skipped += 1
            continue
        setattr(filing, f"{slot}_id", to_user.id)
        log_event(g.user, "filing.reassign", client=filing.client,
                  summary=f"{filing.return_type.name} {filing.period_label}: {slot} reassigned",
                  changes=[(slot, old_user.name if old_user else "", to_user.name)])
        moved += 1
    db.session.commit()
    flash(f"Reassigned {moved} open filing(s) to {to_user.name}."
          + (f" {skipped} skipped: they would be both maker and reviewer." if skipped else ""), "success")
    return redirect(request.referrer or url_for("board.board"))


# --- My work and Review queue -------------------------------------------------

def _by_due(query):
    return query.order_by(Filing.due_date.is_(None), Filing.due_date, Filing.id)


@bp.route("/my-work")
def my_work():
    user = g.user
    filings = db.session.execute(_by_due(c.visible_filings(user).where(
        Filing.maker_id == user.id, Filing.status.in_(OPEN_STATUSES)))).scalars().all()
    mentions = db.session.execute(
        db.select(FilingMention).join(FilingComment).where(FilingMention.user_id == user.id, FilingMention.seen.is_(False))
        .order_by(FilingComment.at.desc())
    ).scalars().all()
    mentions = [m for m in mentions if c.can_see_filing(user, m.comment.filing)]
    return render_template("board/my_work.html", filings=filings, mentions=mentions)


@bp.route("/review")
def review_queue():
    user = g.user
    everyone = c.can_supervise(user) and request.args.get("all") == "1"
    base = c.visible_filings(user)
    if not everyone:
        base = base.where(Filing.reviewer_id == user.id)
    to_review = db.session.execute(_by_due(base.where(Filing.status == "ready_for_review"))).scalars().all()
    to_close = db.session.execute(_by_due(base.where(Filing.status == "filed"))).scalars().all()
    return render_template("board/review.html", to_review=to_review, to_close=to_close, everyone=everyone)


# --- Filing detail -------------------------------------------------------------

@bp.route("/filings/<int:filing_id>")
def filing_detail(filing_id):
    filing = _get_filing(filing_id)
    unseen = db.session.execute(
        db.select(FilingMention).join(FilingComment)
        .where(FilingComment.filing_id == filing.id, FilingMention.user_id == g.user.id, FilingMention.seen.is_(False))
    ).scalars().all()
    for mention in unseen:
        mention.seen = True
    db.session.commit()
    handles = [(c.handle_of(u), u.name) for u in _staff_choices()]
    return render_template(
        "board/filing.html", f=filing, rt=filing.return_type, allowed=c.allowed_transitions(g.user, filing),
        can_supervise=c.can_supervise(g.user), staff=_staff_choices(), handles=handles,
        can_comment=g.user.role != BILLING and g.user.role != "readonly",
        can_set_event=filing.return_type.due_basis == "event" and filing.status in OPEN_STATUSES
        and (c.can_supervise(g.user) or c._is_maker(g.user, filing)),
        show_identifier=g.user.role != BILLING or filing.registration.kind == "GST",
    )


@bp.route("/filings/<int:filing_id>/status", methods=["POST"])
def filing_status(filing_id):
    filing = _get_filing(filing_id)
    errors = c.apply_transition(filing, request.form.get("to", ""), g.user, request.form)
    if errors:
        db.session.rollback()
        for e in errors:
            flash(e, "error")
    else:
        db.session.commit()
        flash(f"Status is now {c.STATUSES[filing.status]}.", "success")
    return redirect(url_for("board.filing_detail", filing_id=filing_id))


@bp.route("/filings/<int:filing_id>/comment", methods=["POST"])
def filing_comment(filing_id):
    filing = _get_filing(filing_id)
    if g.user.role in (BILLING, "readonly"):
        abort(403)
    body = request.form.get("body", "").strip()
    if body:
        comment = c.add_comment(filing, g.user, body[:5000])
        log_event(g.user, "filing.comment", client=filing.client,
                  summary=f"Commented on {filing.return_type.name} {filing.period_label}"
                  + (f" (mentioned {', '.join(m.user.name for m in comment.mentions if m.user)})" if comment.mentions else ""))
        db.session.commit()
    return redirect(url_for("board.filing_detail", filing_id=filing_id) + "#comments")


@bp.route("/filings/<int:filing_id>/assign", methods=["POST"])
def filing_assign(filing_id):
    filing = _get_filing(filing_id)
    if not c.can_supervise(g.user):
        abort(403)
    changes = []
    for slot in ("maker", "reviewer"):
        value = request.form.get(slot, type=int)
        user = db.session.get(User, value) if value else None
        if value and (user is None or not user.active or user.role not in ASSIGNABLE_ROLES):
            abort(400)
        old = getattr(filing, slot)
        if (old.id if old else None) != (user.id if user else None):
            changes.append((slot, old.name if old else "", user.name if user else ""))
            setattr(filing, f"{slot}_id", user.id if user else None)
    if filing.maker_id and filing.maker_id == filing.reviewer_id:
        db.session.rollback()
        flash("Maker and reviewer must be different people.", "error")
    elif changes:
        log_event(g.user, "filing.reassign", client=filing.client,
                  summary=f"{filing.return_type.name} {filing.period_label}: reassigned", changes=changes)
        db.session.commit()
    return redirect(url_for("board.filing_detail", filing_id=filing_id))


@bp.route("/filings/<int:filing_id>/event-date", methods=["POST"])
def filing_event_date(filing_id):
    """Enter the AGM date once; every AGM-based filing for that client and year picks it up."""
    filing = _get_filing(filing_id)
    if filing.return_type.due_basis != "event" or not (c.can_supervise(g.user) or c._is_maker(g.user, filing)):
        abort(403)
    event = parse_date(request.form.get("event_date", ""))
    if event is None or event < filing.period_end:
        flash("Enter a valid date after the year end.", "error")
        return redirect(url_for("board.filing_detail", filing_id=filing_id))
    siblings = db.session.execute(
        db.select(Filing).join(ReturnType).where(
            Filing.client_id == filing.client_id, Filing.period_end == filing.period_end,
            ReturnType.due_basis == "event", Filing.status.in_(OPEN_STATUSES))
    ).scalars().all()
    for sib in siblings:
        old = sib.event_date
        sib.event_date = event
        sib.due_date = compute_due(sib.return_type, sib.period_end, sib.period_code, event)
        log_event(g.user, "filing.event_date", client=sib.client,
                  summary=f"{sib.return_type.name} {sib.period_label}: {sib.return_type.event_label or 'event date'} set",
                  changes=[("event_date", old.strftime("%d-%m-%Y") if old else "", event.strftime("%d-%m-%Y"))])
    db.session.commit()
    flash(f"Date saved on {len(siblings)} filing(s); due dates updated.", "success")
    return redirect(url_for("board.filing_detail", filing_id=filing_id))


# --- Registrations -------------------------------------------------------------

def can_manage_registrations(user, client):
    return user.role in (PARTNER, MANAGER) or (user.role == STAFF and is_assigned(user, client))


def _registration_form(client, reg):
    form = request.form
    kind = form.get("kind", reg.kind if reg else "GST")
    clean, errors = c.validate_registration(
        client, kind, form.get("identifier", ""), form.get("scheme") or None,
        form.get("active_from", ""), form.get("active_to", ""), reg)
    chosen_ids = {int(x) for x in form.getlist("return_types") if x.isdigit()}
    tag_types = c.matching_return_types(clean.get("kind"), clean.get("scheme")) if clean else []
    chosen = [rt for rt in tag_types if rt.id in chosen_ids]
    if not errors and not chosen:
        errors.append("Tick at least one return")
    return clean, chosen, errors


def _render_registration(client, reg, values):
    all_types = db.session.execute(db.select(ReturnType).where(ReturnType.active.is_(True))
                                   .order_by(ReturnType.sort_order)).scalars().all()
    return render_template("board/registration.html", client=client, reg=reg, values=values,
                           kinds=c.KINDS, return_types=all_types)


@bp.route("/clients/<int:client_id>/registrations/new", methods=["GET", "POST"])
def registration_new(client_id):
    client = get_visible_client_or_404(g.user, client_id)
    if not can_manage_registrations(g.user, client):
        abort(403)
    if request.method == "POST":
        clean, chosen, errors = _registration_form(client, None)
        if not errors:
            reg = Registration(client=client, **clean)
            reg.return_types = chosen
            db.session.add(reg)
            db.session.flush()
            log_event(g.user, "registration.create", client=client,
                      summary=f"Added {c.KINDS[reg.kind][0]} {reg.identifier}",
                      changes=[("registration", "", f"{reg.tag} {reg.identifier}: " + ", ".join(rt.name for rt in chosen))])
            db.session.commit()
            return redirect(url_for("clients.profile", client_id=client.id, tab="filings"))
        for e in errors:
            flash(e, "error")
    values = request.form if request.method == "POST" else {"kind": "GST", "scheme": "Monthly"}
    return _render_registration(client, None, values)


@bp.route("/registrations/<int:reg_id>/edit", methods=["GET", "POST"])
def registration_edit(reg_id):
    reg = db.session.get(Registration, reg_id) or abort(404)
    client = get_visible_client_or_404(g.user, reg.client_id)
    if not can_manage_registrations(g.user, client):
        abort(403)
    if request.method == "POST":
        before = f"{reg.tag} {reg.identifier} {reg.active_from or ''}–{reg.active_to or ''}: " + ", ".join(rt.name for rt in reg.return_types)
        clean, chosen, errors = _registration_form(client, reg)
        has_filings = db.session.execute(db.select(Filing.id).where(Filing.registration_id == reg.id).limit(1)).first()
        if has_filings and clean and (clean["kind"], clean["identifier"], clean["scheme"]) != (reg.kind, reg.identifier, reg.scheme):
            errors.append("Filings exist for this registration: end it (Active to) and add a new one instead of changing type, number or scheme")
        if not errors:
            for key, value in clean.items():
                setattr(reg, key, value)
            reg.return_types = chosen
            after = f"{reg.tag} {reg.identifier} {reg.active_from or ''}–{reg.active_to or ''}: " + ", ".join(rt.name for rt in chosen)
            if after != before:
                log_event(g.user, "registration.update", client=client,
                          summary=f"Edited {c.KINDS[reg.kind][0]} {reg.identifier}", changes=[("registration", before, after)])
            db.session.commit()
            return redirect(url_for("clients.profile", client_id=client.id, tab="filings"))
        for e in errors:
            flash(e, "error")
        db.session.rollback()
    values = request.form if request.method == "POST" else {
        "kind": reg.kind, "identifier": reg.identifier, "scheme": reg.scheme or "",
        "active_from": reg.active_from.isoformat() if reg.active_from else "",
        "active_to": reg.active_to.isoformat() if reg.active_to else "",
    }
    return _render_registration(client, reg, values)


@bp.route("/registrations/<int:reg_id>/delete", methods=["POST"])
def registration_delete(reg_id):
    reg = db.session.get(Registration, reg_id) or abort(404)
    client = get_visible_client_or_404(g.user, reg.client_id)
    if not can_manage_registrations(g.user, client):
        abort(403)
    if db.session.execute(db.select(Filing.id).where(Filing.registration_id == reg.id).limit(1)).first():
        flash("This registration has filings; set an Active-to date instead of deleting it.", "error")
    else:
        log_event(g.user, "registration.delete", client=client, summary=f"Deleted {c.KINDS[reg.kind][0]} {reg.identifier}",
                  changes=[("registration", f"{reg.tag} {reg.identifier}", "")])
        db.session.delete(reg)
        db.session.commit()
    return redirect(url_for("clients.profile", client_id=client.id, tab="filings"))


@bp.route("/registrations/from-master", methods=["POST"])
def registrations_from_master():
    """One-off migration: create registrations from GSTINs, TAN and services in the client master."""
    if g.user.role != PARTNER:
        abort(403)
    total = 0
    for client in db.session.execute(db.select(Client).where(Client.status == "Active")).scalars():
        created = c.registrations_from_master(client)
        if created:
            db.session.flush()
            db.session.expire(client, ["registrations"])
            log_event(g.user, "registration.create", client=client,
                      summary=f"Added {len(created)} registration(s) from client master",
                      changes=[("registration", "", f"{r.tag} {r.identifier}: " + ", ".join(rt.name for rt in r.return_types))
                               for r in created])
            total += len(created)
    db.session.commit()
    flash(f"Created {total} registration(s) from the client master. Review each client's Filings tab for optional returns (IFF, GSTR-9, MSME-1).", "success")
    return redirect(url_for("returns.index"))


def client_filings_context(user, client):
    """Data for the Filings tab on the client profile."""
    filings = db.session.execute(
        c.visible_filings(user).where(Filing.client_id == client.id)
        .order_by(Filing.period_end.desc(), Filing.return_type_id).limit(200)
    ).scalars().all()
    return {
        "registrations": client.registrations if user.role != BILLING else [],
        "client_filings": filings,
        "can_manage_regs": can_manage_registrations(user, client),
        "kinds": c.KINDS,
    }
