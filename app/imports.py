"""CSV import: upload -> preview with row-by-row errors -> confirm."""
import csv
import io
import json
import re
import secrets

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from .clientsvc import save_client, validate_client
from .models import ImportBatch, db, utcnow
from .permissions import FIELDS, can_import, editable_fields, ordered, visible_fields
from .validators import clean_code, split_list

bp = Blueprint("imports", __name__, url_prefix="/import")

MAX_ROWS = 5000


def _norm(header):
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


# Accept the field key ("pan"), its label ("Date of birth / incorporation") and a few common aliases.
HEADER_MAP = {_norm(key): key for key in FIELDS}
HEADER_MAP.update({_norm(label): key for key, label in FIELDS.items()})
HEADER_MAP.update({
    "gstin": "gstins",
    "gst": "gstins",
    "gst_number": "gstins",
    "cin": "cin_llpin",
    "llpin": "cin_llpin",
    "dob": "dob_incorporation",
    "date_of_birth": "dob_incorporation",
    "date_of_incorporation": "dob_incorporation",
    "group": "group",
    "family": "group",
    "services": "services",
    "primary": "primary_staff",
    "reviewer": "reviewer_staff",
    "phone": "mobile",
    "mobile_number": "mobile",
    "udyam": "udyam",
})


def _decode(data):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_csv(text, user):
    """Validate every row. Returns (columns, rows, ignored_headers).

    Each row is {"line", "values", "errors"}; ``errors`` maps field key -> message.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        return [], [], []

    allowed = editable_fields(user, None)
    mapping, ignored = {}, []
    for index, header in enumerate(headers):
        key = HEADER_MAP.get(_norm(header))
        if key and key in allowed and key not in mapping.values():
            mapping[index] = key
        elif header.strip() and (key is None or key in visible_fields(user)):
            ignored.append(header.strip())  # columns the user cannot even see are dropped silently
    columns = ordered(set(mapping.values()))

    rows, seen_pan, seen_gstin = [], {}, {}
    for line, cells in enumerate(reader, start=2):
        if not any(cell.strip() for cell in cells):
            continue
        if len(rows) >= MAX_ROWS:
            break
        raw = {key: (cells[i] if i < len(cells) else "") for i, key in mapping.items()}
        _, errors = validate_client(raw, allowed, None, show_codes=True)

        # Duplicates inside the file itself.
        pan = clean_code(raw.get("pan", ""))
        if pan and "pan" not in errors:
            if pan in seen_pan:
                errors["pan"] = f"Duplicate PAN {pan}: same as row {seen_pan[pan]}"
            else:
                seen_pan[pan] = line
        for gstin in (clean_code(x) for x in split_list(raw.get("gstins", ""))):
            if gstin in seen_gstin and "gstins" not in errors:
                errors["gstins"] = f"Duplicate GSTIN {gstin}: same as row {seen_gstin[gstin]}"
            seen_gstin.setdefault(gstin, line)

        rows.append({"line": line, "values": raw, "errors": errors})
    return columns, rows, ignored


def _batch(token):
    batch = db.session.execute(
        db.select(ImportBatch).where(ImportBatch.token == token, ImportBatch.user_id == g.user.id)
    ).scalar_one_or_none()
    if batch is None:
        abort(404)
    return batch


@bp.before_request
def importers_only():
    if not can_import(g.user):
        abort(403)


@bp.route("/", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        upload_file = request.files.get("file")
        if not upload_file or not upload_file.filename:
            flash("Choose a CSV file to upload.", "error")
            return redirect(url_for("imports.upload"))
        columns, rows, ignored = parse_csv(_decode(upload_file.read()), g.user)
        if not rows:
            flash("The file has no data rows.", "error")
            return redirect(url_for("imports.upload"))
        batch = ImportBatch(
            token=secrets.token_urlsafe(16),
            user_id=g.user.id,
            filename=upload_file.filename[:255],
            rows_json=json.dumps({"columns": columns, "rows": rows, "ignored": ignored}),
        )
        db.session.add(batch)
        db.session.commit()
        return redirect(url_for("imports.preview", token=batch.token))
    return render_template(
        "imports/upload.html", columns=ordered(editable_fields(g.user, None)), labels=FIELDS
    )


@bp.route("/<token>")
def preview(token):
    batch = _batch(token)
    data = json.loads(batch.rows_json)
    rows = data["rows"]
    bad = sum(1 for r in rows if r["errors"])
    return render_template(
        "imports/preview.html",
        batch=batch,
        columns=data["columns"],
        rows=rows,
        ignored=data["ignored"],
        labels=FIELDS,
        good=len(rows) - bad,
        bad=bad,
    )


@bp.route("/<token>/errors.csv")
def errors_csv(token):
    batch = _batch(token)
    data = json.loads(batch.rows_json)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["row"] + data["columns"] + ["errors"])
    for row in data["rows"]:
        if row["errors"]:
            writer.writerow(
                [row["line"]] + [row["values"].get(c, "") for c in data["columns"]]
                + ["; ".join(row["errors"].values())]
            )
    return Response(
        "﻿" + out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=import-errors.csv"},
    )


@bp.route("/<token>/confirm", methods=["POST"])
def confirm(token):
    batch = _batch(token)
    if batch.confirmed_at:
        flash("This file has already been imported.", "error")
        return redirect(url_for("clients.index"))
    data = json.loads(batch.rows_json)
    allowed = editable_fields(g.user, None)
    created, skipped = [], 0
    for row in data["rows"]:
        if row["errors"]:
            skipped += 1
            continue
        # Re-validate: the data may have changed since the preview.
        clean, errors = validate_client(row["values"], allowed, None)
        if errors:
            skipped += 1
            continue
        client, _ = save_client(
            clean, g.user, action="client.import",
            summary=f"Imported from {batch.filename} row {row['line']}",
        )
        created.append(client)
    batch.confirmed_at = utcnow()
    db.session.commit()
    flash(f"Imported {len(created)} client(s); skipped {skipped} row(s) with errors.", "success")
    return redirect(url_for("clients.index"))
