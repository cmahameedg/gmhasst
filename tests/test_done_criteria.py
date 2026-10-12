"""The four Phase 1 'Done when' checks."""
import csv
import io
import json
import re

import pytest

from app.models import ActivityChange, ActivityEvent, Client, ImportBatch, db
from app.permissions import ARTICLE, MANAGER, PARTNER, READONLY, STAFF

from .conftest import import_sample, user_by_role

# Data rows in sample_data/dummy_clients.csv that must be rejected, and why (file line -> field).
EXPECTED_ERRORS = {
    5: {"pan"},  # bad PAN format
    9: {"pan"},  # duplicate of row 3's PAN
    12: {"name"},  # missing client name
    15: {"gstins"},  # PAN inside GSTIN does not match client PAN
    18: {"mobile", "email"},  # 8-digit mobile, email without domain suffix
    20: {"onboarding_date"},  # missing required date
}
# Good rows where Anita (Article) is primary or reviewer.
ARTICLE_ROWS = {2, 3, 7, 11, 14, 16, 21}


@pytest.fixture
def imported(app, as_role):
    partner = as_role(PARTNER)
    preview = import_sample(partner)
    token = preview.rsplit("/", 1)[1]
    resp = partner.post(f"/import/{token}/confirm")
    assert resp.status_code == 302
    return partner


def test_dummy_csv_flags_errors_row_by_row(app, as_role):
    partner = as_role(PARTNER)
    preview_url = import_sample(partner)
    html = partner.get(preview_url).get_data(as_text=True)

    batch = db.session.execute(db.select(ImportBatch)).scalar_one()
    rows = json.loads(batch.rows_json)["rows"]
    assert len(rows) == 20
    flagged = {r["line"]: set(r["errors"]) for r in rows if r["errors"]}
    assert flagged == EXPECTED_ERRORS

    # The preview highlights exactly those rows and shows the reasons.
    error_rows = set(int(n) for n in re.findall(r'class="row-error" data-row="(\d+)"', html))
    assert error_rows == set(EXPECTED_ERRORS)
    assert "14</strong> ready" in html and "6</strong> with errors" in html
    assert "ABCDE1234 is not in the format AAAAA9999A" in html
    assert "Duplicate PAN BSHPS4321L: same as row 3" in html
    assert "Client name is required" in html
    assert "not the client PAN AAJFC7788W" in html

    # Nothing is saved until confirmed.
    assert db.session.execute(db.select(db.func.count(Client.id))).scalar() == 0

    token = preview_url.rsplit("/", 1)[1]
    partner.post(f"/import/{token}/confirm")
    clients = db.session.execute(db.select(Client).order_by(Client.code)).scalars().all()
    assert [c.code for c in clients] == [f"GMH-{n:04d}" for n in range(1, 15)]
    sepl = next(c for c in clients if c.pan == "AAECS9012N")
    assert len(sepl.gstins) == 2 and "ROC" in sepl.services

    # Confirming twice does not import twice.
    partner.post(f"/import/{token}/confirm")
    assert db.session.execute(db.select(db.func.count(Client.id))).scalar() == 14

    errors_csv = partner.get(f"/import/{token}/errors.csv").get_data(as_text=True)
    assert len(list(csv.reader(io.StringIO(errors_csv.lstrip("﻿"))))) == 1 + 6


def test_article_sees_only_assigned_clients_even_with_edited_urls(app, imported, as_role):
    article = as_role(ARTICLE)
    anita = user_by_role(ARTICLE)
    clients = db.session.execute(db.select(Client)).scalars().all()
    mine = [c for c in clients if anita.id in (c.primary_staff_id, c.reviewer_staff_id)]
    others = [c for c in clients if c not in mine]
    assert len(mine) == len(ARTICLE_ROWS) and others

    listing = article.get("/").get_data(as_text=True)
    for c in mine:
        assert c.code in listing
    for c in others:
        assert c.code not in listing and c.pan not in listing

    # Editing the URL by hand: every client route answers 404 for someone else's client.
    for c in others:
        for url in (f"/clients/{c.id}", f"/clients/{c.id}/edit", f"/clients/{c.id}?tab=documents"):
            assert article.get(url).status_code == 404, url
        before = c.status
        new = "Inactive" if before != "Inactive" else "Active"
        assert article.post(f"/clients/{c.id}/edit", data={"status": new}).status_code == 404
        db.session.refresh(c)
        assert c.status == before
    # Ids that do not exist look exactly the same.
    assert article.get("/clients/99999").status_code == 404

    # Searching for another client's PAN or GSTIN finds nothing; export holds only own clients.
    other = next(c for c in others if c.gstins)
    assert other.code not in article.get(f"/?q={other.pan}").get_data(as_text=True)
    assert other.code not in article.get(f"/?q={other.gstins[0].gstin}").get_data(as_text=True)
    exported = article.get("/export.csv").get_data(as_text=True)
    assert len(list(csv.DictReader(io.StringIO(exported.lstrip("﻿"))))) == len(mine)

    # Article can change status only; other submitted fields are ignored.
    target = mine[0]
    old_name = target.name
    resp = article.post(f"/clients/{target.id}/edit", data={"status": "Inactive", "name": "Hacked", "pan": "ZZZZZ9999Z"})
    assert resp.status_code == 302
    db.session.refresh(target)
    assert target.status == "Inactive" and target.name == old_name and target.pan != "ZZZZZ9999Z"

    # Staff-only and partner-only screens are closed too.
    for url in ("/staff/", "/import/", "/activity", "/clients/new"):
        assert article.get(url).status_code == 403, url


FEE_WORDS = re.compile(r"fee|monthly retainer|per return", re.I)


def test_manager_sees_no_fee_field_anywhere(app, imported, as_role):
    partner = imported
    client = db.session.execute(db.select(Client).where(Client.pan == "AAECS9012N")).scalar_one()
    # A fee change by the partner must not leak through the activity log either.
    partner.post(f"/clients/{client.id}/edit", data={**_form_of(client), "fee_basis": "Annual"})
    assert db.session.get(Client, client.id).fee_basis == "Annual"

    manager = as_role(MANAGER)
    pages = [
        "/",
        f"/clients/{client.id}",
        f"/clients/{client.id}/edit",
        "/clients/new",
        "/import/",
        "/activity",
        f"/activity?client={client.id}",
        "/export.csv",
    ] + [f"/clients/{client.id}?tab={t}" for t in ("overview", "filings", "notices", "documents")]
    for url in pages:
        resp = manager.get(url)
        assert resp.status_code == 200, url
        found = FEE_WORDS.search(resp.get_data(as_text=True))
        assert not found, f"{url} shows {found.group(0)!r}"
    assert manager.get(f"/clients/{client.id}?tab=fees").status_code == 404

    # Import preview for a manager does not show the CSV's fee column.
    preview = import_sample(manager)
    assert not FEE_WORDS.search(manager.get(preview).get_data(as_text=True))

    # A crafted POST cannot change the fee either.
    manager.post(f"/clients/{client.id}/edit", data={**_form_of(client), "fee_basis": "Free"})
    assert db.session.get(Client, client.id).fee_basis == "Annual"

    for role in (STAFF, ARTICLE, READONLY):
        other = as_role(role)
        assert not FEE_WORDS.search(other.get("/").get_data(as_text=True))
        assert not FEE_WORDS.search(other.get("/export.csv").get_data(as_text=True))


def _form_of(client):
    """The edit form as a browser would submit it unchanged."""
    from app.clientsvc import snapshot

    values = snapshot(client, export=True)
    values["primary_staff"] = str(client.primary_staff_id or "")
    values["reviewer_staff"] = str(client.reviewer_staff_id or "")
    values["gstins"] = "\n".join(g.gstin for g in client.gstins)
    values.pop("services")
    return {**values, "services": client.services}


def test_every_edit_is_logged_with_old_and_new_values(app, imported, as_role):
    partner = imported
    client = db.session.execute(db.select(Client).where(Client.pan == "AAKFK5678M")).scalar_one()
    form = _form_of(client)
    form.update(mobile="9000000001", status="Inactive", services=["GST Monthly", "TDS"], notes="Moved to monthly")
    assert partner.post(f"/clients/{client.id}/edit", data=form).status_code == 302

    event = db.session.execute(
        db.select(ActivityEvent).where(ActivityEvent.action == "client.update", ActivityEvent.client_id == client.id)
    ).scalar_one()
    changes = {c.field: (c.old_value, c.new_value) for c in event.changes}
    assert changes == {
        "mobile": ("9876543210", "9000000001"),
        "status": ("Active", "Inactive"),
        "services": ("GST QRMP; Income Tax; TDS", "GST Monthly; TDS"),
        "notes": ("", "Moved to monthly"),
    }
    assert event.user.email == user_by_role(PARTNER).email

    # Staff edit and Article status edit are logged too.
    ravi_client = db.session.execute(
        db.select(Client).where(Client.primary_staff_id == user_by_role(STAFF).id)
    ).scalars().first()
    as_role(STAFF).post(f"/clients/{ravi_client.id}/edit", data={**_form_of(ravi_client), "district": "Changed"})
    anita_client = db.session.execute(
        db.select(Client).where(Client.primary_staff_id == user_by_role(ARTICLE).id)
    ).scalars().first()
    as_role(ARTICLE).post(f"/clients/{anita_client.id}/edit", data={"status": "Closed"})
    logged = {
        (c.event.client_id, c.field, c.new_value)
        for c in db.session.execute(db.select(ActivityChange)).scalars()
    }
    assert (ravi_client.id, "district", "Changed") in logged
    assert (anita_client.id, "status", "Closed") in logged

    # And they appear on the activity log screen, filterable by client.
    html = partner.get(f"/activity?client={client.id}").get_data(as_text=True)
    assert "9876543210" in html and "9000000001" in html
    html = partner.get(f"/activity?staff={user_by_role(ARTICLE).id}").get_data(as_text=True)
    assert anita_client.code in html
