"""Phase 2: compliance board. The first four tests are the 'Done when' checks."""
import re
from collections import Counter
from datetime import date

import pytest

from app import periods as p
from app.models import ActivityChange, Client, Filing, FilingMention, ReturnType, db
from app.permissions import ARTICLE, MANAGER, PARTNER, STAFF
from app.returns import compute_due

from .conftest import import_sample, user_by_role


@pytest.fixture
def board(app, as_role):
    """Dummy clients imported, registrations set up from the client master, today pinned to 12 Oct 2026."""
    app.config["TODAY"] = date(2026, 10, 12)
    partner = as_role(PARTNER)
    token = import_sample(partner).rsplit("/", 1)[1]
    partner.post(f"/import/{token}/confirm")
    assert partner.post("/registrations/from-master").status_code == 302
    return partner


def rt(code):
    return db.session.execute(db.select(ReturnType).where(ReturnType.code == code)).scalar_one()


def filings(**where):
    query = db.select(Filing)
    for key, value in where.items():
        query = query.where(getattr(Filing, key) == value)
    return db.session.execute(query).scalars().all()


def client_named(name):
    return db.session.execute(db.select(Client).where(Client.name == name)).scalar_one()


MONTHLY_GST = {  # client -> number of GSTINs on GST Monthly
    "Sharma Textiles": 1, "Southern Exports Private Limited": 2, "Green Leaf LLP": 1,
    "Bharat Infra Projects Limited": 1, "Precision Tools Private Limited": 1,
}
QRMP_GST = {"Krishna Traders": 1, "Deepa Kitchens": 1}
TAN_HOLDERS = {"Krishna Traders", "Southern Exports Private Limited", "Sri Ganesh Charitable Trust",
               "Bharat Infra Projects Limited", "Precision Tools Private Limited"}


def by_client(rows):
    return Counter(f.client.name for f in rows)


# --- Done when ------------------------------------------------------------------

def test_generating_october_creates_correct_rows_for_monthly_and_qrmp_clients(board):
    resp = board.post("/board/generate", data={"month": "2026-10"})
    assert resp.status_code == 302

    gstr1, gstr3b = filings(return_type_id=rt("GSTR1").id), filings(return_type_id=rt("GSTR3B").id)
    assert by_client(gstr1) == MONTHLY_GST and by_client(gstr3b) == MONTHLY_GST
    assert {f.due_date for f in gstr1} == {date(2026, 11, 11)}
    assert {f.due_date for f in gstr3b} == {date(2026, 11, 20)}
    assert {f.period_label for f in gstr1} == {"Oct 2026"}

    # QRMP in month 1 of the quarter: PMT-06 only. IFF is optional (not linked by default);
    # quarterly GSTR-1 / GSTR-3B wait for December.
    pmt06 = filings(return_type_id=rt("PMT06").id)
    assert by_client(pmt06) == QRMP_GST and {f.due_date for f in pmt06} == {date(2026, 11, 25)}
    for code in ("IFF", "GSTR1Q", "GSTR3BQ"):
        assert filings(return_type_id=rt(code).id) == []
    # QRMP clients get no monthly GSTR-1/3B, monthly clients no PMT-06.
    assert not set(QRMP_GST) & set(by_client(gstr1)) and not set(MONTHLY_GST) & set(by_client(pmt06))

    tds = filings(return_type_id=rt("TDSPAY").id)
    assert set(by_client(tds)) == TAN_HOLDERS and {f.due_date for f in tds} == {date(2026, 11, 7)}

    # Only these four returns fall due for an October period.
    assert {f.return_type.code for f in filings()} == {"GSTR1", "GSTR3B", "PMT06", "TDSPAY"}

    # Maker and reviewer come from the client master.
    sharma = next(f for f in gstr1 if f.client.name == "Sharma Textiles")
    assert sharma.maker.email == "anita.article@demo.local" and sharma.reviewer.email == "ravi.staff@demo.local"

    # Generating again creates nothing new.
    count = len(filings())
    board.post("/board/generate", data={"month": "2026-10"})
    assert len(filings()) == count

    # And the board shows them.
    html = board.get("/board?month=2026-10").get_data(as_text=True)
    assert html.count('data-filing="') == count


def test_qrmp_quarter_end_and_standing_exceptions(board):
    board.post("/board/generate", data={"month": "2026-12"})
    assert by_client(filings(return_type_id=rt("GSTR1Q").id)) == QRMP_GST
    assert {f.due_date for f in filings(return_type_id=rt("GSTR1Q").id)} == {date(2027, 1, 13)}
    assert {f.due_date for f in filings(return_type_id=rt("GSTR3BQ").id)} == {date(2027, 1, 22)}
    assert filings(return_type_id=rt("PMT06").id) == []  # month 3 of the quarter
    assert {f.due_date for f in filings(return_type_id=rt("TDS24Q").id)} == {date(2027, 1, 31)}
    assert {f.due_date for f in filings(return_type_id=rt("ADVTAX").id)} == {date(2026, 12, 15)}

    # Rules from the Return Master for the other periods named in the brief.
    def due(code, year, month):
        r = rt(code)
        period = p.period_for(r.frequency, year, month)
        return compute_due(r, period.end, period.code)

    assert due("TDSPAY", 2027, 3) == date(2027, 4, 30)       # March TDS: 30 April
    assert due("TDSPAY", 2027, 2) == date(2027, 3, 7)
    assert due("TDS26Q", 2027, 3) == date(2027, 5, 31)       # Q4 return: 31 May
    assert due("TDS26Q", 2026, 6) == date(2026, 7, 31)
    assert due("TDS26Q", 2026, 9) == date(2026, 10, 31)
    assert due("ADVTAX", 2027, 3) == date(2027, 3, 15)
    assert due("ITR", 2027, 3) == date(2027, 7, 31)
    assert due("ITRA", 2027, 3) == date(2027, 10, 31)
    assert due("TAR", 2027, 3) == date(2027, 9, 30)
    assert due("GSTR9", 2027, 3) == date(2027, 12, 31)
    assert due("GSTR4", 2027, 3) == date(2027, 4, 30)
    assert due("CMP08", 2026, 12) == date(2027, 1, 18)
    assert due("DPT3", 2027, 3) == date(2027, 6, 30)
    assert due("DIR3KYC", 2027, 3) == date(2027, 9, 30)
    assert due("MSME1", 2026, 9) == date(2026, 10, 31)
    assert due("MSME1", 2027, 3) == date(2027, 4, 30)
    assert due("LLP11", 2027, 3) == date(2027, 5, 30)
    assert due("LLP8", 2027, 3) == date(2027, 10, 30)
    assert due("IFF", 2026, 10) == date(2026, 11, 13)
    assert p.period_for(rt("IFF").frequency, 2026, 12) is None  # month 3 of the quarter: no IFF
    aoc4 = rt("AOC4")
    assert compute_due(aoc4, date(2027, 3, 31), "Y") is None
    assert compute_due(aoc4, date(2027, 3, 31), "Y", date(2027, 9, 25)) == date(2027, 10, 25)
    assert compute_due(rt("MGT7"), date(2027, 3, 31), "Y", date(2027, 9, 25)) == date(2027, 11, 24)
    assert compute_due(rt("ADT1"), date(2027, 3, 31), "Y", date(2027, 9, 25)) == date(2027, 10, 10)


def _master_form(r, **changes):
    form = {
        "name": r.name, "old_name": r.old_name or "", "new_name": r.new_name or "", "category": r.category,
        "frequency": r.frequency, "due_basis": r.due_basis, "due_day": str(r.due_day or ""),
        "due_month_offset": str(r.due_month_offset if r.due_month_offset is not None else ""),
        "event_label": r.event_label or "", "event_days": str(r.event_days or ""), "applies_to": r.applies_to,
        "portal_url": r.portal_url or "", "sort_order": str(r.sort_order), "notes": r.notes or "",
        "default_on": "1" if r.default_on else "", "requires_everify": "1" if r.requires_everify else "",
        "active": "1" if r.active else "",
        "ov_code": [o.period_code for o in r.overrides], "ov_day": [str(o.due_day) for o in r.overrides],
        "ov_offset": [str(o.due_month_offset) for o in r.overrides],
    }
    form.update(changes)
    return {k: v for k, v in form.items() if v != ""}


def test_changing_master_due_date_updates_all_open_filings(board, as_role):
    board.post("/board/generate", data={"month": "2026-09"})
    board.post("/board/generate", data={"month": "2026-10"})
    gstr3b = rt("GSTR3B")
    rows = filings(return_type_id=gstr3b.id)
    done = rows[0]
    done.status, done.arn, done.filed_on = "filed", "AA3309260000001", date(2026, 10, 10)
    db.session.commit()

    resp = board.post(f"/returns/{gstr3b.id}/edit", data=_master_form(gstr3b, due_day="24"))
    assert resp.status_code == 302
    for f in filings(return_type_id=gstr3b.id):
        db.session.refresh(f)
        expected = date(f.period_end.year + (f.period_end.month == 12), f.period_end.month % 12 + 1, 24)
        if f.id == done.id:
            assert f.due_date == date(2026, 10, 20)  # filed: history is not rewritten
        else:
            assert f.due_date == expected, f.period_label
    assert len([f for f in rows if f.id != done.id]) > 10

    # Other return types are untouched.
    assert {f.due_date for f in filings(return_type_id=rt("GSTR1").id, gen_month="2026-10")} == {date(2026, 11, 11)}

    # The change is in the activity log.
    assert db.session.execute(db.select(ActivityChange).where(ActivityChange.field == "rm_due_day",
                              ActivityChange.new_value == "24")).scalar_one()

    # A one-off extension moves only that period's open filings.
    board.post(f"/returns/{gstr3b.id}/extensions", data={"period_month": "2026-10", "due_date": "2026-11-27", "note": "Notfn 99/2026"})
    for f in filings(return_type_id=gstr3b.id):
        db.session.refresh(f)
        if f.gen_month == "2026-10":
            assert f.due_date == date(2026, 11, 27)
        elif f.id != done.id:
            assert f.due_date == date(2026, 10, 24)

    # Only a Partner can edit the master.
    assert as_role(MANAGER).post(f"/returns/{gstr3b.id}/edit", data=_master_form(gstr3b, due_day="1")).status_code == 403


def _move(client_, filing, to, **data):
    return client_.post(f"/filings/{filing.id}/status", data={"to": to, **data}, follow_redirects=True).get_data(as_text=True)


def test_filing_cannot_reach_filed_without_approval_and_arn(board, as_role, app):
    board.post("/board/generate", data={"month": "2026-10"})
    # Krishna Traders: maker Ravi (Staff), reviewer Meena (Manager).
    filing = next(f for f in filings(return_type_id=rt("PMT06").id) if f.client.name == "Krishna Traders")
    ravi, meena = as_role(STAFF), as_role(MANAGER)
    full = {"arn": "AA331026123456X", "filed_on": "2026-10-12", "tax_paid": "12,500"}

    def status():
        db.session.refresh(filing)
        return filing.status

    # Straight to Filed from any pre-approval state is refused.
    assert "cannot move" in _move(ravi, filing, "filed", **full)
    for step in ("data_awaited", "data_received", "in_preparation"):
        _move(ravi, filing, step)
        assert status() == step
        _move(ravi, filing, "filed", **full)
        assert status() == step
    _move(ravi, filing, "ready_for_review")
    assert status() == "ready_for_review"
    _move(ravi, filing, "filed", **full)
    _move(ravi, filing, "approved")  # maker is not the reviewer
    assert status() == "ready_for_review"

    # Reviewer sends back with queries; a comment is required.
    assert "Write the review queries" in _move(meena, filing, "review_queries")
    _move(meena, filing, "review_queries", note="Reconcile with 2B")
    assert status() == "review_queries"
    _move(ravi, filing, "filed", **full)
    assert status() == "review_queries"
    _move(ravi, filing, "in_preparation")
    _move(ravi, filing, "ready_for_review")
    _move(meena, filing, "approved")
    assert status() == "approved"

    # Approved, but the ARN, filing date and tax paid are all required.
    html = _move(ravi, filing, "filed", filed_on="2026-10-12", tax_paid="12500")
    assert "ARN / acknowledgement number is required" in html and status() == "approved"
    html = _move(ravi, filing, "filed", arn="AA331026123456X", filed_on="2026-10-12")
    assert "Tax paid is required" in html and status() == "approved"
    assert "Filing date is required" in _move(ravi, filing, "filed", arn="AA331026123456X", tax_paid="0")
    _move(ravi, filing, "filed", **full)
    assert status() == "filed"
    assert filing.arn == "AA331026123456X" and filing.tax_paid_paise == 1250000
    history = [h.to_status for h in filing.history]
    assert history.index("approved") < history.index("filed")

    # Closure by the reviewer needs the acknowledgement and, with tax paid, the challan.
    assert "Note the challan" in _move(meena, filing, "closed", ack_saved="1")
    _move(meena, filing, "closed", ack_saved="1", challan_ref="CPIN2610001")
    assert status() == "closed"


def test_late_filing_requires_late_fee_and_interest(board, as_role, app):
    board.post("/board/generate", data={"month": "2026-10"})
    filing = next(f for f in filings(return_type_id=rt("PMT06").id) if f.client.name == "Krishna Traders")
    filing.status = "approved"
    db.session.commit()
    app.config["TODAY"] = date(2026, 12, 5)
    ravi = as_role(STAFF)
    html = _move(ravi, filing, "filed", arn="AA331026123456X", filed_on="2026-12-01", tax_paid="100")
    assert "Late fee is required" in html and "Interest is required" in html
    _move(ravi, filing, "filed", arn="AA331026123456X", filed_on="2026-12-01", tax_paid="100", late_fee="300", interest="12.50")
    db.session.refresh(filing)
    assert filing.status == "filed" and filing.late_fee_paise == 30000 and filing.interest_paise == 1250


def test_reviewer_cannot_approve_own_work_and_article_cannot_file(board, as_role):
    board.post("/board/generate", data={"month": "2026-10"})
    gstr1 = filings(return_type_id=rt("GSTR1").id)
    # Meena (Manager) prepares and submits, then tries to approve her own submission.
    target = next(f for f in gstr1 if f.client.name == "Green Leaf LLP")
    meena = as_role(MANAGER)
    _move(meena, target, "in_preparation")
    _move(meena, target, "ready_for_review")
    _move(meena, target, "approved")
    db.session.refresh(target)
    assert target.status == "ready_for_review"
    _move(as_role(PARTNER), target, "approved")
    db.session.refresh(target)
    assert target.status == "approved"

    # Anita (Article) is maker for Sharma Textiles: can prepare, cannot file.
    sharma = next(f for f in gstr1 if f.client.name == "Sharma Textiles")
    anita = as_role(ARTICLE)
    _move(anita, sharma, "in_preparation")
    _move(anita, sharma, "ready_for_review")
    _move(as_role(STAFF), sharma, "approved")  # Ravi is the reviewer
    db.session.refresh(sharma)
    assert sharma.status == "approved"
    _move(anita, sharma, "filed", arn="AA331026999999X", filed_on="2026-10-12", tax_paid="0")
    db.session.refresh(sharma)
    assert sharma.status == "approved"


def test_unfiled_september_items_appear_in_october_as_overdue(board):
    board.post("/board/generate", data={"month": "2026-09"})
    board.post("/board/generate", data={"month": "2026-10"})
    sep_gstr1 = filings(return_type_id=rt("GSTR1").id, gen_month="2026-09")
    sep_gstr3b = filings(return_type_id=rt("GSTR3B").id, gen_month="2026-09")
    # One September GSTR-1 was filed on time: it must not be carried forward.
    filed = sep_gstr1[0]
    filed.status, filed.arn, filed.filed_on = "filed", "AA330926000001A", date(2026, 10, 9)
    db.session.commit()

    html = board.get("/board?month=2026-10").get_data(as_text=True)
    chips = {int(m.group(1)): (m.group(2), m.group(3))
             for m in re.finditer(r'data-filing="(\d+)" data-colour="(\w+)" data-carried="(\d)"', html)}
    # Sep GSTR-1 (due 11 Oct) is past due on 12 Oct: carried forward and red.
    for f in sep_gstr1[1:]:
        assert chips[f.id] == ("red", "1"), f.client.name
    assert filed.id not in chips
    # Sep GSTR-3B (due 20 Oct) is carried forward too, not yet red.
    for f in sep_gstr3b:
        assert chips[f.id] == ("grey", "1")
    # October's own filings are not carried and not overdue.
    for f in filings(gen_month="2026-10"):
        assert chips[f.id] == ("grey", "0")
    assert "↻ Sep 2026" in html
    red = sum(1 for colour_, _ in chips.values() if colour_ == "red")
    assert red >= len(sep_gstr1) - 1  # advance tax due 15 Sep is overdue too
    assert re.search(r"<strong[^>]*>\s*%d\s*</strong> overdue" % red, html)

    # And they keep carrying (and stay red) into November until filed.
    html = board.get("/board?month=2026-11").get_data(as_text=True)
    for f in sep_gstr1[1:]:
        assert f'data-filing="{f.id}" data-colour="red" data-carried="1"' in html


# --- Screens, access and bulk actions ---------------------------------------------

def test_board_respects_client_visibility(board, as_role):
    board.post("/board/generate", data={"month": "2026-10"})
    anita = as_role(ARTICLE)
    me = user_by_role(ARTICLE)
    html = anita.get("/board?month=2026-10").get_data(as_text=True)
    for f in filings():
        mine = me.id in (f.client.primary_staff_id, f.client.reviewer_staff_id)
        assert (f'data-filing="{f.id}"' in html) == mine
        if not mine:
            assert anita.get(f"/filings/{f.id}").status_code == 404
            assert anita.post(f"/filings/{f.id}/status", data={"to": "in_preparation"}).status_code == 404
    assert anita.post("/board/generate", data={"month": "2026-11"}).status_code == 403


def test_my_work_and_review_queue(board, as_role):
    board.post("/board/generate", data={"month": "2026-10"})
    ravi_user = user_by_role(STAFF)
    ravi = as_role(STAFF)
    html = ravi.get("/my-work").get_data(as_text=True)
    mine = [f for f in filings() if f.maker_id == ravi_user.id]
    assert mine and all(f"/filings/{f.id}\"" in html for f in mine)
    # Sorted by due date: TDS (7 Nov) before GSTR-1 (11 Nov) before GSTR-3B (20 Nov).
    dues = [m for m in re.findall(r"(\d\d-\d\d-\d{4})", html)]
    parsed = [date(int(d[6:]), int(d[3:5]), int(d[:2])) for d in dues]
    assert parsed == sorted(parsed)

    target = mine[0]
    _move(ravi, target, "in_preparation")
    _move(ravi, target, "ready_for_review")
    reviewer = target.reviewer
    assert reviewer is not None
    queue = as_role(MANAGER if reviewer.role == MANAGER else PARTNER).get("/review?all=1").get_data(as_text=True)
    assert f"/filings/{target.id}\"" in queue


def test_comments_with_mentions(board, as_role):
    board.post("/board/generate", data={"month": "2026-10"})
    filing = filings(return_type_id=rt("GSTR1").id)[0]
    board.post(f"/filings/{filing.id}/comment", data={"body": "@ravi.staff please chase the purchase register"})
    mention = db.session.execute(db.select(FilingMention)).scalar_one()
    assert mention.user.email == "ravi.staff@demo.local" and not mention.seen
    ravi = as_role(STAFF)
    assert "chase the purchase register" in ravi.get("/my-work").get_data(as_text=True)
    ravi.get(f"/filings/{filing.id}")
    db.session.refresh(mention)
    assert mention.seen


def test_bulk_nil_column_and_reassign(board, as_role):
    board.post("/board/generate", data={"month": "2026-10"})
    pmt = rt("PMT06")
    resp = board.post("/board/bulk-nil", data={"month": "2026-10", "return_type_id": pmt.id, "nil_kind": "Not applicable"})
    assert resp.status_code == 302
    rows = filings(return_type_id=pmt.id)
    assert rows and all(f.status == "nil_na" and f.nil_kind == "Not applicable" for f in rows)
    assert all(f.history[-1].to_status == "nil_na" for f in rows)

    ravi, partner_user = user_by_role(STAFF), user_by_role(PARTNER)
    open_ravi = [f for f in filings() if f.maker_id == ravi.id and f.status != "nil_na"]
    board.post("/board/reassign", data={"slot": "maker", "from_user": ravi.id, "to_user": partner_user.id})
    for f in open_ravi:
        db.session.refresh(f)
        assert f.maker_id == partner_user.id
    assert as_role(STAFF).post("/board/reassign", data={"slot": "maker", "from_user": partner_user.id,
                                                        "to_user": ravi.id}).status_code == 403


def test_return_master_searchable_by_old_and_new_form_names(board):
    tds = rt("TDS24Q")
    board.post(f"/returns/{tds.id}/edit", data=_master_form(tds, new_name="Form 138"))
    for term in ("24Q", "Form 138", "salary"):
        html = board.get(f"/returns/?q={term}").get_data(as_text=True)
        assert "TDS return (salary)" in html, term
    assert "TDS return (salary)" not in board.get("/returns/?q=GSTR").get_data(as_text=True)


def test_registration_rules(board, as_role):
    krishna = client_named("Krishna Traders")
    staff = as_role(STAFF)  # Ravi is Krishna's primary
    # A GSTIN that is not on the client master is refused.
    html = staff.post(f"/clients/{krishna.id}/registrations/new", data={
        "kind": "GST", "identifier": "27AAPFU0939F1ZV", "scheme": "Monthly",
        "return_types": [str(rt("GSTR1").id)]}, follow_redirects=True).get_data(as_text=True)
    assert "not the client PAN" in html or "not on this client" in html
    # Opting in to IFF on the existing QRMP registration.
    reg = next(r for r in krishna.registrations if r.kind == "GST")
    staff.post(f"/registrations/{reg.id}/edit", data={
        "kind": "GST", "identifier": reg.identifier, "scheme": "QRMP",
        "return_types": [str(x.id) for x in reg.return_types] + [str(rt("IFF").id)]})
    db.session.refresh(reg)
    assert rt("IFF") in reg.return_types
    board.post("/board/generate", data={"month": "2026-10"})
    assert by_client(filings(return_type_id=rt("IFF").id)) == {"Krishna Traders": 1}
