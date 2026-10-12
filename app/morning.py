"""Morning email: 8:00 AM IST on working days (run by cron; see README).

Each staff member gets their own items 1-6 from the attention dashboard.
Partners get the firm-wide summary with team counts.
"""
from datetime import date

from flask import current_app, render_template, url_for

from . import attention
from .mailer import send_mail
from .models import MorningEmailLog, User, db
from .permissions import ARTICLE, MANAGER, PARTNER, STAFF

RECIPIENT_ROLES = (PARTNER, MANAGER, STAFF, ARTICLE)


def is_working_day(day):
    config = current_app.config
    working = {int(d) for d in str(config["WORKING_DAYS"]).split(",") if d.strip()}
    holidays = {h.strip() for h in str(config.get("HOLIDAYS") or "").split(",") if h.strip()}
    return day.weekday() in working and day.isoformat() not in holidays


def _link(endpoint, **args):
    return current_app.config["BASE_URL"].rstrip("/") + url_for(endpoint, **args)


def build(user, day):
    """Returns (subject, text, html, item_count) for one person."""
    firm = user.role == PARTNER
    items = attention.collect(day) if firm else attention.items_for(user, day)
    groups = attention.grouped(items)
    team = firm_totals = None
    if firm:
        team, firm_totals = attention.team_stats(day)
    owners = {u.id: u.name for u in db.session.execute(db.select(User)).scalars()}
    overdue = sum(1 for i in items if i.overdue)
    when = day.strftime("%a %d %b")
    if firm:
        subject = f"Firm summary {when}: {len(items)} item(s), {overdue} overdue"
    elif items:
        subject = f"Your day {when}: {len(items)} item(s)" + (f", {overdue} overdue" if overdue else "")
    else:
        subject = f"Your day {when}: nothing urgent"
    context = dict(user=user, day=day, groups=groups, total=len(items), firm=firm, team=team,
                   firm_totals=firm_totals, owners=owners, link=_link, window=attention.ON_TIME_WINDOW_DAYS)
    text = render_template("email/morning.txt", **context)
    html = render_template("email/morning.html", **context)
    return subject, text, html, len(items)


def send_all(day, force=False, dry_run=False):
    """Send today's emails. Returns a list of (user, outcome) pairs."""
    if not force and not is_working_day(day):
        return []
    results = []
    people = db.session.execute(
        db.select(User).where(User.active.is_(True), User.role.in_(RECIPIENT_ROLES), User.password_hash.is_not(None))
        .order_by(User.role != PARTNER, User.name)
    ).scalars().all()
    for user in people:
        already = db.session.execute(db.select(MorningEmailLog).where(
            MorningEmailLog.user_id == user.id, MorningEmailLog.for_date == day)).scalar_one_or_none()
        if already and not force:
            results.append((user, "already sent"))
            continue
        subject, text, html, count = build(user, day)
        if dry_run:
            results.append((user, f"would send: {subject}"))
            continue
        try:
            send_mail(user.email, subject, text, html)
        except Exception as exc:  # one bad address must not stop everyone else's email
            current_app.logger.exception("Morning email to %s failed", user.email)
            results.append((user, f"failed: {exc}"))
            continue
        if already:
            already.item_count = count
        else:
            db.session.add(MorningEmailLog(user_id=user.id, for_date=day, item_count=count))
        db.session.commit()
        results.append((user, f"sent: {subject}"))
    return results
