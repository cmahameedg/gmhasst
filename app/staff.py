"""Staff screen (Partner only): invite, set role, assign clients, deactivate leavers."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import or_

from .activity import log_event
from .auth import new_invite_token
from .clientsvc import save_client, validate_client
from .models import Client, User, db, utcnow
from .permissions import (
    ASSIGNABLE_ROLES,
    PARTNER,
    ROLE_LABELS,
    ROLES,
    can_manage_staff,
)
from .validators import is_valid_email

bp = Blueprint("staff", __name__, url_prefix="/staff")


@bp.before_request
def partner_only():
    if not can_manage_staff(g.user):
        abort(403)


def _get_user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    return user


def _clients_of(user):
    return db.session.execute(
        db.select(Client)
        .where(or_(Client.primary_staff_id == user.id, Client.reviewer_staff_id == user.id))
        .order_by(Client.code)
    ).scalars().all()


def _active_partners():
    return db.session.execute(
        db.select(db.func.count()).select_from(User).where(User.role == PARTNER, User.active.is_(True))
    ).scalar()


def _invite_url(token):
    return url_for("auth.accept_invite", token=token, _external=True)


@bp.route("/")
def index():
    users = db.session.execute(db.select(User).order_by(User.active.desc(), User.name)).scalars().all()
    counts = {}
    for user in users:
        counts[user.id] = db.session.execute(
            db.select(db.func.count())
            .select_from(Client)
            .where(or_(Client.primary_staff_id == user.id, Client.reviewer_staff_id == user.id))
        ).scalar()
    return render_template("staff/index.html", users=users, counts=counts, roles=ROLES)


@bp.route("/invite", methods=["POST"])
def invite():
    email = request.form.get("email", "").strip().lower()
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "")
    if not is_valid_email(email) or not name or role not in ROLES:
        flash("Enter a name, a valid email and a role.", "error")
        return redirect(url_for("staff.index"))
    if db.session.execute(db.select(User).where(User.email == email)).scalar_one_or_none():
        flash(f"{email} already has an account.", "error")
        return redirect(url_for("staff.index"))
    user = User(email=email, name=name, role=role, active=True)
    token = new_invite_token(user)
    db.session.add(user)
    db.session.flush()
    log_event(
        g.user, "staff.invite", subject=user, summary=f"Invited {name} as {ROLE_LABELS[role]}",
        changes=[("role", "", ROLE_LABELS[role])],
    )
    db.session.commit()
    return render_template("staff/invite_link.html", invitee=user, link=_invite_url(token))


@bp.route("/<int:user_id>")
def detail(user_id):
    user = _get_user(user_id)
    all_clients = db.session.execute(db.select(Client).order_by(Client.code)).scalars().all()
    return render_template(
        "staff/detail.html",
        member=user,
        clients=_clients_of(user),
        all_clients=all_clients,
        roles=ROLES,
        assignable=user.role in ASSIGNABLE_ROLES and user.active,
    )


@bp.route("/<int:user_id>/role", methods=["POST"])
def set_role(user_id):
    user = _get_user(user_id)
    role = request.form.get("role", "")
    if role not in ROLES or role == user.role:
        return redirect(url_for("staff.detail", user_id=user.id))
    if user.role == PARTNER and user.active and _active_partners() <= 1:
        flash("There must always be at least one active Partner.", "error")
    elif role not in ASSIGNABLE_ROLES and _clients_of(user):
        flash(f"Reassign {user.name}'s clients first: a {ROLE_LABELS[role]} cannot hold clients.", "error")
    else:
        old = user.role
        user.role = role
        log_event(
            g.user, "staff.role", subject=user, summary=f"Changed role of {user.name}",
            changes=[("role", ROLE_LABELS[old], ROLE_LABELS[role])],
        )
        db.session.commit()
        flash(f"{user.name} is now {ROLE_LABELS[role]}.", "success")
    return redirect(url_for("staff.detail", user_id=user.id))


@bp.route("/<int:user_id>/assign", methods=["POST"])
def assign(user_id):
    user = _get_user(user_id)
    slot = request.form.get("slot")
    client = db.session.get(Client, request.form.get("client_id", type=int) or 0)
    if client is None or slot not in ("primary_staff", "reviewer_staff"):
        abort(400)
    clean, errors = validate_client({slot: str(user.id)}, {slot}, client)
    if errors:
        flash("; ".join(errors.values()), "error")
    else:
        save_client(clean, g.user, client, action="client.assign", summary=f"Assigned {user.name}")
        db.session.commit()
        flash(f"{user.name} assigned to {client.code}.", "success")
    return redirect(url_for("staff.detail", user_id=user.id))


@bp.route("/<int:user_id>/unassign", methods=["POST"])
def unassign(user_id):
    user = _get_user(user_id)
    slot = request.form.get("slot")
    client = db.session.get(Client, request.form.get("client_id", type=int) or 0)
    if client is None or slot not in ("primary_staff", "reviewer_staff"):
        abort(400)
    if getattr(client, slot + "_id") == user.id:
        save_client({slot: None}, g.user, client, action="client.assign", summary=f"Unassigned {user.name}")
        db.session.commit()
    return redirect(url_for("staff.detail", user_id=user.id))


@bp.route("/<int:user_id>/reset", methods=["POST"])
def reset_access(user_id):
    """Lost phone or forgotten password: wipe credentials and issue a fresh invite link."""
    user = _get_user(user_id)
    if not user.active:
        abort(400)
    user.password_hash = None
    user.totp_secret = None
    user.totp_enabled = False
    user.totp_last_counter = None
    token = new_invite_token(user)
    log_event(g.user, "staff.reset", subject=user, summary=f"Reset password and 2FA for {user.name}")
    db.session.commit()
    return render_template("staff/invite_link.html", invitee=user, link=_invite_url(token), reset=True)


@bp.route("/<int:user_id>/deactivate", methods=["GET", "POST"])
def deactivate(user_id):
    """Deactivate a leaver. Every client they hold must be handed to someone else first."""
    user = _get_user(user_id)
    if not user.active:
        return redirect(url_for("staff.detail", user_id=user.id))
    if user.id == g.user.id:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("staff.detail", user_id=user.id))
    if user.role == PARTNER and _active_partners() <= 1:
        flash("There must always be at least one active Partner.", "error")
        return redirect(url_for("staff.detail", user_id=user.id))

    clients = _clients_of(user)
    candidates = [
        u for u in db.session.execute(
            db.select(User).where(User.active.is_(True), User.role.in_(ASSIGNABLE_ROLES)).order_by(User.name)
        ).scalars()
        if u.id != user.id
    ]
    errors, chosen = {}, {}

    if request.method == "POST":
        default_primary = request.form.get("default_primary", "")
        default_reviewer = request.form.get("default_reviewer", "")
        plans = []
        for client in clients:
            raw = {}
            if client.primary_staff_id == user.id:
                raw["primary_staff"] = request.form.get(f"primary_{client.id}") or default_primary
            if client.reviewer_staff_id == user.id:
                raw["reviewer_staff"] = request.form.get(f"reviewer_{client.id}") or default_reviewer
            chosen[client.id] = raw
            missing = [slot for slot, val in raw.items() if not val]
            if missing or str(user.id) in raw.values():
                errors[client.id] = "Choose who takes over"
                continue
            clean, problems = validate_client(raw, set(raw), client)
            if problems:
                errors[client.id] = "; ".join(problems.values())
            else:
                plans.append((client, clean))

        if not errors:
            for client, clean in plans:
                save_client(clean, g.user, client, action="client.reassign",
                            summary=f"Reassigned from leaver {user.name}")
            user.active = False
            user.deactivated_at = utcnow()
            user.invite_token_hash = None
            log_event(
                g.user, "staff.deactivate", subject=user,
                summary=f"Deactivated {user.name}; {len(plans)} client(s) reassigned",
                changes=[("active", "Yes", "No")],
            )
            db.session.commit()
            flash(f"{user.name} deactivated and {len(plans)} client(s) reassigned.", "success")
            return redirect(url_for("staff.index"))
        flash("Every client needs a new owner before the account can be closed.", "error")

    return render_template(
        "staff/deactivate.html", member=user, clients=clients, candidates=candidates,
        errors=errors, chosen=chosen,
    )


@bp.route("/<int:user_id>/reactivate", methods=["POST"])
def reactivate(user_id):
    user = _get_user(user_id)
    if not user.active:
        user.active = True
        user.deactivated_at = None
        log_event(g.user, "staff.reactivate", subject=user, summary=f"Reactivated {user.name}",
                  changes=[("active", "No", "Yes")])
        db.session.commit()
    return redirect(url_for("staff.detail", user_id=user.id))
