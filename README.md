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
