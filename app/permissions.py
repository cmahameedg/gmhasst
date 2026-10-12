"""The role matrix. Every screen asks this module what the current user may see or do.

Role        Clients visible        Fees  Edit clients        Manage staff
Partner     All                    Yes   Yes                 Yes
Manager     All                    No    Yes                 No
Staff       Assigned only          No    Assigned only       No
Article     Assigned only          No    Status fields only  No
Billing     All (name/PAN/GSTIN)   Yes   No                  No
Read-only   All                    No    No                  No
"""
from flask import abort
from sqlalchemy import or_

from .models import Client, db

PARTNER = "partner"
MANAGER = "manager"
STAFF = "staff"
ARTICLE = "article"
BILLING = "billing"
READONLY = "readonly"

ROLE_LABELS = {
    PARTNER: "Partner",
    MANAGER: "Manager",
    STAFF: "Staff",
    ARTICLE: "Article",
    BILLING: "Billing",
    READONLY: "Read-only",
}
ROLES = list(ROLE_LABELS)

# Roles that can be primary / reviewer on a client.
ASSIGNABLE_ROLES = {PARTNER, MANAGER, STAFF, ARTICLE}

# Every client field, in display order: key -> label.
FIELDS = {
    "code": "Client code",
    "name": "Client name",
    "trade_name": "Trade name",
    "entity_type": "Entity type",
    "pan": "PAN",
    "gstins": "GSTIN",
    "tan": "TAN",
    "cin_llpin": "CIN / LLPIN",
    "udyam": "Udyam number",
    "iec": "IEC",
    "dob_incorporation": "Date of birth / incorporation",
    "contact_person": "Contact person",
    "mobile": "Mobile",
    "email": "Email",
    "address": "Address",
    "district": "District",
    "state": "State",
    "group": "Group / family",
    "services": "Services taken",
    "primary_staff": "Primary staff",
    "reviewer_staff": "Reviewer",
    "onboarding_source": "Onboarding source",
    "onboarding_date": "Onboarding date",
    "fee_basis": "Fee basis",
    "status": "Status",
    "notes": "Notes",
}

FEE_FIELDS = {"fee_basis"}
ASSIGNMENT_FIELDS = {"primary_staff", "reviewer_staff"}
STATUS_FIELDS = {"status"}
BILLING_FIELDS = {"code", "name", "pan", "gstins"} | FEE_FIELDS
READ_ONLY_FIELDS = {"code"}  # generated, never edited

ALL_CLIENTS_ROLES = {PARTNER, MANAGER, BILLING, READONLY}


def sees_all_clients(user):
    return user.role in ALL_CLIENTS_ROLES


def can_see_fees(user):
    return user.role in {PARTNER, BILLING}


def can_manage_staff(user):
    return user.role == PARTNER


def can_create_clients(user):
    return user.role in {PARTNER, MANAGER}


def can_import(user):
    return user.role in {PARTNER, MANAGER}


def can_view_activity(user):
    return user.role in {PARTNER, MANAGER}


def visible_fields(user):
    """Client fields this user may see, anywhere: list, profile, export, activity log."""
    if user.role == BILLING:
        return set(BILLING_FIELDS)
    fields = set(FIELDS)
    if not can_see_fees(user):
        fields -= FEE_FIELDS
    return fields


def ordered(fields):
    return [key for key in FIELDS if key in fields]


def is_assigned(user, client):
    return user.id in (client.primary_staff_id, client.reviewer_staff_id)


def editable_fields(user, client=None):
    """Fields this user may change on ``client`` (or on a new client when None)."""
    role = user.role
    if role in {PARTNER, MANAGER}:
        return visible_fields(user) - READ_ONLY_FIELDS
    if client is None:
        return set()
    if role == STAFF and is_assigned(user, client):
        return visible_fields(user) - READ_ONLY_FIELDS - ASSIGNMENT_FIELDS
    if role == ARTICLE and is_assigned(user, client):
        return set(STATUS_FIELDS)
    return set()


def can_edit_client(user, client):
    return bool(editable_fields(user, client))


def visible_clients(user):
    """Base query of clients this user is allowed to know exist."""
    query = db.select(Client)
    if not sees_all_clients(user):
        query = query.where(
            or_(Client.primary_staff_id == user.id, Client.reviewer_staff_id == user.id)
        )
    return query


def get_visible_client_or_404(user, client_id):
    """Fetch a client by id, but 404 (not 403) when it is outside the user's scope.

    A 404 does not even confirm that the id exists, so editing the URL reveals nothing.
    """
    client = db.session.execute(
        visible_clients(user).where(Client.id == client_id)
    ).scalar_one_or_none()
    if client is None:
        abort(404)
    return client
