import os
import sys

import pyotp
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.models import User, db  # noqa: E402
from app.permissions import ARTICLE, BILLING, MANAGER, PARTNER, READONLY, STAFF  # noqa: E402

PASSWORD = "correct-horse-battery"

DEMO_USERS = {
    PARTNER: ("priya.partner@demo.local", "Priya (Partner)"),
    MANAGER: ("meena.manager@demo.local", "Meena (Manager)"),
    STAFF: ("ravi.staff@demo.local", "Ravi (Staff)"),
    ARTICLE: ("anita.article@demo.local", "Anita (Article)"),
    BILLING: ("bala.billing@demo.local", "Bala (Billing)"),
    READONLY: ("rekha.readonly@demo.local", "Rekha (Read-only)"),
}


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
        "WTF_CSRF_ENABLED": False,
        "SESSION_COOKIE_SECURE": False,
        "SERVER_NAME": "localhost",
    })
    with app.app_context():
        for role, (email, name) in DEMO_USERS.items():
            user = User(email=email, name=name, role=role, totp_secret=pyotp.random_base32(), totp_enabled=True)
            user.set_password(PASSWORD)
            db.session.add(user)
        db.session.commit()
        yield app


def user_by_role(role):
    return db.session.execute(db.select(User).where(User.email == DEMO_USERS[role][0])).scalar_one()


def login(app, role):
    """A test client fully logged in (password + TOTP) as the demo user for ``role``."""
    client = app.test_client()
    user = user_by_role(role)
    user.totp_last_counter = None  # allow the same code to be reused across test clients
    db.session.commit()
    client.post("/login", data={"email": user.email, "password": PASSWORD})
    resp = client.post("/login/verify", data={"code": pyotp.TOTP(user.totp_secret).now()})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/"), resp.data[:300]
    return client


@pytest.fixture
def as_role(app):
    return lambda role: login(app, role)


SAMPLE_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_data", "dummy_clients.csv")


def import_sample(client):
    with open(SAMPLE_CSV, "rb") as fh:
        resp = client.post("/import/", data={"file": (fh, "dummy_clients.csv")}, content_type="multipart/form-data")
    assert resp.status_code == 302
    preview_url = resp.headers["Location"]
    return preview_url
