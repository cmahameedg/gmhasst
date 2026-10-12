import re
from datetime import date

import pyotp
import pytest

from app import validators as v
from app.clientsvc import save_client, validate_client
from app.models import Client, User, db
from app.permissions import ARTICLE, BILLING, FIELDS, MANAGER, PARTNER, READONLY, STAFF

from .conftest import PASSWORD, user_by_role


def _new_client(actor, **overrides):
    raw = {
        "name": "Test Client",
        "entity_type": "Individual",
        "pan": "ABCPT1234Q",
        "dob_incorporation": "01-01-1990",
        "onboarding_date": "01-01-2024",
        **overrides,
    }
    clean, errors = validate_client(raw, set(FIELDS) - {"code"})
    assert not errors, errors
    client, _ = save_client(clean, actor)
    db.session.commit()
    return client


# --- Login and 2FA ---------------------------------------------------------

def test_password_alone_does_not_log_in(app):
    c = app.test_client()
    assert c.get("/").status_code == 302
    user = user_by_role(PARTNER)
    resp = c.post("/login", data={"email": user.email, "password": PASSWORD})
    assert resp.headers["Location"].endswith("/login/verify")
    assert c.get("/").headers["Location"].endswith("/login")
    assert c.post("/login/verify", data={"code": "000000"}).status_code == 200
    assert c.get("/").status_code == 302


def test_totp_code_cannot_be_replayed(app):
    user = user_by_role(STAFF)
    code = pyotp.TOTP(user.totp_secret).now()
    first, second = app.test_client(), app.test_client()
    for c in (first, second):
        c.post("/login", data={"email": user.email, "password": PASSWORD})
    assert first.post("/login/verify", data={"code": code}).status_code == 302
    assert second.post("/login/verify", data={"code": code}).status_code == 200  # rejected


def test_lockout_after_repeated_failures(app):
    c = app.test_client()
    user = user_by_role(STAFF)
    for _ in range(5):
        c.post("/login", data={"email": user.email, "password": "wrong"})
    resp = c.post("/login", data={"email": user.email, "password": PASSWORD}, follow_redirects=True)
    assert "Too many failed attempts" in resp.get_data(as_text=True)


def test_invite_flow_requires_2fa_setup(app, as_role):
    partner = as_role(PARTNER)
    html = partner.post("/staff/invite", data={"email": "new@demo.local", "name": "New Person", "role": STAFF}).get_data(as_text=True)
    link = re.search(r'value="(http://localhost/invite/[^"]+)"', html).group(1)
    path = link.replace("http://localhost", "")

    c = app.test_client()
    resp = c.post(path, data={"password": "a-long-password", "confirm": "a-long-password"})
    assert resp.headers["Location"].endswith("/login/setup-2fa")
    assert c.get("/").status_code == 302  # not in yet
    page = c.get("/login/setup-2fa").get_data(as_text=True)
    assert "<svg" in page
    user = db.session.execute(db.select(User).where(User.email == "new@demo.local")).scalar_one()
    assert c.post("/login/setup-2fa", data={"code": pyotp.TOTP(user.totp_secret).now()}).status_code == 302
    assert c.get("/").status_code == 200
    # The link only works once.
    assert app.test_client().get(path).status_code == 404


# --- Staff lifecycle --------------------------------------------------------

def test_leaver_clients_must_be_reassigned_before_deactivation(app, as_role):
    partner = as_role(PARTNER)
    ravi, anita, meena = user_by_role(STAFF), user_by_role(ARTICLE), user_by_role(MANAGER)
    a = _new_client(user_by_role(PARTNER), pan="ABCPA1111A", primary_staff=str(ravi.id), reviewer_staff=str(meena.id))
    b = _new_client(user_by_role(PARTNER), pan="ABCPB2222B", primary_staff=str(anita.id), reviewer_staff=str(ravi.id))
    ravi_client = as_role(STAFF)

    # No replacement chosen: refused, Ravi stays active.
    resp = partner.post(f"/staff/{ravi.id}/deactivate", data={})
    assert "Every client needs a new owner" in resp.get_data(as_text=True)
    db.session.refresh(ravi)
    assert ravi.active

    # Meena cannot take over as primary on a client where she is already reviewer.
    resp = partner.post(f"/staff/{ravi.id}/deactivate", data={f"primary_{a.id}": str(meena.id), f"reviewer_{b.id}": str(meena.id)})
    assert "Reviewer must be a different person" in resp.get_data(as_text=True)

    resp = partner.post(
        f"/staff/{ravi.id}/deactivate",
        data={f"primary_{a.id}": str(anita.id), "default_reviewer": str(meena.id)},
    )
    assert resp.status_code == 302
    db.session.refresh(ravi), db.session.refresh(a), db.session.refresh(b)
    assert not ravi.active
    assert a.primary_staff_id == anita.id and b.reviewer_staff_id == meena.id
    # Ravi's open session dies with the account.
    assert ravi_client.get("/").status_code == 302


def test_cannot_remove_last_partner(app, as_role):
    partner = as_role(PARTNER)
    me = user_by_role(PARTNER)
    partner.post(f"/staff/{me.id}/role", data={"role": MANAGER})
    db.session.refresh(me)
    assert me.role == PARTNER


def test_only_partner_manages_staff(app, as_role):
    for role in (MANAGER, STAFF, ARTICLE, BILLING, READONLY):
        assert as_role(role).get("/staff/").status_code == 403


# --- Role visibility ----------------------------------------------------------

def test_billing_sees_name_pan_gstin_only(app, as_role):
    gst = "33ABCPT1234Q1Z" + v.gstin_check_char("33ABCPT1234Q1Z")
    client = _new_client(
        user_by_role(PARTNER), gstins=gst, mobile="9876543210", notes="Secret note", fee_basis="Annual",
        trade_name="Hidden Trade",
    )
    billing = as_role(BILLING)
    for url in ("/", f"/clients/{client.id}", "/export.csv"):
        html = billing.get(url).get_data(as_text=True)
        assert "ABCPT1234Q" in html and gst in html
        for secret in ("9876543210", "Secret note", "Individual", "Hidden Trade"):
            assert secret not in html, (url, secret)
    assert "Annual" in billing.get(f"/clients/{client.id}?tab=fees").get_data(as_text=True)
    assert billing.get(f"/clients/{client.id}/edit").status_code == 403


def test_readonly_cannot_edit(app, as_role):
    client = _new_client(user_by_role(PARTNER))
    ro = as_role(READONLY)
    assert ro.get(f"/clients/{client.id}").status_code == 200
    assert ro.post(f"/clients/{client.id}/edit", data={"status": "Closed"}).status_code == 403


def test_staff_edits_assigned_but_not_assignment_or_others(app, as_role):
    ravi, meena = user_by_role(STAFF), user_by_role(MANAGER)
    mine = _new_client(user_by_role(PARTNER), pan="ABCPA1111A", primary_staff=str(ravi.id))
    other = _new_client(user_by_role(PARTNER), pan="ABCPB2222B")
    staff = as_role(STAFF)
    assert staff.get(f"/clients/{other.id}").status_code == 404
    staff.post(f"/clients/{mine.id}/edit", data={
        "name": "Renamed", "entity_type": "Individual", "pan": "ABCPA1111A", "dob_incorporation": "1990-01-01",
        "onboarding_date": "2024-01-01", "status": "Active", "primary_staff": str(meena.id),
    })
    db.session.refresh(mine)
    assert mine.name == "Renamed" and mine.primary_staff_id == ravi.id


# --- Client master rules ----------------------------------------------------

def test_client_codes_are_sequential_and_never_reused(app):
    partner = user_by_role(PARTNER)
    first = _new_client(partner, pan="ABCPA1111A")
    second = _new_client(partner, pan="ABCPB2222B")
    assert (first.code, second.code) == ("GMH-0001", "GMH-0002")
    db.session.delete(second)
    db.session.commit()
    assert _new_client(partner, pan="ABCPC3333C").code == "GMH-0003"


def test_validation_messages(app):
    fields = set(FIELDS) - {"code"}
    base = {"name": "X", "entity_type": "Individual", "pan": "ABCPT1234Q", "dob_incorporation": "01-01-1990", "onboarding_date": "01-01-2024"}
    cases = {
        "pan": {"pan": "ABCPT12345"},
        "tan": {"tan": "ABC12345D"},
        "mobile": {"mobile": "12345"},
        "email": {"email": "nobody@nowhere"},
        "cin_llpin": {"cin_llpin": "U12345TN2020PTC123456"},  # CIN on an Individual
        "gstins": {"gstins": "33ABCPT1234Q1ZZ"},  # bad checksum
        "dob_incorporation": {"dob_incorporation": (date.today().replace(year=date.today().year + 1)).isoformat()},
        "entity_type": {"entity_type": "Company"},
        "services": {"services": "GST Monthly; Astrology"},
    }
    for field, override in cases.items():
        _, errors = validate_client({**base, **override}, fields)
        assert field in errors, (field, errors)
    _, errors = validate_client(
        {**base, "entity_type": "LLP", "pan": "AAOFG7890Q", "cin_llpin": "AAB-1234",
         "mobile": "+91 98400 12345", "email": "A@B.IN", "state": "tamil nadu"},
        fields,
    )
    assert not errors


@pytest.mark.parametrize("gstin,ok", [("27AAPFU0939F1ZV", True), ("29AAGCB7383J1Z4", True), ("27AAPFU0939F1ZW", False)])
def test_gstin_checksum(gstin, ok):
    assert (v.gstin_error(gstin) is None) == ok


def test_export_neutralises_spreadsheet_formulas(app, as_role):
    _new_client(user_by_role(PARTNER), name="=HYPERLINK(\"http://evil\")")
    body = as_role(PARTNER).get("/export.csv").get_data(as_text=True)
    assert "'=HYPERLINK" in body
