"""Fixed lists used by the client master."""

ENTITY_TYPES = [
    "Individual",
    "HUF",
    "Proprietorship",
    "Partnership Firm",
    "LLP",
    "Private Limited",
    "Public Limited",
    "Trust",
    "Society",
    "AOP/BOI",
]

# CIN / LLPIN is shown and accepted only for these entity types.
CIN_ENTITY_TYPES = {"LLP", "Private Limited", "Public Limited"}

SERVICES = [
    "GST Monthly",
    "GST QRMP",
    "Income Tax",
    "TDS",
    "ROC",
    "Bookkeeping",
    "Tax Audit",
    "Cost Audit",
    "Project Report",
    "GST Refund",
    "Export Advisory",
    "Registrations",
]

ONBOARDING_SOURCES = [
    "Referral",
    "Existing client",
    "Walk-in",
    "Online",
    "Father's practice",
    "Other",
]

FEE_BASES = ["Monthly retainer", "Per return", "Annual", "Free"]

STATUSES = ["Active", "Inactive", "Closed"]

# State / UT name -> GST state code.
STATES = {
    "Jammu and Kashmir": "01",
    "Himachal Pradesh": "02",
    "Punjab": "03",
    "Chandigarh": "04",
    "Uttarakhand": "05",
    "Haryana": "06",
    "Delhi": "07",
    "Rajasthan": "08",
    "Uttar Pradesh": "09",
    "Bihar": "10",
    "Sikkim": "11",
    "Arunachal Pradesh": "12",
    "Nagaland": "13",
    "Manipur": "14",
    "Mizoram": "15",
    "Tripura": "16",
    "Meghalaya": "17",
    "Assam": "18",
    "West Bengal": "19",
    "Jharkhand": "20",
    "Odisha": "21",
    "Chhattisgarh": "22",
    "Madhya Pradesh": "23",
    "Gujarat": "24",
    "Dadra and Nagar Haveli and Daman and Diu": "26",
    "Maharashtra": "27",
    "Karnataka": "29",
    "Goa": "30",
    "Lakshadweep": "31",
    "Kerala": "32",
    "Tamil Nadu": "33",
    "Puducherry": "34",
    "Andaman and Nicobar Islands": "35",
    "Telangana": "36",
    "Andhra Pradesh": "37",
    "Ladakh": "38",
}

# Codes that can legitimately start a GSTIN: every state above plus
# legacy Daman & Diu (25), Other Territory (97) and Centre Jurisdiction (99).
GSTIN_STATE_CODES = set(STATES.values()) | {"25", "97", "99"}
