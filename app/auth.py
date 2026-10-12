"""Login with password + authenticator-app code (TOTP), and invite acceptance."""
import hashlib
import io
import secrets
import time
from datetime import timedelta

import pyotp
import qrcode
import qrcode.image.svg
from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .activity import log_event
from .models import User, db, utcnow

bp = Blueprint("auth", __name__)

PASSWORD_MIN_LENGTH = 10
MAX_FAILED_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)
INVITE_VALIDITY = timedelta(days=7)

# Endpoints reachable without a fully authenticated session.
PUBLIC_ENDPOINTS = {"auth.login", "auth.verify", "auth.setup_2fa", "auth.accept_invite", "static", "dashboard.feed"}


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_invite_token(user):
    token = secrets.token_urlsafe(32)
    user.invite_token_hash = hash_token(token)
    user.invite_expires_at = utcnow() + INVITE_VALIDITY
    return token


def load_user():
    """Before every request: load the logged-in user, or send them to the login page."""
    g.user = None
    uid = session.get("uid")
    if uid:
        user = db.session.get(User, uid)
        # A deactivated user is logged out on their very next request.
        if user and user.active and user.totp_enabled and not user.is_pending:
            g.user = user
        else:
            session.clear()
    if g.user is None and request.endpoint not in PUBLIC_ENDPOINTS:
        return redirect(url_for("auth.login"))
    return None


def _pending_user():
    uid = session.get("pre_uid")
    started = session.get("pre_at", 0)
    if not uid or time.time() - started > 600:
        return None
    user = db.session.get(User, uid)
    return user if user and user.active and not user.is_pending else None


def _locked(user):
    return user.locked_until is not None and user.locked_until > utcnow()


def _record_failure(user):
    user.failed_logins += 1
    if user.failed_logins >= MAX_FAILED_ATTEMPTS:
        user.locked_until = utcnow() + LOCKOUT
        user.failed_logins = 0
    db.session.commit()


def _finish_login(user):
    user.failed_logins = 0
    user.locked_until = None
    session.clear()
    session["uid"] = user.id
    session.permanent = True
    log_event(user, "auth.login", subject=user, summary="Logged in")
    db.session.commit()
    return redirect(url_for("dashboard.today_view"))


def _check_totp(user, code):
    """Accept a code within one 30-second step either side, and never the same step twice."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6 or not user.totp_secret:
        return False
    totp = pyotp.TOTP(user.totp_secret)
    now = time.time()
    for offset in (0, -1, 1):
        at = now + offset * totp.interval
        if secrets.compare_digest(totp.at(at), code):
            counter = int(at // totp.interval)
            if user.totp_last_counter is not None and counter <= user.totp_last_counter:
                return False
            user.totp_last_counter = counter
            return True
    return False


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("clients.index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.session.execute(db.select(User).where(User.email == email)).scalar_one_or_none()
        if user and _locked(user):
            flash("Too many failed attempts. Try again in 15 minutes.", "error")
        elif user and user.active and user.check_password(password):
            session.clear()
            session["pre_uid"] = user.id
            session["pre_at"] = time.time()
            if user.totp_enabled:
                return redirect(url_for("auth.verify"))
            return redirect(url_for("auth.setup_2fa"))
        else:
            if user:
                _record_failure(user)
            flash("Email or password is incorrect.", "error")
    return render_template("auth/login.html")


@bp.route("/login/verify", methods=["GET", "POST"])
def verify():
    user = _pending_user()
    if user is None or not user.totp_enabled:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if _locked(user):
            session.clear()
            flash("Too many failed attempts. Try again in 15 minutes.", "error")
            return redirect(url_for("auth.login"))
        if _check_totp(user, request.form.get("code")):
            return _finish_login(user)
        _record_failure(user)
        flash("That code is not valid. Check your authenticator app and try again.", "error")
    return render_template("auth/verify.html")


@bp.route("/login/setup-2fa", methods=["GET", "POST"])
def setup_2fa():
    user = _pending_user()
    if user is None or user.totp_enabled:
        return redirect(url_for("auth.login"))
    if not user.totp_secret:
        user.totp_secret = pyotp.random_base32()
        db.session.commit()
    if request.method == "POST":
        if _check_totp(user, request.form.get("code")):
            user.totp_enabled = True
            log_event(user, "auth.2fa_enabled", subject=user, summary="Set up two-factor authentication")
            return _finish_login(user)
        flash("That code is not valid. Scan the QR code again and enter the 6-digit code.", "error")

    uri = pyotp.TOTP(user.totp_secret).provisioning_uri(
        name=user.email, issuer_name=current_app.config["TOTP_ISSUER"]
    )
    buffer = io.BytesIO()
    qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8).save(buffer)
    return render_template(
        "auth/setup_2fa.html", qr_svg=buffer.getvalue().decode(), secret=user.totp_secret
    )


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/invite/<token>", methods=["GET", "POST"])
def accept_invite(token):
    user = db.session.execute(
        db.select(User).where(User.invite_token_hash == hash_token(token))
    ).scalar_one_or_none()
    if user is None or not user.active or user.invite_expires_at < utcnow():
        return render_template("error.html", code=404, message="This invite link is invalid or has expired. Ask a Partner for a new one."), 404

    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < PASSWORD_MIN_LENGTH:
            flash(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.", "error")
        elif password != request.form.get("confirm", ""):
            flash("The two passwords do not match.", "error")
        else:
            user.set_password(password)
            user.invite_token_hash = None
            user.invite_expires_at = None
            user.totp_secret = None
            user.totp_enabled = False
            user.totp_last_counter = None
            log_event(user, "auth.invite_accepted", subject=user, summary="Accepted invite and set password")
            db.session.commit()
            session.clear()
            session["pre_uid"] = user.id
            session["pre_at"] = time.time()
            return redirect(url_for("auth.setup_2fa"))
    return render_template("auth/invite.html", invitee=user)
