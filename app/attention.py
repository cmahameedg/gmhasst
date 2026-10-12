"""What needs action, ordered by consequence rather than date.

1. Notices with reply due within 3 days, or already past due (also hearings within 3 days).
2. Filings overdue (past due date and not Filed).
3. Filings due within 3 days and not yet at Approved.
4. Items sitting in Review queries for more than 2 days.
5. DSCs expiring within 30 days (and registration deadlines due within 30 days).
6. Filings due within 7 days.

Each item appears once, in the highest category it qualifies for, and belongs to the
person who must act next: the notice's assignee; a filing's reviewer while it is
Ready for review, otherwise its maker; the client's primary staff for DSCs and
registration deadlines.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .compliance import STATUSES as FILING_STATUSES
from .compliance import format_inr
from .models import Client, Dsc, Filing, FilingHistory, Notice, RegisterDeadline, User, db
from .notices import AWAITING_REPLY, CLOSED
from .permissions import ASSIGNABLE_ROLES, visible_clients
from .registers import DEADLINE_KINDS, DSC_WARNING_DAYS
from .returns import OPEN_STATUSES
from .timeutil import now_utc, today

NOTICE_DAYS = 3
FILING_SOON_DAYS = 3
REVIEW_QUERY_DAYS = 2
FILING_WEEK_DAYS = 7
ON_TIME_WINDOW_DAYS = 90

CATEGORIES = {
    1: "Notices: reply due within 3 days or overdue",
    2: "Filings overdue",
    3: "Filings due within 3 days, not yet approved",
    4: "In review queries for more than 2 days",
    5: "DSCs and registration deadlines within 30 days",
    6: "Filings due within 7 days",
}


@dataclass
class Item:
    category: int
    kind: str
    due: date | None
    title: str
    detail: str
    endpoint: str
    args: dict
    owner_id: int | None
    client: Client
    overdue: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def sort_key(self):
        return (self.category, self.due or date.max, self.client.code, self.title)


def _owner(filing):
    return filing.reviewer_id if filing.status == "ready_for_review" else filing.maker_id


def _review_query_since(filing_ids):
    """filing id -> when it last entered Review queries."""
    if not filing_ids:
        return {}
    rows = db.session.execute(
        db.select(FilingHistory.filing_id, db.func.max(FilingHistory.at))
        .where(FilingHistory.filing_id.in_(filing_ids), FilingHistory.to_status == "review_queries")
        .group_by(FilingHistory.filing_id)
    ).all()
    return dict(rows)


def collect(on=None, client_ids=None):
    """Every actionable item in the firm (optionally limited to some clients), ranked."""
    on = on or today()
    now = now_utc()
    items = []

    def scoped(query, column):
        return query.where(column.in_(client_ids)) if client_ids is not None else query

    # 1. Notices
    notices = db.session.execute(scoped(db.select(Notice).where(Notice.status.not_in(CLOSED)), Notice.client_id)).scalars()
    for n in notices:
        due, what = None, None
        if n.status in AWAITING_REPLY and n.reply_due_date <= on + timedelta(days=NOTICE_DAYS):
            due, what = n.reply_due_date, "Reply due"
        elif n.status == "Hearing fixed" and n.hearing_date and n.hearing_date <= on + timedelta(days=NOTICE_DAYS):
            due, what = n.hearing_date, "Hearing"
        if due is None:
            continue
        amount = f" · demand {format_inr(n.amount_demanded_paise)}" if n.amount_demanded_paise else ""
        items.append(Item(1, "notice", due, f"{n.department} {n.notice_type} notice", f"{what} · ref {n.reference_no} · {n.period}{amount}",
                          "notices.detail", {"notice_id": n.id}, n.assigned_to_id, n.client, overdue=due < on,
                          extra={"status": n.status}))

    # 2, 3, 4, 6. Filings
    filings = db.session.execute(scoped(
        db.select(Filing).where(Filing.status.in_(OPEN_STATUSES)).where(
            (Filing.status == "review_queries")
            | ((Filing.due_date.is_not(None)) & (Filing.due_date <= on + timedelta(days=FILING_WEEK_DAYS)))
        ), Filing.client_id)).scalars().all()
    since = _review_query_since([f.id for f in filings if f.status == "review_queries"])
    for f in filings:
        due = f.due_date
        stuck = f.status == "review_queries" and since.get(f.id) and now - since[f.id] > timedelta(days=REVIEW_QUERY_DAYS)
        if due is not None and due < on:
            category = 2
        elif due is not None and due <= on + timedelta(days=FILING_SOON_DAYS) and f.status != "approved":
            category = 3
        elif stuck:
            category = 4
        elif due is not None and due <= on + timedelta(days=FILING_WEEK_DAYS):
            category = 6
        else:
            continue
        detail = f"{f.period_label} · {FILING_STATUSES[f.status]}"
        if stuck:
            detail += f" · in review queries {(now - since[f.id]).days} days"
        items.append(Item(category, "filing", due, f.return_type.name, detail, "board.filing_detail",
                          {"filing_id": f.id}, _owner(f), f.client, overdue=due is not None and due < on,
                          extra={"status": f.status}))

    # 5. DSCs and registration deadlines
    limit = on + timedelta(days=DSC_WARNING_DAYS)
    for d in db.session.execute(scoped(db.select(Dsc).where(Dsc.active.is_(True), Dsc.expiry_date <= limit),
                                       Dsc.client_id)).scalars():
        left = (d.expiry_date - on).days
        when = f"expired {-left} day(s) ago" if left < 0 else "expires today" if left == 0 else f"expires in {left} day(s)"
        items.append(Item(5, "dsc", d.expiry_date, f"DSC: {d.holder_name}", f"{d.dsc_class} · {d.issuer} · {when} · held by {d.held_by}",
                          "registers.index", {"client": d.client_id}, d.client.primary_staff_id, d.client,
                          overdue=left < 0))
    for r in db.session.execute(scoped(db.select(RegisterDeadline).where(
            RegisterDeadline.done_on.is_(None), RegisterDeadline.due_date <= limit), RegisterDeadline.client_id)).scalars():
        items.append(Item(5, "deadline", r.due_date, DEADLINE_KINDS[r.kind], r.notes or "", "registers.index",
                          {"client": r.client_id}, r.client.primary_staff_id, r.client, overdue=r.due_date < on))

    return sorted(items, key=lambda i: i.sort_key)


def visible_client_ids(user):
    return set(db.session.execute(visible_clients(user).with_only_columns(Client.id)).scalars())


def items_for(user, on=None):
    """The user's own items: things they must act on, on clients they can see."""
    return [i for i in collect(on, visible_client_ids(user)) if i.owner_id == user.id]


def grouped(items):
    groups = defaultdict(list)
    for item in items:
        groups[item.category].append(item)
    return [(cat, CATEGORIES[cat], groups[cat]) for cat in sorted(groups)]


def team_stats(on=None):
    """Per person: open items, overdue items, and on-time filing rate over the last 90 days."""
    on = on or today()
    people = db.session.execute(
        db.select(User).where(User.active.is_(True), User.role.in_(ASSIGNABLE_ROLES)).order_by(User.name)
    ).scalars().all()
    open_filings = db.session.execute(db.select(Filing).where(Filing.status.in_(OPEN_STATUSES))).scalars().all()
    open_notices = db.session.execute(db.select(Notice).where(Notice.status.not_in(CLOSED))).scalars().all()
    window_start = on - timedelta(days=ON_TIME_WINDOW_DAYS)
    filed = db.session.execute(db.select(Filing).where(
        Filing.status.in_(("filed", "closed")), Filing.filed_on.is_not(None),
        Filing.filed_on >= window_start, Filing.filed_on <= on)).scalars().all()

    def rate(rows):
        timed = [f for f in rows if f.due_date is not None]
        if not timed:
            return None
        return round(100 * sum(1 for f in timed if f.filed_on <= f.due_date) / len(timed))

    rows = []
    for person in people:
        mine_f = [f for f in open_filings if _owner(f) == person.id]
        mine_n = [n for n in open_notices if n.assigned_to_id == person.id]
        overdue = sum(1 for f in mine_f if f.due_date and f.due_date < on) + sum(
            1 for n in mine_n if n.status in AWAITING_REPLY and n.reply_due_date < on)
        mine_filed = [f for f in filed if f.maker_id == person.id]
        rows.append({"user": person, "open": len(mine_f) + len(mine_n), "overdue": overdue,
                     "filed": len(mine_filed), "on_time": rate(mine_filed)})
    unowned_f = [f for f in open_filings if _owner(f) is None]
    firm = {
        "open": len(open_filings) + len(open_notices),
        "overdue": sum(1 for f in open_filings if f.due_date and f.due_date < on)
        + sum(1 for n in open_notices if n.status in AWAITING_REPLY and n.reply_due_date < on),
        "filed": len(filed), "on_time": rate(filed), "unassigned": len(unowned_f),
    }
    return rows, firm
