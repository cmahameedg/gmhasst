import csv
import io

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import or_

from .activity import log_event
from .clientsvc import CHOICES, REQUIRED_FIELDS, snapshot, save_client, validate_client
from .constants import CIN_ENTITY_TYPES, ENTITY_TYPES, SERVICES, STATES, STATUSES
from .models import Client, ClientGroup, Gstin, User, client_services, db
from .permissions import (
    ASSIGNABLE_ROLES,
    BILLING,
    FIELDS,
    can_create_clients,
    can_see_fees,
    editable_fields,
    get_visible_client_or_404,
    ordered,
    sees_all_clients,
    visible_clients,
    visible_fields,
)

bp = Blueprint("clients", __name__)

PAGE_SIZE = 50
TABS = ["overview", "filings", "notices", "fees", "documents"]


def _filtered_query(user, args):
    query = visible_clients(user)
    q = (args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        gst_match = db.select(Gstin.client_id).where(Gstin.gstin.ilike(like))
        conditions = [
            Client.name.ilike(like),
            Client.pan.ilike(like),
            Client.code.ilike(like),
            Client.id.in_(gst_match),
        ]
        if user.role != BILLING:
            conditions.append(Client.trade_name.ilike(like))
        query = query.where(or_(*conditions))

    # Billing can only see name, PAN and GSTIN, so it cannot filter on anything else.
    if user.role != BILLING:
        if args.get("entity_type"):
            query = query.where(Client.entity_type == args["entity_type"])
        if args.get("status"):
            query = query.where(Client.status == args["status"])
        if args.get("service"):
            has_service = db.select(client_services.c.client_id).where(
                client_services.c.service == args["service"]
            )
            query = query.where(Client.id.in_(has_service))
        staff_id = args.get("staff", type=int)
        if staff_id:
            query = query.where(
                or_(Client.primary_staff_id == staff_id, Client.reviewer_staff_id == staff_id)
            )
    return query.order_by(Client.code)


def _assignable_staff():
    return db.session.execute(
        db.select(User)
        .where(User.active.is_(True), User.role.in_(ASSIGNABLE_ROLES))
        .order_by(User.name)
    ).scalars().all()


@bp.route("/")
def index():
    user = g.user
    page = max(request.args.get("page", 1, type=int), 1)
    query = _filtered_query(user, request.args)
    total = db.session.execute(db.select(db.func.count()).select_from(query.subquery())).scalar()
    clients = db.session.execute(query.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).scalars().all()
    return render_template(
        "clients/index.html",
        clients=clients,
        total=total,
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        args=request.args,
        fields=visible_fields(user),
        entity_types=ENTITY_TYPES,
        services=SERVICES,
        statuses=STATUSES,
        staff=_assignable_staff(),
    )


def _csv_safe(value):
    """Stop spreadsheet apps treating a cell as a formula."""
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


@bp.route("/export.csv")
def export():
    user = g.user
    columns = ordered(visible_fields(user))
    clients = db.session.execute(_filtered_query(user, request.args)).scalars().all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for client in clients:
        values = snapshot(client, export=True)
        writer.writerow([_csv_safe(values[key]) for key in columns])
    log_event(user, "client.export", summary=f"Exported {len(clients)} clients to CSV")
    db.session.commit()
    return Response(
        "﻿" + out.getvalue(),  # BOM so Excel reads UTF-8 correctly
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=clients.csv"},
    )


@bp.route("/clients/<int:client_id>")
def profile(client_id):
    user = g.user
    client = get_visible_client_or_404(user, client_id)
    tab = request.args.get("tab", "overview")
    tabs = [t for t in TABS if t != "fees" or can_see_fees(user)]
    if tab not in tabs:
        abort(404)
    fields = visible_fields(user)
    group_members = []
    if client.group and "group" in fields:
        group_members = db.session.execute(
            visible_clients(user)
            .where(Client.group_id == client.group_id, Client.id != client.id)
            .order_by(Client.name)
        ).scalars().all()
    extra = {}
    if tab == "filings":
        from .board import client_filings_context

        extra = client_filings_context(user, client)
    elif tab == "notices":
        from .models import Dsc, RegisterDeadline
        from .notices import client_notices
        from .registers import can_edit as can_edit_registers

        extra = {
            "client_notices": client_notices(user, client),
            "client_dscs": db.session.execute(db.select(Dsc).where(Dsc.client_id == client.id)
                                              .order_by(Dsc.active.desc(), Dsc.expiry_date)).scalars().all(),
            "client_deadlines": db.session.execute(db.select(RegisterDeadline).where(RegisterDeadline.client_id == client.id)
                                                   .order_by(RegisterDeadline.due_date.desc())).scalars().all(),
            "can_edit_registers": can_edit_registers(user, client),
        }
    return render_template(
        "clients/profile.html",
        **extra,
        client=client,
        values=snapshot(client),
        fields=ordered(fields - {"fee_basis"}),
        tab=tab,
        tabs=tabs,
        labels=FIELDS,
        state_code=STATES.get(client.state or ""),
        group_members=group_members,
        can_edit=bool(editable_fields(user, client)),
    )


def _form_raw(form):
    raw = {key: form.get(key, "") for key in FIELDS}
    raw["services"] = form.getlist("services")
    return raw


def _render_form(client, values, editable, errors):
    user = g.user
    return render_template(
        "clients/form.html",
        client=client,
        values=values,
        editable=editable,
        shown=ordered(visible_fields(user) - {"code"}),
        errors=errors,
        labels=FIELDS,
        required=REQUIRED_FIELDS,
        choices=CHOICES,
        services=SERVICES,
        cin_types=sorted(CIN_ENTITY_TYPES),
        staff=_assignable_staff(),
        groups=db.session.execute(db.select(ClientGroup.name).order_by(ClientGroup.name)).scalars().all(),
        states=STATES,
    )


def _form_values(client):
    """Current values for pre-filling the form (staff as ids, dates as ISO)."""
    values = snapshot(client, export=True)
    values["primary_staff"] = str(client.primary_staff_id or "")
    values["reviewer_staff"] = str(client.reviewer_staff_id or "")
    values["gstins"] = "\n".join(g.gstin for g in client.gstins)
    values["services"] = client.services
    return values


@bp.route("/clients/new", methods=["GET", "POST"])
def new():
    user = g.user
    if not can_create_clients(user):
        abort(403)
    editable = editable_fields(user, None)
    if request.method == "POST":
        raw = _form_raw(request.form)
        clean, errors = validate_client(raw, editable, None)
        if not errors:
            client, _ = save_client(clean, user)
            db.session.commit()
            flash(f"Client {client.code} created.", "success")
            return redirect(url_for("clients.profile", client_id=client.id))
        return _render_form(None, raw, editable, errors)
    return _render_form(None, {"status": "Active", "services": []}, editable, {})


@bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
def edit(client_id):
    user = g.user
    client = get_visible_client_or_404(user, client_id)
    editable = editable_fields(user, client)
    if not editable:
        abort(403)
    if request.method == "POST":
        raw = _form_raw(request.form)
        clean, errors = validate_client(raw, editable, client, show_codes=sees_all_clients(user))
        if not errors:
            _, changes = save_client(clean, user, client)
            db.session.commit()
            flash(f"Saved {len(changes)} change(s)." if changes else "No changes.", "success")
            return redirect(url_for("clients.profile", client_id=client.id))
        values = _form_values(client)
        values.update({k: raw[k] for k in editable})
        return _render_form(client, values, editable, errors)
    return _render_form(client, _form_values(client), editable, {})
