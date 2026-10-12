"""Attention dashboard, calendar month view and private iCal feed."""
import calendar as pycal
import hashlib
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from . import attention
from . import periods as p
from .activity import log_event
from .models import CalendarFeed, Client, Dsc, Filing, Notice, RegisterDeadline, ReturnType, User, db
from .notices import AWAITING_REPLY, CLOSED
from .notices import can_view as can_view_notices
from .permissions import MANAGER, PARTNER, visible_clients
from .registers import DEADLINE_KINDS
from .returns import OPEN_STATUSES, compute_due
from .timeutil import today

bp = Blueprint("dashboard", __name__)


@bp.route("/today")
def today_view():
    user = g.user
    can_firm = user.role in (PARTNER, MANAGER)
    scope = request.args.get("view") or ("firm" if user.role == PARTNER else "mine")
    if scope == "firm" and not can_firm:
        abort(403)
    on = today()
    if scope == "firm":
        items = attention.collect(on)
    else:
        items = attention.items_for(user, on)
    team = firm = None
    if scope == "firm" and user.role == PARTNER:
        team, firm = attention.team_stats(on)
    owners = {u.id: u for u in db.session.execute(db.select(User)).scalars()}
    return render_template("dashboard/today.html", groups=attention.grouped(items), total=len(items), scope=scope,
                           can_firm=can_firm, team=team, firm=firm, owners=owners, on=on,
                           window=attention.ON_TIME_WINDOW_DAYS)


# --- Calendar ----------------------------------------------------------------

@dataclass
class Entry:
    day: date
    kind: str  # filing, master, notice, hearing, dsc, deadline
    title: str
    detail: str
    endpoint: str | None
    args: dict
    css: str
    uid: str


def calendar_entries(user, start, end):
    """Everything due between start and end (inclusive) that this user can see."""
    on = today()
    client_ids = visible_clients(user).with_only_columns(Client.id)
    entries = []

    filings = db.session.execute(db.select(Filing).where(
        Filing.client_id.in_(client_ids), Filing.due_date >= start, Filing.due_date <= end)).scalars().all()
    groups = defaultdict(list)
    for f in filings:
        groups[(f.return_type_id, f.due_date, f.period_label)].append(f)
    covered = set()
    for (rt_id, due, label), rows in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[1][0].return_type.sort_order)):
        rt = rows[0].return_type
        covered.add((rt_id, due))
        open_rows = [f for f in rows if f.status in OPEN_STATUSES]
        css = "red" if open_rows and due < on else "amber" if open_rows else "green"
        names = ", ".join(sorted({f.client.name for f in open_rows})[:6])
        detail = f"{len(open_rows)} open of {len(rows)}" + (f": {names}" if names else "")
        entries.append(Entry(due, "filing", f"{rt.name} · {label}", detail, "board.board",
                             {"month": rows[0].gen_month}, css, f"filing-{rt_id}-{due.isoformat()}"))

    # Return Master dates for periods not generated yet, so the month is complete.
    for rt in db.session.execute(db.select(ReturnType).where(ReturnType.active.is_(True), ReturnType.due_basis == "period")
                                 .order_by(ReturnType.sort_order)).scalars():
        y, m = p.add_months(start.year, start.month, -26)
        while (y, m) <= (end.year, end.month):
            period = p.period_for(rt.frequency, y, m)
            if period is not None:
                due = compute_due(rt, period.end, period.code)
                if start <= due <= end and (rt.id, due) not in covered:
                    entries.append(Entry(due, "master", f"{rt.name} · {period.label}", "From the Return Master (not generated)",
                                         "returns.index", {}, "grey", f"master-{rt.id}-{due.isoformat()}"))
            y, m = p.add_months(y, m, 1)

    if can_view_notices(user):
        for n in db.session.execute(db.select(Notice).where(Notice.client_id.in_(client_ids), Notice.status.not_in(CLOSED))).scalars():
            if n.status in AWAITING_REPLY and start <= n.reply_due_date <= end:
                entries.append(Entry(n.reply_due_date, "notice", f"Notice reply: {n.client.name}",
                                     f"{n.department} {n.notice_type} · ref {n.reference_no} · {n.assigned_to.name}",
                                     "notices.detail", {"notice_id": n.id}, "red" if n.reply_due_date < on else "blue",
                                     f"notice-{n.id}"))
            if n.status == "Hearing fixed" and n.hearing_date and start <= n.hearing_date <= end:
                entries.append(Entry(n.hearing_date, "hearing", f"Hearing: {n.client.name}",
                                     f"{n.department} {n.notice_type} · ref {n.reference_no}", "notices.detail",
                                     {"notice_id": n.id}, "blue", f"hearing-{n.id}"))
        for d in db.session.execute(db.select(Dsc).where(Dsc.client_id.in_(client_ids), Dsc.active.is_(True),
                                                         Dsc.expiry_date >= start, Dsc.expiry_date <= end)).scalars():
            entries.append(Entry(d.expiry_date, "dsc", f"DSC expires: {d.holder_name}", f"{d.client.name} · held by {d.held_by}",
                                 "registers.index", {"client": d.client_id}, "amber", f"dsc-{d.id}"))
        for r in db.session.execute(db.select(RegisterDeadline).where(
                RegisterDeadline.client_id.in_(client_ids), RegisterDeadline.done_on.is_(None),
                RegisterDeadline.due_date >= start, RegisterDeadline.due_date <= end)).scalars():
            entries.append(Entry(r.due_date, "deadline", f"{DEADLINE_KINDS[r.kind]}: {r.client.name}", r.notes or "",
                                 "registers.index", {"client": r.client_id}, "amber", f"deadline-{r.id}"))
    return sorted(entries, key=lambda e: (e.day, e.kind != "notice", e.title))


@bp.route("/calendar")
def month_view():
    try:
        year, month = p.parse_month_key(request.args.get("month", ""))
    except ValueError:
        year, month = today().year, today().month
    first = date(year, month, 1)
    last = date(year, month, p.last_day(year, month))
    entries = calendar_entries(g.user, first, last)
    by_day = defaultdict(list)
    for e in entries:
        by_day[e.day].append(e)
    weeks = pycal.Calendar(firstweekday=0).monthdatescalendar(year, month)
    has_feed = db.session.execute(db.select(CalendarFeed.id).where(CalendarFeed.user_id == g.user.id)).first() is not None
    return render_template("dashboard/calendar.html", weeks=weeks, by_day=by_day, month=month, year=year,
                           key=p.month_key(year, month), prev_key=p.month_key(*p.add_months(year, month, -1)),
                           next_key=p.month_key(*p.add_months(year, month, 1)), has_feed=has_feed,
                           new_feed_url=request.args.get("feed_url"))


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


@bp.route("/calendar/feed", methods=["POST"])
def create_feed():
    """Create (or replace) this user's private feed link. The link is shown once."""
    token = secrets.token_urlsafe(32)
    feed = db.session.execute(db.select(CalendarFeed).where(CalendarFeed.user_id == g.user.id)).scalar_one_or_none()
    if request.form.get("action") == "revoke":
        if feed:
            db.session.delete(feed)
            log_event(g.user, "calendar.feed", subject=g.user, summary="Revoked calendar feed link")
            db.session.commit()
        flash("Calendar feed link revoked. Calendars using it will stop updating.", "success")
        return redirect(url_for("dashboard.month_view"))
    if feed:
        feed.token_hash = _hash(token)
    else:
        db.session.add(CalendarFeed(user_id=g.user.id, token_hash=_hash(token)))
    log_event(g.user, "calendar.feed", subject=g.user, summary="Created calendar feed link")
    db.session.commit()
    url = url_for("dashboard.feed", token=token, _external=True)
    return render_template("dashboard/feed_link.html", url=url)


def _ics_escape(text):
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line):
    """RFC 5545: lines longer than 75 octets are folded with CRLF + space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(current) + len(b) > (75 if not parts else 74):
            parts.append(current.decode("utf-8"))
            current = b""
        current += b
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def build_ics(user, entries, base_url):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//GMH Practice//Compliance//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_escape('GMH due dates')}", "X-WR-TIMEZONE:Asia/Kolkata"]
    for e in entries:
        link = base_url.rstrip("/") + url_for(e.endpoint, **e.args) if e.endpoint else ""
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e.uid}-u{user.id}@gmh-practice",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{e.day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(e.day + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape(e.title)}",
            f"DESCRIPTION:{_ics_escape(e.detail + (chr(10) + link if link else ''))}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


@bp.route("/calendar/feed/<token>.ics")
def feed(token):
    """Public URL (for Google Calendar): the unguessable token is the credential."""
    feed_row = db.session.execute(db.select(CalendarFeed).where(CalendarFeed.token_hash == _hash(token))).scalar_one_or_none()
    if feed_row is None or not feed_row.user.active:
        abort(404)
    on = today()
    entries = calendar_entries(feed_row.user, on - timedelta(days=60), on + timedelta(days=365))
    body = build_ics(feed_row.user, entries, request.host_url)
    return Response(body, mimetype="text/calendar",
                    headers={"Content-Disposition": "inline; filename=gmh-due-dates.ics"})
