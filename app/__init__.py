import os
import secrets
from datetime import timedelta

import click
from flask import Flask, g, render_template
from flask_wtf.csrf import CSRFProtect

from .models import User, db

csrf = CSRFProtect()


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    secret = os.environ.get("SECRET_KEY")
    if not secret:
        secret_file = os.path.join(app.instance_path, "secret_key")
        if not os.path.exists(secret_file):
            with open(secret_file, "w") as fh:
                fh.write(secrets.token_hex(32))
            os.chmod(secret_file, 0o600)
        with open(secret_file) as fh:
            secret = fh.read().strip()

    app.config.update(
        SECRET_KEY=secret,
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "sqlite:///" + os.path.join(app.instance_path, "gmh.db")
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=10),
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
        TIMEZONE=os.environ.get("TIMEZONE", "Asia/Kolkata"),
        TOTP_ISSUER=os.environ.get("TOTP_ISSUER", "GMH Practice"),
        WTF_CSRF_TIME_LIMIT=None,
        BASE_URL=os.environ.get("BASE_URL", "http://localhost:5000"),
        MAIL_BACKEND=os.environ.get("MAIL_BACKEND", "smtp" if os.environ.get("SMTP_HOST") else "file"),
        MAIL_FROM=os.environ.get("MAIL_FROM", "GMH Practice <noreply@localhost>"),
        SMTP_HOST=os.environ.get("SMTP_HOST"),
        SMTP_PORT=os.environ.get("SMTP_PORT", "587"),
        SMTP_USER=os.environ.get("SMTP_USER"),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD"),
        # Monday=0 ... Sunday=6. Default: Monday to Saturday.
        WORKING_DAYS=os.environ.get("WORKING_DAYS", "0,1,2,3,4,5"),
        HOLIDAYS=os.environ.get("HOLIDAYS", ""),  # comma-separated YYYY-MM-DD
    )
    if config:
        app.config.update(config)

    db.init_app(app)
    csrf.init_app(app)

    from . import activity, auth, board, clients, dashboard, imports, notices, registers, returns, staff

    app.register_blueprint(auth.bp)
    app.register_blueprint(clients.bp)
    app.register_blueprint(staff.bp)
    app.register_blueprint(imports.bp)
    app.register_blueprint(activity.bp)
    app.register_blueprint(returns.bp)
    app.register_blueprint(board.bp)
    app.register_blueprint(notices.bp)
    app.register_blueprint(registers.bp)
    app.register_blueprint(dashboard.bp)
    app.before_request(auth.load_user)

    from . import permissions as perms
    from .timeutil import to_local

    @app.context_processor
    def inject():
        return {"perms": perms, "current_user": g.get("user"), "ROLE_LABELS": perms.ROLE_LABELS}

    @app.template_filter("localtime")
    def localtime(dt, fmt="%d-%m-%Y %H:%M"):
        local = to_local(dt)
        return local.strftime(fmt) if local else ""

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(403)
    def forbidden(_):
        return render_template("error.html", code=403, message="You do not have access to this page."), 403

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", code=404, message="Not found."), 404

    with app.app_context():
        db.create_all()
        returns.seed_return_master()

    register_cli(app)
    return app


def register_cli(app):
    @app.cli.command("create-partner")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    @click.password_option()
    def create_partner(email, name, password):
        """Create the first Partner account (2FA is set up at first login)."""
        from .auth import PASSWORD_MIN_LENGTH
        from .permissions import PARTNER

        if len(password) < PASSWORD_MIN_LENGTH:
            raise click.ClickException(f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        email = email.strip().lower()
        if db.session.execute(db.select(User).where(User.email == email)).scalar_one_or_none():
            raise click.ClickException(f"{email} already exists")
        user = User(email=email, name=name.strip(), role=PARTNER)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Partner {email} created. Log in and scan the QR code to enable 2FA.")

    @app.cli.command("send-morning-emails")
    @click.option("--date", "day", default=None, help="YYYY-MM-DD (default: today in TIMEZONE)")
    @click.option("--force", is_flag=True, help="Send even on a non-working day or if already sent")
    @click.option("--dry-run", is_flag=True, help="Show what would be sent")
    def send_morning_emails(day, force, dry_run):
        """Run at 8:00 AM IST on working days (see README for the cron line)."""
        from .morning import is_working_day, send_all
        from .timeutil import today
        from .validators import parse_date

        on = parse_date(day) if day else today()
        if on is None:
            raise click.ClickException("Date must be YYYY-MM-DD")
        if not force and not is_working_day(on):
            click.echo(f"{on} is not a working day; nothing sent.")
            return
        with app.test_request_context(base_url=app.config["BASE_URL"]):
            for user, outcome in send_all(on, force=force, dry_run=dry_run):
                click.echo(f"{user.email}: {outcome}")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Create one demo user per role (for trying the dummy CSV), printing invite links."""
        from .auth import new_invite_token
        from .permissions import ARTICLE, BILLING, MANAGER, READONLY, STAFF

        demo = [
            ("meena.manager@demo.local", "Meena (Manager)", MANAGER),
            ("ravi.staff@demo.local", "Ravi (Staff)", STAFF),
            ("anita.article@demo.local", "Anita (Article)", ARTICLE),
            ("bala.billing@demo.local", "Bala (Billing)", BILLING),
            ("rekha.readonly@demo.local", "Rekha (Read-only)", READONLY),
        ]
        for email, name, role in demo:
            if db.session.execute(db.select(User).where(User.email == email)).scalar_one_or_none():
                click.echo(f"{email} exists, skipped")
                continue
            user = User(email=email, name=name, role=role)
            token = new_invite_token(user)
            db.session.add(user)
            click.echo(f"{name:22} /invite/{token}")
        db.session.commit()
