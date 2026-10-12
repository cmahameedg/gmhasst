"""Notices register: GST / Income Tax / TDS / ROC notices, replies, hearings and orders."""
import os
import secrets

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from sqlalchemy import or_
from werkzeug.utils import secure_filename

from .activity import log_event
from .compliance import format_inr, parse_rupees
from .models import Client, Notice, NoticeDocument, NoticeHistory, User, db
from .permissions import (
    ARTICLE,
    ASSIGNABLE_ROLES,
    BILLING,
    MANAGER,
    PARTNER,
    STAFF,
    get_visible_client_or_404,
    is_assigned,
    visible_clients,
)
from .timeutil import today
from .validators import parse_date

bp = Blueprint("notices", __name__, url_prefix="/notices")

DEPARTMENTS = ["GST", "Income Tax", "TDS", "ROC", "Other"]
NOTICE_TYPES = {
    "GST": ["ASMT-10", "DRC-01", "DRC-01A", "DRC-01B", "DRC-01C", "REG-03", "REG-17", "RFD-08", "Others"],
    "Income Tax": ["Intimation u/s 143(1)", "Scrutiny", "Defective return", "Demand", "Others"],
    "TDS": ["Others"],
    "ROC": ["Others"],
    "Other": ["Others"],
}
STATUSES = [
    "Received", "Documents awaited", "Reply drafted", "Under review", "Replied",
    "Hearing fixed", "Closed — dropped", "Closed — order passed", "Appeal",
]
# Statuses in which a reply is still owed: the reply due date drives alerts.
AWAITING_REPLY = {"Received", "Documents awaited", "Reply drafted", "Under review"}
CLOSED = {"Closed — dropped", "Closed — order passed"}
DOC_KINDS = {"notice": "Notice", "reply": "Reply", "order": "Order", "other": "Other"}
MAX_PDF_BYTES = 15 * 1024 * 1024


def can_view(user):
    return user.role != BILLING


def can_edit(user, notice=None, client=None):
    """Create or edit all fields: Partner, Manager, or Staff on their own clients / notices."""
    client = client or (notice.client if notice else None)
    if user.role in (PARTNER, MANAGER):
        return True
    if user.role == STAFF:
        return (client is not None and is_assigned(user, client)) or (notice is not None and notice.assigned_to_id == user.id)
    return False


def can_change_status(user, notice):
    return can_edit(user, notice) or (user.role == ARTICLE and notice.assigned_to_id == user.id)


def visible_notices(user):
    return db.select(Notice).where(Notice.client_id.in_(visible_clients(user).with_only_columns(Client.id)))


def _get(notice_id):
    if not can_view(g.user):
        abort(403)
    notice = db.session.execute(visible_notices(g.user).where(Notice.id == notice_id)).scalar_one_or_none()
    if notice is None:
        abort(404)
    return notice


def _assignable():
    return db.session.execute(
        db.select(User).where(User.active.is_(True), User.role.in_(ASSIGNABLE_ROLES)).order_by(User.name)
    ).scalars().all()


def _snapshot(n):
    return {
        "department": n.department or "", "notice_type": n.notice_type or "", "description": n.description or "",
        "reference_no": n.reference_no or "", "notice_date": n.notice_date.strftime("%d-%m-%Y") if n.notice_date else "",
        "period": n.period or "", "amount_demanded": format_inr(n.amount_demanded_paise),
        "reply_due_date": n.reply_due_date.strftime("%d-%m-%Y") if n.reply_due_date else "",
        "assigned_to": n.assigned_to.name if n.assigned_to else "", "status": n.status or "",
        "hearing_date": n.hearing_date.strftime("%d-%m-%Y") if n.hearing_date else "",
        "reply_date": n.reply_date.strftime("%d-%m-%Y") if n.reply_date else "", "reply_ref": n.reply_ref or "",
        "outcome_amount": format_inr(n.outcome_amount_paise),
    }


def validate_fields(form, notice):
    """Apply the main notice fields from a form. Returns a list of errors."""
    errors = []
    department = form.get("department", "")
    notice_type = form.get("notice_type", "")
    if department not in DEPARTMENTS:
        errors.append("Choose a department")
    elif notice_type not in NOTICE_TYPES[department]:
        errors.append(f"Choose a notice type for {department}")
    description = form.get("description", "").strip()
    if notice_type == "Others" and not description:
        errors.append("Describe the notice when the type is Others")
    reference = form.get("reference_no", "").strip()
    notice_date = parse_date(form.get("notice_date", ""))
    period = form.get("period", "").strip()
    reply_due = parse_date(form.get("reply_due_date", ""))
    if not reference:
        errors.append("Reference number is required")
    if notice_date is None:
        errors.append("Notice date is required")
    elif notice_date > today():
        errors.append("Notice date cannot be in the future")
    if not period:
        errors.append("Period / FY is required")
    if reply_due is None:
        errors.append("Reply due date is required")
    elif notice_date and reply_due < notice_date:
        errors.append("Reply due date is before the notice date")
    try:
        amount = parse_rupees(form.get("amount_demanded"))
    except ValueError:
        errors.append("Amount demanded must be in rupees, e.g. 1,25,000")
        amount = None
    assignee = db.session.get(User, form.get("assigned_to", type=int) or 0)
    if assignee is None or not assignee.active or assignee.role not in ASSIGNABLE_ROLES:
        errors.append("Assign the notice to a staff member")
    if errors:
        return errors
    notice.department, notice.notice_type, notice.description = department, notice_type, description or None
    notice.reference_no, notice.notice_date, notice.period = reference, notice_date, period
    notice.reply_due_date, notice.amount_demanded_paise, notice.assigned_to = reply_due, amount, assignee
    return []


def apply_status(notice, to, form, user):
    """Change status with the brief's rules. Returns errors; caller commits."""
    if to not in STATUSES:
        return ["Choose a status"]
    errors = []
    if to == "Replied":
        reply_date = parse_date(form.get("reply_date", "")) or notice.reply_date
        reply_ref = (form.get("reply_ref") or notice.reply_ref or "").strip()
        if reply_date is None or not reply_ref:
            errors.append("Reply date and reply reference are required to mark Replied")
        elif reply_date > today() or reply_date < notice.notice_date:
            errors.append("Reply date must be between the notice date and today")
        else:
            notice.reply_date, notice.reply_ref = reply_date, reply_ref
    if to == "Hearing fixed":
        hearing = parse_date(form.get("hearing_date", ""))
        if hearing is None:
            errors.append("Hearing date is required")
        else:
            notice.hearing_date = hearing
    if to in CLOSED:
        try:
            outcome = parse_rupees(form.get("outcome_amount"))
        except ValueError:
            outcome, errors = None, errors + ["Outcome amount must be in rupees"]
        if outcome is None and not errors:
            errors.append("Outcome amount is required to close (enter 0 if nothing is payable)")
        elif outcome is not None:
            notice.outcome_amount_paise = outcome
    if errors:
        return errors
    old = notice.status
    notice.status = to
    note = (form.get("note") or "").strip() or None
    notice.history.append(NoticeHistory(user_id=user.id, from_status=old, to_status=to, note=note))
    return []


def _log(user, notice, before, summary):
    after = _snapshot(notice)
    changes = [(f"notice_{k}", before.get(k, ""), v) for k, v in after.items() if before.get(k, "") != v]
    if changes:
        log_event(user, "notice", client=notice.client, summary=summary, changes=changes)


@bp.route("/")
def index():
    if not can_view(g.user):
        abort(403)
    query = visible_notices(g.user)
    state = request.args.get("state", "open")
    if state == "open":
        query = query.where(Notice.status.not_in(CLOSED))
    elif state == "closed":
        query = query.where(Notice.status.in_(CLOSED))
    if request.args.get("department"):
        query = query.where(Notice.department == request.args["department"])
    if request.args.get("assigned", type=int):
        query = query.where(Notice.assigned_to_id == request.args.get("assigned", type=int))
    q = request.args.get("q", "").strip()
    if q:
        like = f"%{q}%"
        query = query.join(Client).where(or_(Notice.reference_no.ilike(like), Client.name.ilike(like),
                                             Client.code.ilike(like), Client.pan.ilike(like)))
    notices = db.session.execute(query.order_by(Notice.reply_due_date, Notice.id)).scalars().all()
    return render_template("notices/index.html", notices=notices, args=request.args, departments=DEPARTMENTS,
                           staff=_assignable(), awaiting=AWAITING_REPLY, closed=CLOSED,
                           can_create=g.user.role in (PARTNER, MANAGER, STAFF))


def _form_page(notice, client, clients):
    return render_template("notices/form.html", notice=notice, client=client, clients=clients,
                           departments=DEPARTMENTS, types=NOTICE_TYPES, staff=_assignable(),
                           values=request.form if request.method == "POST" else None)


@bp.route("/new", methods=["GET", "POST"])
def new():
    if g.user.role not in (PARTNER, MANAGER, STAFF):
        abort(403)
    clients = db.session.execute(visible_clients(g.user).order_by(Client.code)).scalars().all()
    client_id = request.values.get("client_id", type=int)
    client = get_visible_client_or_404(g.user, client_id) if client_id else None
    if request.method == "POST":
        if client is None or not can_edit(g.user, client=client):
            flash("Choose one of your clients.", "error")
            return _form_page(None, client, clients)
        notice = Notice(client=client, status="Received")
        with db.session.no_autoflush:
            errors = validate_fields(request.form, notice)
        if errors:
            for e in errors:
                flash(e, "error")
            return _form_page(None, client, clients)
        notice.history.append(NoticeHistory(user_id=g.user.id, to_status="Received", note="Notice recorded"))
        db.session.add(notice)
        db.session.flush()
        _log(g.user, notice, {}, f"Recorded {notice.department} notice {notice.notice_type} ({notice.reference_no})")
        db.session.commit()
        return redirect(url_for("notices.detail", notice_id=notice.id))
    return _form_page(None, client, clients)


@bp.route("/<int:notice_id>")
def detail(notice_id):
    notice = _get(notice_id)
    return render_template("notices/detail.html", n=notice, statuses=STATUSES, doc_kinds=DOC_KINDS,
                           can_edit=can_edit(g.user, notice), can_status=can_change_status(g.user, notice),
                           awaiting=AWAITING_REPLY, closed=CLOSED)


@bp.route("/<int:notice_id>/edit", methods=["GET", "POST"])
def edit(notice_id):
    notice = _get(notice_id)
    if not can_edit(g.user, notice):
        abort(403)
    if request.method == "POST":
        before = _snapshot(notice)
        with db.session.no_autoflush:
            errors = validate_fields(request.form, notice)
        if errors:
            db.session.rollback()
            for e in errors:
                flash(e, "error")
        else:
            _log(g.user, notice, before, f"Edited notice {notice.reference_no}")
            db.session.commit()
            return redirect(url_for("notices.detail", notice_id=notice.id))
    return _form_page(notice, notice.client, None)


@bp.route("/<int:notice_id>/status", methods=["POST"])
def change_status(notice_id):
    notice = _get(notice_id)
    if not can_change_status(g.user, notice):
        abort(403)
    before = _snapshot(notice)
    errors = apply_status(notice, request.form.get("status", ""), request.form, g.user)
    if errors:
        db.session.rollback()
        for e in errors:
            flash(e, "error")
    else:
        _log(g.user, notice, before, f"Notice {notice.reference_no}: {before['status']} → {notice.status}")
        db.session.commit()
        flash(f"Status is now {notice.status}.", "success")
    return redirect(url_for("notices.detail", notice_id=notice.id))


def _upload_dir():
    path = os.path.join(current_app.instance_path, "uploads", "notices")
    os.makedirs(path, exist_ok=True)
    return path


@bp.route("/<int:notice_id>/documents", methods=["POST"])
def upload(notice_id):
    notice = _get(notice_id)
    if not can_change_status(g.user, notice):
        abort(403)
    kind = request.form.get("kind", "")
    upload_file = request.files.get("file")
    if kind not in DOC_KINDS or not upload_file or not upload_file.filename:
        flash("Choose the document type and a PDF file.", "error")
        return redirect(url_for("notices.detail", notice_id=notice.id))
    data = upload_file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        flash("PDF is larger than 15 MB.", "error")
    elif not data.startswith(b"%PDF-"):
        flash("Only PDF files can be uploaded.", "error")
    else:
        stored = secrets.token_hex(16) + ".pdf"
        with open(os.path.join(_upload_dir(), stored), "wb") as fh:
            fh.write(data)
        name = secure_filename(upload_file.filename) or f"{kind}.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        notice.documents.append(NoticeDocument(kind=kind, filename=name, stored_name=stored, size=len(data),
                                               uploaded_by_id=g.user.id))
        log_event(g.user, "notice.document", client=notice.client,
                  summary=f"Uploaded {DOC_KINDS[kind].lower()} PDF to notice {notice.reference_no}",
                  changes=[("notice_document", "", name)])
        db.session.commit()
        flash(f"Uploaded {name}.", "success")
    return redirect(url_for("notices.detail", notice_id=notice.id))


@bp.route("/<int:notice_id>/documents/<int:doc_id>")
def download(notice_id, doc_id):
    notice = _get(notice_id)
    doc = next((d for d in notice.documents if d.id == doc_id), None) or abort(404)
    return send_file(os.path.join(_upload_dir(), doc.stored_name), mimetype="application/pdf",
                     as_attachment=True, download_name=doc.filename)


@bp.route("/<int:notice_id>/documents/<int:doc_id>/delete", methods=["POST"])
def delete_document(notice_id, doc_id):
    notice = _get(notice_id)
    if not can_edit(g.user, notice):
        abort(403)
    doc = next((d for d in notice.documents if d.id == doc_id), None) or abort(404)
    path = os.path.join(_upload_dir(), doc.stored_name)
    notice.documents.remove(doc)
    log_event(g.user, "notice.document", client=notice.client,
              summary=f"Deleted document from notice {notice.reference_no}", changes=[("notice_document", doc.filename, "")])
    db.session.commit()
    if os.path.exists(path):
        os.remove(path)
    return redirect(url_for("notices.detail", notice_id=notice.id))


def client_notices(user, client):
    if not can_view(user):
        return []
    return db.session.execute(visible_notices(user).where(Notice.client_id == client.id)
                              .order_by(Notice.reply_due_date.desc())).scalars().all()
