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


# --- Phase 2: compliance -----------------------------------------------------

registration_returns = db.Table(
    "registration_returns",
    db.Column("registration_id", db.Integer, db.ForeignKey("registrations.id"), primary_key=True),
    db.Column("return_type_id", db.Integer, db.ForeignKey("return_types.id"), primary_key=True),
)


class ReturnType(db.Model):
    """A row of the Return Master. All due-date knowledge lives here, never in code."""

    __tablename__ = "return_types"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    # Form names under the Income-tax Act, 1961 and the Income-tax Act, 2025, both searchable.
    old_name = db.Column(db.String(120))
    new_name = db.Column(db.String(120))
    category = db.Column(db.String(30), nullable=False)
    frequency = db.Column(db.String(20), nullable=False)
    # "period": due_day of the month that is due_month_offset months after the period end.
    # "event": event_days after an event date entered on the filing (e.g. AGM).
    due_basis = db.Column(db.String(10), nullable=False, default="period")
    due_day = db.Column(db.Integer)
    due_month_offset = db.Column(db.Integer)
    event_label = db.Column(db.String(40))
    event_days = db.Column(db.Integer)
    # Comma-separated registration kinds/schemes, e.g. "GST:Monthly" or "TAN".
    applies_to = db.Column(db.String(200), nullable=False)
    default_on = db.Column(db.Boolean, nullable=False, default=True)
    requires_everify = db.Column(db.Boolean, nullable=False, default=False)
    portal_url = db.Column(db.String(255))
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=100)

    overrides = db.relationship("DueOverride", backref="return_type", cascade="all, delete-orphan",
                                order_by="DueOverride.period_code")
    extensions = db.relationship("DueExtension", backref="return_type", cascade="all, delete-orphan",
                                 order_by="DueExtension.period_end.desc()")

    @property
    def applies_list(self):
        return [a.strip() for a in self.applies_to.split(",") if a.strip()]

    @property
    def display_name(self):
        names = [self.name] + [n for n in (self.old_name, self.new_name) if n]
        return " / ".join(dict.fromkeys(names))


class DueOverride(db.Model):
    """A standing exception, e.g. TDS payment for March is due 30 April, not 7 April."""

    __tablename__ = "due_overrides"

    id = db.Column(db.Integer, primary_key=True)
    return_type_id = db.Column(db.Integer, db.ForeignKey("return_types.id"), nullable=False)
    period_code = db.Column(db.String(4), nullable=False)
    due_day = db.Column(db.Integer, nullable=False)
    due_month_offset = db.Column(db.Integer, nullable=False)


class DueExtension(db.Model):
    """A one-off extension notified for a single period."""

    __tablename__ = "due_extensions"

    id = db.Column(db.Integer, primary_key=True)
    return_type_id = db.Column(db.Integer, db.ForeignKey("return_types.id"), nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    note = db.Column(db.String(200))


class Registration(db.Model):
    __tablename__ = "registrations"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False)
    identifier = db.Column(db.String(30), nullable=False)
    scheme = db.Column(db.String(30))
    active_from = db.Column(db.Date)
    active_to = db.Column(db.Date)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    client = db.relationship("Client", backref=db.backref("registrations", order_by="Registration.id"))
    return_types = db.relationship("ReturnType", secondary=registration_returns, order_by="ReturnType.sort_order")

    @property
    def tag(self):
        return f"{self.kind}:{self.scheme}" if self.scheme else self.kind


class Filing(db.Model):
    __tablename__ = "filings"
    __table_args__ = (UniqueConstraint("registration_id", "return_type_id", "period_end"),)

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    registration_id = db.Column(db.Integer, db.ForeignKey("registrations.id"), nullable=False)
    return_type_id = db.Column(db.Integer, db.ForeignKey("return_types.id"), nullable=False, index=True)
    gen_month = db.Column(db.String(7), nullable=False, index=True)  # "2026-10"
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    period_label = db.Column(db.String(60), nullable=False)
    period_code = db.Column(db.String(4), nullable=False)
    due_date = db.Column(db.Date)  # None while waiting for an event date (AGM)
    event_date = db.Column(db.Date)
    status = db.Column(db.String(20), nullable=False, default="not_started", index=True)
    nil_kind = db.Column(db.String(20))  # "Nil" or "Not applicable"
    maker_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    arn = db.Column(db.String(60))
    filed_on = db.Column(db.Date)
    # Money is held in paise to avoid floating-point rounding.
    tax_paid_paise = db.Column(db.Integer)
    late_fee_paise = db.Column(db.Integer)
    interest_paise = db.Column(db.Integer)
    challan_ref = db.Column(db.String(60))
    ack_saved = db.Column(db.Boolean, nullable=False, default=False)
    everified = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    client = db.relationship("Client")
    registration = db.relationship("Registration")
    return_type = db.relationship("ReturnType")
    maker = db.relationship("User", foreign_keys=[maker_id])
    reviewer = db.relationship("User", foreign_keys=[reviewer_id])
    history = db.relationship("FilingHistory", backref="filing", cascade="all, delete-orphan",
                              order_by="FilingHistory.id")
    comments = db.relationship("FilingComment", backref="filing", cascade="all, delete-orphan",
                               order_by="FilingComment.id")


class FilingHistory(db.Model):
    __tablename__ = "filing_history"

    id = db.Column(db.Integer, primary_key=True)
    filing_id = db.Column(db.Integer, db.ForeignKey("filings.id"), nullable=False, index=True)
    at = db.Column(db.DateTime, nullable=False, default=utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    from_status = db.Column(db.String(20))
    to_status = db.Column(db.String(20), nullable=False)
    note = db.Column(db.Text)

    user = db.relationship("User")


class FilingComment(db.Model):
    __tablename__ = "filing_comments"

    id = db.Column(db.Integer, primary_key=True)
    filing_id = db.Column(db.Integer, db.ForeignKey("filings.id"), nullable=False, index=True)
    at = db.Column(db.DateTime, nullable=False, default=utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)

    user = db.relationship("User")
    mentions = db.relationship("FilingMention", backref="comment", cascade="all, delete-orphan")


class FilingMention(db.Model):
    __tablename__ = "filing_mentions"

    id = db.Column(db.Integer, primary_key=True)
    comment_id = db.Column(db.Integer, db.ForeignKey("filing_comments.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    seen = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship("User")
