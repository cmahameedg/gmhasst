# GMH Practice — Phase 1: clients, staff and roles

This is the client master for the practice. Each person sees only what their role allows, and the server checks this on every request. The browser is never trusted with it.

| Role | Clients visible | Fees | Edit clients | Manage staff |
|---|---|---|---|---|
| Partner | All | Yes | Yes | Yes |
| Manager | All | No | Yes | No |
| Staff | Assigned only | No | Assigned only | No |
| Article | Assigned only | No | Status only | No |
| Billing | All (code, name, PAN, GSTIN) | Yes | No | No |
| Read-only | All | No | No | No |

"Assigned" means the person is the client's primary staff or its reviewer.
The whole matrix lives in `app/permissions.py`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export FLASK_APP=wsgi.py
.venv/bin/flask create-partner          # first Partner: email, name, password
.venv/bin/flask run                     # http://127.0.0.1:5000
```

At first login the Partner scans a QR code with an authenticator app (Google or Microsoft Authenticator). After that, every login asks for a password and a 6-digit code.

Settings (environment variables):

| Variable | Default | Notes |
|---|---|---|
| `SESSION_COOKIE_SECURE` | `1` | Set `0` only for plain-HTTP testing on a LAN IP. Keep `1` behind HTTPS. |
| `DATABASE_URL` | `sqlite:///instance/gmh.db` | Back up `instance/` regularly. |
| `SECRET_KEY` | generated into `instance/secret_key` | |
| `TIMEZONE` | `Asia/Kolkata` | Used for display and for the activity-log date filter. |

To run it for real, put it behind HTTPS, for example `waitress-serve --listen=127.0.0.1:8000 wsgi:app` behind Caddy or nginx.

## Screens

1. **Login + 2FA.** Five wrong attempts lock the account for 15 minutes. A 2FA code cannot be used twice.
2. **Clients.** Search by name, PAN, GSTIN or code. Filter by entity type, service, staff or status. Export to CSV; the export contains only the fields your role can see.
3. **Client profile.** Shows every field. The Filings, Notices, Documents and Fees tabs are placeholders for later phases. Only fee roles see the Fees tab.
4. **Staff** (Partner only).
   - **Invite:** creates a one-time link that expires in 7 days. Send it over WhatsApp or email.
   - **Change role** and **assign clients.**
   - **Reset password & 2FA:** for a lost phone.
   - **Deactivate leaver:** every client the leaver holds must get a new primary or reviewer before the account closes. The leaver's open session ends at once.
5. **Import CSV.** Upload a file and see a preview with bad rows and cells highlighted. Confirming imports the good rows and skips the bad ones. You can download the rejected rows as a CSV to fix and re-upload.
6. **Activity log** (Partner and Manager).
   - Every change is logged with old and new values: creates, edits, imports, assignments, staff changes, logins and exports.
   - You can filter by client, staff member and date.
   - Managers never see fee changes.

## Validation rules

- **PAN:** `AAAAA9999A`, unique.
- **GSTIN:** 15 characters, a valid state code and the GSTN checksum. The PAN inside it must match the client's PAN. Each GSTIN is unique across clients.
- **TAN:** `AAAA99999A`.
- **CIN / LLPIN:** accepted only for Private Limited, Public Limited and LLP. A CIN has 21 characters; an LLPIN looks like `AAA-9999`.
- **Udyam:** `UDYAM-XX-00-0000000`.
- **IEC:** 10 characters.
- **Mobile:** 10 digits. A leading `+91` or `0` is removed first.
- **Email:** checked for a valid format.
- **Required fields:** name, entity type, PAN, date of birth or incorporation, and onboarding date. Dates cannot be in the future.
- **Client codes:** `GMH-0001` onward, taken from a counter, so a code is never reused.
- The primary staff member and the reviewer must be different people.

## Trying it with the dummy data

```bash
.venv/bin/flask seed-demo     # one demo user per role; prints invite links
```

`sample_data/dummy_clients.csv` has 20 clients:

- 14 import cleanly.
- 6 are flagged:
  - row 5: bad PAN
  - row 9: duplicate PAN
  - row 12: missing name
  - row 15: GSTIN with the wrong PAN
  - row 18: bad mobile and email
  - row 20: missing onboarding date

Staff in the file are matched by the demo users' email addresses.

## Tests

```bash
.venv/bin/pytest -q
```

`tests/test_done_criteria.py` checks the four Phase 1 acceptance criteria.
`tests/test_access.py` covers 2FA, invites, deactivating leavers, the Billing and Read-only roles, and the validators.

---

# Phase 2: compliance board

Every filing for every client and period shows on one board. Filings are generated from each client's **registrations**.

## How filings are generated

1. **Registrations.** Each client has registrations, managed on the client's Filings tab:
   - GSTIN with a scheme of Monthly, QRMP or Composition
   - TAN
   - Income tax (Non-audit or Audit)
   - Company (CIN), LLP (LLPIN) or Director (DIN)
   
   Each registration links to the returns it files. Optional returns such as IFF, GSTR-9 and MSME-1 are left unticked by default.
   
   For an existing practice, a Partner can press **Return master → Set up registrations from client master** once. This creates registrations from each client's GSTINs, TAN and services.
2. **Generate period.** A Partner picks a month on the board and presses **Generate period**. The app creates one filing per registration for each linked return whose period ends in that month:
   - monthly returns every month
   - IFF and PMT-06 in months 1 and 2 of each quarter
   - quarterly returns in June, September, December and March
   - half-yearly returns in September and March
   - annual returns in March (the financial-year end)
   
   Generating the same month again creates nothing new. You can also run `flask generate-period 2026-10 --as-email partner@…`.
3. **Carry-forward.** Anything not finished from an earlier month stays on every later board, marked ↻, and is red once it is past its due date.

## Return Master (only a Partner can edit it)

Due dates live only in the database:

- **Rule:** a due day plus months after the period ends, for example "11th of next month". AGM-based returns (AOC-4, MGT-7, ADT-1) are due a set number of days after the AGM date. Entering the AGM date on one filing updates all of them for that client and year.
- **Standing exceptions:** for example, TDS for March is due 30 April, and Q4 TDS returns are due 31 May.
- **One-off extensions:** these apply to a single period, such as GSTR-3B for October 2026 being due on 27 November.

Saving a row recalculates the due date on every **open** filing of that type. Filed, closed and Nil filings keep their original dates.

Every row starts with the dates from the brief. **Check each one against the current notification before go-live.**

Each return has two form-name fields, one under the Income-tax Act, 1961 and one under the Income-tax Act, 2025. Search matches either. The 2025-Act names are blank: fill them in from the notified rules.

## Status flow

The order is: Not started → Data awaited / Data received → In preparation → Ready for review → (Review queries ↺) → Approved → Filed → Closed. Nil / Not applicable is a separate exit.

| Rule | How it is enforced |
|---|---|
| Filed only after Approved | The only way into Filed is from Approved. |
| Filed needs an ARN, a filing date and tax paid (₹) | All three are required on the form. |
| A late filing needs late fee and interest | Required when the filing date is after the due date. |
| The checker is not the maker | Whoever sent the filing for review cannot approve it. A Partner is the exception. |
| Review queries need a comment | The comment is added to the filing's comment thread. |
| Closing a filing | Needs the acknowledgement saved. ITRs must also be e-verified, and any tax paid needs the challan (CIN/BRN). |
| Who acts | Maker: the assigned Staff or Article, or a Partner or Manager. Reviewer: the filing's reviewer, or a Partner or Manager. Filing and marking Nil: Staff, Manager or Partner (not an Article). |

Every status change, reassignment, AGM date and Return Master edit is written to the activity log.

## Screens

- **Board:** clients down, returns across.
  - Cell colours: grey Not started, amber in progress, blue Ready for review, green Filed or Closed, red overdue.
  - **Bulk…** on a column marks all of that month's open filings in the column Nil or Not applicable.
  - **Reassign filings…** moves open filings from one person to another.
- **My work:** your open filings, earliest due first, plus unread @mentions.
- **Review queue:** filings waiting for your review, and filed returns waiting to be closed.
- **Filing detail:** status history, comments with `@name` mentions (the part of an email address before the @), the ARN, tax, late fee and interest, and a link to the government portal. No passwords are stored.

---

# Phase 3: attention dashboard, notices and DSC

## Today (the landing page after login)

**Today** shows one list of what needs action, ordered by consequence rather than by date. Each item appears once, under the most urgent rule it meets:

1. Notices whose reply is due within 3 days or already past due. Hearings within 3 days count here too.
2. Filings that are overdue: past their due date and not Filed.
3. Filings due within 3 days that are not yet at Approved.
4. Filings that have sat in Review queries for more than 2 days.
5. DSCs expiring within 30 days, and registration deadlines due within 30 days.
6. Filings due within 7 days.

**Whose list an item goes on:**
- **Notices:** the person the notice is assigned to.
- **Filings:** the reviewer while the filing is at Ready for review; otherwise the maker.
- **DSCs and deadlines:** the client's primary staff member.

**Views:**
- **Partner:** the whole firm by default, with team counts for each staff member: open items, overdue items, and on-time filing rate over the last 90 days. The on-time rate counts filings with a filing date on or before the due date, attributed to the maker.
- **Manager:** can switch between their own list and the whole firm.
- **Everyone else:** sees only their own list.

## Notices (Notices menu, and the client's Notices tab)

The fields follow the brief.
- **Notice types** depend on the department.
- **"Others"** needs a short description.
- **Replied** needs the reply date and the reply reference.
- **Hearing fixed** needs the hearing date. This is my addition, so that hearings feed rule 1.
- **Closing** (dropped, or order passed) needs the outcome amount; enter 0 when nothing is payable.

Notice, reply and order documents must be PDFs, up to 15 MB each. They are stored under `instance/uploads/` and are downloaded only after the same permission check as the notice itself.

**Who can do what:**
- Partner and Manager: everything.
- Staff: record and edit notices on their own clients.
- Article: change the status of, and upload documents to, notices assigned to them.
- Billing: has no access to notices.

## DSC and registration register (DSC menu)

- **DSCs:** one row per certificate holder. **Renewed…** records the new certificate and marks the old one inactive.
- **Registration deadlines:**
  - **Udyam annual update:** you enter the due date. When you mark one done, next year's is created automatically.
  - **IEC annual update:** defaults to 30 June, the end of the April–June window. It also repeats yearly.
  - **GST bank account (Rule 10A):** enter the registration grant date and the deadline is set 30 days later. The rule also requires the details before the first GSTR-1 / IFF if that comes sooner.

## Calendar

The month view shows:
- every filing due date, with its open count;
- Return Master dates for periods not generated yet, in grey;
- notice reply dates and hearings;
- DSC expiries and registration deadlines.

**Get calendar feed link (iCal)** creates a private URL. It is shown once and only a hash of it is stored. Add it in Google Calendar under *Other calendars → + → From URL*. It covers the last 60 days and the next 12 months, and can be revoked or replaced from the Calendar page.

## Morning email

```bash
flask send-morning-emails                     # today; skips non-working days and anyone already sent
flask send-morning-emails --dry-run           # preview subjects
flask send-morning-emails --date 2026-10-12 --force
```

`deploy/crontab.example` runs it at **8:00 AM IST** (`CRON_TZ=Asia/Kolkata`).

**Who gets what:**
- Partner, Manager, Staff and Article users each get their own items from rules 1–6, as text and HTML, with links into the app.
- Partners get the firm summary with team counts.
- People with nothing urgent get a short "nothing urgent" email.

| Setting | Default | |
|---|---|---|
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM` | — | Gmail / Google Workspace: `smtp.gmail.com`, port 587, with an app password |
| `MAIL_BACKEND` | `smtp` if `SMTP_HOST` is set, otherwise `file` | `file` writes `.eml` files to `instance/outbox/` for a trial run |
| `BASE_URL` | `http://localhost:5000` | Used for links in emails |
| `WORKING_DAYS` | `0,1,2,3,4,5` (Monday to Saturday) | Set to `0,1,2,3,4` for a five-day week |
| `HOLIDAYS` | — | Comma-separated `YYYY-MM-DD` dates |
