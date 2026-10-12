from datetime import timedelta

from flask import Blueprint, abort, g, render_template, request
from sqlalchemy import or_

from .models import ActivityChange, ActivityEvent, Client, User, db
from .timeutil import local_day_start_utc
from .validators import parse_date

bp = Blueprint("activity", __name__)

PAGE_SIZE = 50


def log_event(actor, action, client=None, subject=None, summary=None, changes=()):
    """Record an action. ``changes`` is a list of (field, old, new) tuples. Caller commits."""
    event = ActivityEvent(
        user_id=actor.id if actor else None,
        client_id=client.id if client else None,
        subject_user_id=subject.id if subject else None,
        action=action,
        summary=summary,
    )
    for field, old, new in changes:
        event.changes.append(ActivityChange(field=field, old_value=old, new_value=new))
    db.session.add(event)
    return event


@bp.route("/activity")
def index():
    from .permissions import FIELDS, can_view_activity, visible_fields

    user = g.user
    if not can_view_activity(user):
        abort(403)

    allowed = visible_fields(user)
    query = db.select(ActivityEvent).order_by(ActivityEvent.at.desc(), ActivityEvent.id.desc())

    client_id = request.args.get("client", type=int)
    staff_id = request.args.get("staff", type=int)
    date_from = parse_date(request.args.get("from", ""))
    date_to = parse_date(request.args.get("to", ""))
    if client_id:
        query = query.where(ActivityEvent.client_id == client_id)
    if staff_id:
        query = query.where(
            or_(ActivityEvent.user_id == staff_id, ActivityEvent.subject_user_id == staff_id)
        )
    if date_from:
        query = query.where(ActivityEvent.at >= local_day_start_utc(date_from))
    if date_to:
        query = query.where(ActivityEvent.at < local_day_start_utc(date_to + timedelta(days=1)))

    hidden = set(FIELDS) - allowed
    if hidden:
        # Drop events whose every change is a hidden field (e.g. a fee-only edit).
        has_visible = db.select(ActivityChange.id).where(
            ActivityChange.event_id == ActivityEvent.id, ActivityChange.field.not_in(hidden)
        ).exists()
        has_any = db.select(ActivityChange.id).where(ActivityChange.event_id == ActivityEvent.id).exists()
        query = query.where(or_(~has_any, has_visible))

    page = max(request.args.get("page", 1, type=int), 1)
    events = db.session.execute(query.limit(PAGE_SIZE + 1).offset((page - 1) * PAGE_SIZE)).scalars().all()
    has_next = len(events) > PAGE_SIZE
    events = events[:PAGE_SIZE]

    rows = []
    for event in events:
        changes = [c for c in event.changes if c.field not in hidden]
        rows.append((event, changes))

    return render_template(
        "activity/index.html",
        rows=rows,
        fields=FIELDS,
        clients=db.session.execute(db.select(Client).order_by(Client.code)).scalars().all(),
        staff=db.session.execute(db.select(User).order_by(User.name)).scalars().all(),
        args=request.args,
        page=page,
        has_next=has_next,
    )
