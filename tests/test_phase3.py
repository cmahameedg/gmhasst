"""Phase 3: attention dashboard, notices, DSC and morning email. First three tests are the 'Done when' checks."""
import io
import re
from datetime import date, datetime, timedelta

import pytest

from app import attention
from app.mailer import outbox
from app.models import Client, Dsc, Filing, FilingHistory, MorningEmailLog, Notice, ReturnType, db
from app.morning import send_all
from app.permissions import ARTICLE, MANAGER, PARTNER, STAFF

from .conftest import import_sample, user_by_role

TODAY = date(2026, 10, 12)  # a Monday


@pytest.fixture
def world(app, as_role):
    """Dummy clients, registrations from the master, September and October generated, today = 12 Oct 2026."""
    app.config["TODAY"] = TODAY
    partner = as_role(PARTNER)
    token = import_sample(partner).rsplit("/", 1)[1]
    partner.post(f"/import/{token}/confirm")
    partner.post("/registrations/from-master")
    partner.post("/board/generate", data={"month": "2026-09"})
    partner.post("/board/generate", data={"month": "2026-10"})
    return partner


def client_named(name):
    return db.session.execute(db.select(Client).where(Client.name == name)).scalar_one()


def record_notice(as_who, client, assignee, due, **extra):
    data = {"client_id": client.id, "department": "GST", "notice_type": "DRC-01", "reference_no": extra.pop("ref", "ZD3310260001234"),
            "notice_date": (TODAY - timedelta(days=20)).isoformat(), "period": "FY 2023-24",
            "amount_demanded": "1,25,000", "reply_due_date": due.isoformat(), "assigned_to": assignee.id, **extra}
    resp = as_who.post("/notices/new", data=data)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:2000]
    return db.session.execute(db.select(Notice).where(Notice.reference_no == data["reference_no"])).scalar_one()


def dashboard_items(html):
    """(kind, category) for each row on the dashboard, top to bottom."""
    return re.findall(r'class="item" data-kind="(\w+)" data-category="(\d)"', html)


# --- Done when --------------------------------------------------------------------

def test_notice_due_tomorrow_is_top_of_assigned_staff_dashboard(world, as_role):
    ravi, anita = user_by_role(STAFF), user_by_role(ARTICLE)
    krishna = client_named("Krishna Traders")  # Ravi is primary
    notice = record_notice(world, krishna, ravi, TODAY + timedelta(days=1))
    # A notice due in 10 days is not yet on the dashboard.
    record_notice(world, krishna, ravi, TODAY + timedelta(days=10), ref="ZD3310260009999")

    html = as_role(STAFF).get("/today").get_data(as_text=True)
    rows = dashboard_items(html)
    assert rows[0] == ("notice", "1")
    assert [r for r in rows if r[0] == "notice"] == [("notice", "1")]
    # Ravi also has overdue September filings, which rank below the notice.
    assert ("filing", "2") in rows and rows.index(("filing", "2")) > 0
    first_link = re.search(r'class="item".*?<a href="([^"]+)"', html, re.S).group(1)
    assert first_link.endswith(f"/notices/{notice.id}")
    assert "ZD3310260001234" in html and "₹1,25,000.00" in html

    # It belongs to Ravi, not to Anita.
    assert "ZD3310260001234" not in as_role(ARTICLE).get("/today").get_data(as_text=True)
    # The Partner sees it at the top of the firm view, with its owner.
    firm = world.get("/today?view=firm").get_data(as_text=True)
    assert dashboard_items(firm)[0] == ("notice", "1") and "Ravi (Staff)" in firm

    # Once replied, it drops off.
    world.post(f"/notices/{notice.id}/status", data={"status": "Replied", "reply_date": TODAY.isoformat(), "reply_ref": "ARN-R-1"})
    assert ("notice", "1") not in dashboard_items(as_role(STAFF).get("/today").get_data(as_text=True))


def _add_dsc(as_who, client, expiry, holder="Gopal Krishna"):
    resp = as_who.post("/registers/dsc/new", data={
        "client_id": client.id, "holder_name": holder, "dsc_class": "Class 3 (signing)", "issuer": "eMudhra",
        "expiry_date": expiry.isoformat(), "held_by": "Office"})
    assert resp.status_code == 302
    return db.session.execute(db.select(Dsc).where(Dsc.holder_name == holder)).scalar_one()


def test_dsc_expiring_in_20_days_is_flagged(world, as_role):
    krishna = client_named("Krishna Traders")
    soon = _add_dsc(world, krishna, TODAY + timedelta(days=20))
    later = _add_dsc(world, krishna, TODAY + timedelta(days=45), holder="Partner Two")

    ravi_page = as_role(STAFF).get("/today").get_data(as_text=True)
    assert ("dsc", "5") in dashboard_items(ravi_page)
    assert "DSC: Gopal Krishna" in ravi_page and "expires in 20 day(s)" in ravi_page
    assert "Partner Two" not in ravi_page

    register = world.get("/registers/").get_data(as_text=True)
    assert f'data-dsc="{soon.id}" data-flag="expiring"' in register and "Expires in 20 day(s)" in register
    assert f'data-dsc="{later.id}" data-flag="ok"' in register
    assert "DSC expires: Gopal Krishna" in world.get("/calendar?month=2026-11").get_data(as_text=True)

    # Renewing replaces it and clears the flag.
    world.post(f"/registers/dsc/new?renew={soon.id}", data={
        "client_id": krishna.id, "holder_name": "Gopal Krishna (2028)", "dsc_class": "Class 3 (signing)",
        "issuer": "eMudhra", "expiry_date": "2028-10-31", "held_by": "Office"})
    db.session.refresh(soon)
    assert not soon.active
    assert ("dsc", "5") not in dashboard_items(as_role(STAFF).get("/today").get_data(as_text=True))


def _items_text(text):
    return [line.strip() for line in text.splitlines() if line.strip().startswith("- ")]


def test_morning_email_has_correct_items_for_two_staff_members(world, app, as_role):
    ravi, anita = user_by_role(STAFF), user_by_role(ARTICLE)
    record_notice(world, client_named("Krishna Traders"), ravi, TODAY + timedelta(days=1))
    _add_dsc(world, client_named("Sharma Textiles"), TODAY + timedelta(days=20), holder="Lakshmi Sharma")  # Anita's client
    outbox().clear()

    runner = app.test_cli_runner()
    result = runner.invoke(args=["send-morning-emails", "--date", TODAY.isoformat()])
    assert result.exit_code == 0, result.output
    mails = {m["To"]: m for m in outbox()}
    assert {ravi.email, anita.email, user_by_role(PARTNER).email, user_by_role(MANAGER).email} <= set(mails)
    assert user_by_role("billing").email not in mails and user_by_role("readonly").email not in mails

    def body(msg):
        return msg.get_body(("plain",)).get_content()

    for person in (ravi, anita):
        expected = attention.items_for(person, TODAY)
        text = body(mails[person.email])
        lines = _items_text(text)
        assert len(lines) == len(expected) > 0, person.email
        # Same items, same order (most urgent first).
        for line, item in zip(lines, expected):
            assert item.title in line and item.client.name in line
        assert mails[person.email]["Subject"].startswith(f"Your day Mon 12 Oct: {len(expected)} item(s)")
        assert "https://gmh.example/today" in text

    ravi_text, anita_text = body(mails[ravi.email]), body(mails[anita.email])
    assert "ZD3310260001234" in ravi_text and ravi_text.index("1. Notices") < ravi_text.index("2. Filings overdue")
    assert "ZD3310260001234" not in anita_text and "Krishna Traders" not in anita_text
    assert "DSC: Lakshmi Sharma" in anita_text and "DSC: Lakshmi Sharma" not in ravi_text
    assert "Sharma Textiles" in anita_text and "Sharma Textiles" not in ravi_text
    html = mails[anita.email].get_body(("html",)).get_content()
    assert "Lakshmi Sharma" in html and "Open your dashboard" in html

    partner_mail = body(mails[user_by_role(PARTNER).email])
    assert mails[user_by_role(PARTNER).email]["Subject"].startswith("Firm summary")
    assert "Ravi (Staff):" in partner_mail and "Anita (Article):" in partner_mail
    assert "ZD3310260001234" in partner_mail and "DSC: Lakshmi Sharma" in partner_mail

    # Running again the same morning sends nothing more; Sunday sends nothing.
    count = len(outbox())
    runner.invoke(args=["send-morning-emails", "--date", TODAY.isoformat()])
    assert len(outbox()) == count
    result = runner.invoke(args=["send-morning-emails", "--date", "2026-10-18"])
    assert "not a working day" in result.output and len(outbox()) == count
    assert db.session.execute(db.select(db.func.count(MorningEmailLog.id))).scalar() == 4


# --- Ordering rules ---------------------------------------------------------------------

def _filing(code, client_name, gen="2026-10"):
    rt = db.session.execute(db.select(ReturnType).where(ReturnType.code == code)).scalar_one()
    return db.session.execute(db.select(Filing).join(Client).where(
        Filing.return_type_id == rt.id, Client.name == client_name, Filing.gen_month == gen)).scalars().first()


def test_ordering_rules_three_four_and_six(world, app):
    ravi = user_by_role(STAFF)
    # Rule 3: due within 3 days and not approved. Rule 6: due within 7 days.
    soon = _filing("GSTR1", "Green Leaf LLP")
    soon.due_date = TODAY + timedelta(days=2)
    approved = _filing("GSTR1", "Bharat Infra Projects Limited")
    approved.due_date, approved.status = TODAY + timedelta(days=2), "approved"
    week = _filing("GSTR3B", "Green Leaf LLP")
    week.due_date = TODAY + timedelta(days=6)
    # Rule 4: in review queries for 3 days (due far away).
    stuck = _filing("PMT06", "Krishna Traders")
    stuck.status = "review_queries"
    stuck.history.append(FilingHistory(user_id=ravi.id, from_status="ready_for_review", to_status="review_queries",
                                       at=datetime(2026, 10, 9, 2, 0)))
    fresh = _filing("GSTR3B", "Bharat Infra Projects Limited")
    fresh.status = "review_queries"
    fresh.history.append(FilingHistory(user_id=ravi.id, from_status="ready_for_review", to_status="review_queries",
                                       at=datetime(2026, 10, 11, 2, 0)))
    db.session.commit()

    cats = {(i.kind, i.args.get("filing_id")): i.category for i in attention.collect(TODAY)}
    assert cats[("filing", soon.id)] == 3
    assert cats[("filing", approved.id)] == 6  # already approved: only the 7-day rule applies
    assert cats[("filing", week.id)] == 6
    assert cats[("filing", stuck.id)] == 4
    assert ("filing", fresh.id) not in cats  # 1 day in review queries, due in November
    ordered = [i.category for i in attention.collect(TODAY)]
    assert ordered == sorted(ordered)


def test_team_counts_and_on_time_rate(world, as_role):
    ravi = user_by_role(STAFF)
    mine = [f for f in db.session.execute(db.select(Filing).where(Filing.maker_id == ravi.id, Filing.gen_month == "2026-09")).scalars()
            if f.due_date and f.due_date <= TODAY - timedelta(days=4)][:4]
    assert len(mine) == 4
    filed_dates = [mine[0].due_date, mine[1].due_date, mine[2].due_date + timedelta(days=3), mine[3].due_date - timedelta(days=1)]
    for f, filed_on in zip(mine, filed_dates):
        f.status, f.filed_on, f.arn = "filed", filed_on, "ARN1"
    db.session.commit()
    rows, firm = attention.team_stats(TODAY)
    ravi_row = next(r for r in rows if r["user"].id == ravi.id)
    assert ravi_row["filed"] == 4 and ravi_row["on_time"] == 75
    html = world.get("/today").get_data(as_text=True)  # Partner: firm view by default
    assert f'data-staff="{ravi.id}"' in html and "75%" in html
    assert as_role(STAFF).get("/today?view=firm").status_code == 403


# --- Notices --------------------------------------------------------------------------

def test_notice_status_rules_documents_and_access(world, as_role):
    ravi, anita = user_by_role(STAFF), user_by_role(ARTICLE)
    sharma = client_named("Sharma Textiles")  # Anita primary, Ravi reviewer
    notice = record_notice(world, sharma, anita, TODAY + timedelta(days=5))

    def status(**data):
        return world.post(f"/notices/{notice.id}/status", data=data, follow_redirects=True).get_data(as_text=True)

    assert "Reply date and reply reference are required" in status(status="Replied")
    assert "Hearing date is required" in status(status="Hearing fixed")
    assert "Outcome amount is required" in status(status="Closed — order passed")
    status(status="Replied", reply_date=TODAY.isoformat(), reply_ref="ACK/2026/77")
    db.session.refresh(notice)
    assert notice.status == "Replied" and notice.reply_ref == "ACK/2026/77"
    status(status="Closed — order passed", outcome_amount="40,000")
    db.session.refresh(notice)
    assert notice.status == "Closed — order passed" and notice.outcome_amount_paise == 4000000
    assert [h.to_status for h in notice.history] == ["Received", "Replied", "Closed — order passed"]

    # Required fields on create.
    resp = world.post("/notices/new", data={"client_id": sharma.id, "department": "GST", "notice_type": "Others"}, follow_redirects=True)
    text = resp.get_data(as_text=True)
    for msg in ("Describe the notice", "Reference number is required", "Reply due date is required", "Assign the notice"):
        assert msg in text

    # PDFs only; download goes through the permission check.
    anita_c = as_role(ARTICLE)
    anita_c.post(f"/notices/{notice.id}/documents", data={"kind": "notice", "file": (io.BytesIO(b"MZ not a pdf"), "x.pdf")},
                 content_type="multipart/form-data")
    assert notice.documents == []
    anita_c.post(f"/notices/{notice.id}/documents", data={"kind": "notice", "file": (io.BytesIO(b"%PDF-1.4 test"), "DRC 01.pdf")},
                 content_type="multipart/form-data")
    db.session.refresh(notice)
    doc = notice.documents[0]
    resp = anita_c.get(f"/notices/{notice.id}/documents/{doc.id}")
    assert resp.status_code == 200 and resp.data == b"%PDF-1.4 test"
    # Article can update status of her notice but cannot edit its fields.
    assert anita_c.get(f"/notices/{notice.id}/edit").status_code == 403

    # Someone without the client cannot see the notice or its documents.
    other = record_notice(world, client_named("Krishna Traders"), ravi, TODAY + timedelta(days=5), ref="OTHER1")
    assert anita_c.get(f"/notices/{other.id}").status_code == 404
    assert anita_c.get(f"/notices/{notice.id}/documents/{doc.id}").status_code == 200
    assert as_role("billing").get("/notices/").status_code == 403


def test_past_due_notice_and_hearing_count_as_rule_one(world, as_role):
    ravi = user_by_role(STAFF)
    krishna = client_named("Krishna Traders")
    late = record_notice(world, krishna, ravi, TODAY - timedelta(days=4), ref="LATE1")
    hearing = record_notice(world, krishna, ravi, TODAY + timedelta(days=30), ref="HEAR1")
    world.post(f"/notices/{hearing.id}/status", data={"status": "Hearing fixed", "hearing_date": (TODAY + timedelta(days=2)).isoformat()})
    items = [i for i in attention.items_for(ravi, TODAY) if i.kind == "notice"]
    assert [(i.args["notice_id"], i.overdue) for i in items] == [(late.id, True), (hearing.id, False)]
    assert all(i.category == 1 for i in items)


# --- Calendar and feed --------------------------------------------------------------------

def test_calendar_and_private_ical_feed(world, app):
    ravi = user_by_role(STAFF)
    record_notice(world, client_named("Krishna Traders"), ravi, date(2026, 10, 28), ref="CAL1")
    html = world.get("/calendar?month=2026-11").get_data(as_text=True)
    assert "GSTR-1 · Oct 2026" in html and "GSTR-3B · Oct 2026" in html
    assert "From the Return Master" in html or "data-kind=\"master\"" in html  # e.g. IFF / not-generated dates
    assert "Notice reply: Krishna Traders" in world.get("/calendar?month=2026-10").get_data(as_text=True)

    page = world.post("/calendar/feed", data={"action": "create"}).get_data(as_text=True)
    url = re.search(r'value="(http://localhost/calendar/feed/[^"]+\.ics)"', page).group(1)
    path = url.replace("http://localhost", "")
    anon = app.test_client()
    resp = anon.get(path)
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200 and resp.mimetype == "text/calendar"
    assert body.startswith("BEGIN:VCALENDAR") and "SUMMARY:Notice reply: Krishna Traders" in body
    assert "DTSTART;VALUE=DATE:20261028" in body and "\r\n" in body
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    assert anon.get("/calendar/feed/not-a-token.ics").status_code == 404
    world.post("/calendar/feed", data={"action": "revoke"})
    assert anon.get(path).status_code == 404
