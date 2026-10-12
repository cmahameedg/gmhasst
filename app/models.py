from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    password_hash = db.Column(db.String(255))
    active = db.Column(db.Boolean, nullable=False, default=True)

    totp_secret = db.Column(db.String(64))
    totp_enabled = db.Column(db.Boolean, nullable=False, default=False)
    # Last accepted TOTP time-step, so a code cannot be replayed.
    totp_last_counter = db.Column(db.Integer)

    # Invites store only a hash of the token sent to the invitee.
    invite_token_hash = db.Column(db.String(64))
    invite_expires_at = db.Column(db.DateTime)

    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    deactivated_at = db.Column(db.DateTime)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    @property
    def is_pending(self):
        return self.password_hash is None

    def __repr__(self):
        return f"<User {self.email} {self.role}>"


class Counter(db.Model):
    """Monotonic counters. Client codes come from here so they are never reused."""

    __tablename__ = "counters"

    name = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.Integer, nullable=False, default=0)

    @classmethod
    def next(cls, name):
        counter = db.session.get(cls, name)
        if counter is None:
            counter = cls(name=name, value=0)
            db.session.add(counter)
        counter.value += 1
        db.session.flush()
        return counter.value


class ClientGroup(db.Model):
    __tablename__ = "client_groups"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)


client_services = db.Table(
    "client_services",
    db.Column("client_id", db.Integer, db.ForeignKey("clients.id"), primary_key=True),
    db.Column("service", db.String(40), primary_key=True),
)


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    trade_name = db.Column(db.String(200))
    entity_type = db.Column(db.String(40), nullable=False)
    pan = db.Column(db.String(10), unique=True, nullable=False)
    tan = db.Column(db.String(10))
    cin_llpin = db.Column(db.String(21))
    udyam = db.Column(db.String(19))
    iec = db.Column(db.String(10))
    dob_incorporation = db.Column(db.Date, nullable=False)
    contact_person = db.Column(db.String(120))
    mobile = db.Column(db.String(10))
    email = db.Column(db.String(255))
    address = db.Column(db.Text)
    district = db.Column(db.String(80))
    state = db.Column(db.String(60))
    group_id = db.Column(db.Integer, db.ForeignKey("client_groups.id"))
    primary_staff_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewer_staff_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    onboarding_source = db.Column(db.String(40))
    onboarding_date = db.Column(db.Date, nullable=False)
    fee_basis = db.Column(db.String(40))
    status = db.Column(db.String(20), nullable=False, default="Active")
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    group = db.relationship("ClientGroup", backref="clients")
    primary_staff = db.relationship("User", foreign_keys=[primary_staff_id])
    reviewer_staff = db.relationship("User", foreign_keys=[reviewer_staff_id])
    gstins = db.relationship(
        "Gstin", backref="client", cascade="all, delete-orphan", order_by="Gstin.gstin"
    )

    @property
    def services(self):
        rows = db.session.execute(
            db.select(client_services.c.service).where(client_services.c.client_id == self.id)
        ).scalars()
        return sorted(rows)

    def set_services(self, services):
        db.session.execute(client_services.delete().where(client_services.c.client_id == self.id))
        for service in services:
            db.session.execute(client_services.insert().values(client_id=self.id, service=service))


class Gstin(db.Model):
    __tablename__ = "gstins"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    gstin = db.Column(db.String(15), unique=True, nullable=False)


class ActivityEvent(db.Model):
    """One user action. Field-level old/new values hang off it as ActivityChange rows."""

    __tablename__ = "activity_events"

    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), index=True)
    subject_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(40), nullable=False)
    summary = db.Column(db.String(500))

    user = db.relationship("User", foreign_keys=[user_id])
    subject_user = db.relationship("User", foreign_keys=[subject_user_id])
    client = db.relationship("Client")
    changes = db.relationship(
        "ActivityChange", backref="event", cascade="all, delete-orphan", order_by="ActivityChange.id"
    )


class ActivityChange(db.Model):
    __tablename__ = "activity_changes"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("activity_events.id"), nullable=False)
    field = db.Column(db.String(40), nullable=False)
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)


class ImportBatch(db.Model):
    """A parsed CSV waiting for confirmation. Rows are stored as JSON."""

    __tablename__ = "import_batches"
    __table_args__ = (UniqueConstraint("token"),)

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    filename = db.Column(db.String(255))
    rows_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    confirmed_at = db.Column(db.DateTime)
