#!/usr/bin/env python3
"""Generate realistic demo master data and ingest it into a Masterly Environment.

Run it against an empty Environment and it bootstraps what it needs — Workspace, Domain,
Data Models (published), and one Source per originating system — then generates a
population of business entities and delivers them over ``POST /v1/ingest``.

What makes the data *useful* rather than merely random: the same real-world entity is
delivered by several source systems under different keys, in that system's own field
names and formatting conventions, with the drift you actually get in the field —
abbreviated streets, legal-form variants, upper-cased ERP names, reformatted phone
numbers, typos, missing optional fields. That is what Identity Resolution, Golden
Resolution and Data Quality have to earn their keep on. A configurable share of records
carries a genuine structural defect (bad e-mail, unknown enum value, missing required
field), so the quarantine and DQ surfaces have something real in them too.

Everything is seeded: the same ``--seed`` produces the same records, and re-delivery
upserts by source key, so running twice never doubles the data.

    # bootstrap + generate + ingest, local install on the dev identity binding
    # from this checkout: uv run examples/demo_data.py …
    # anywhere else:      uv run --with 'masterly>=0.2.0' demo_data.py …
    uv run examples/demo_data.py --base-url http://localhost:8001 \
        --dev-login you@example.com --customers 300 --wait

    # look at the records without an install
    uv run examples/demo_data.py --dry-run --customers 5 --out /tmp/records.json

    # ingest into a model you already have, generated from its own definition
    uv run examples/demo_data.py --base-url https://app.example.com \
        --token "$MASTERLY_TOKEN" --environment env_prod_eu \
        --model "Customer" --source crm --records 200

    # top up an Environment as a machine: a service account, no session anywhere
    uv run examples/demo_data.py --base-url https://app.example.com \
        --service-account-token "$MASTERLY_SERVICE_ACCOUNT_TOKEN" \
        --source-id crm=src_7f3c9a --customers 300

Everything it does rides the SDK: `client.workspaces`, `client.domains`,
`client.data_models` and `client.sources` for the configuration, `client.sources.ingest`
for the delivery. The only raw calls left are the two that come before a connection
exists — minting a token and asking which Environments you may use.

Both token personas run it. With a session token it does all of the above. With a
`--service-account-token` it does the one thing a machine principal is allowed to do —
deliver records into the Sources that account's `ingest` scope names — which is why those
Sources are named by id: a service account cannot list an Environment, cannot build
configuration, and cannot read the counters afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx

from masterly import ApiError, Client

# --------------------------------------------------------------------------------------
# Vocabulary. Small, hand-picked pools beat a faker dependency here: the point is control
# over how values vary, and every domain is a reserved `.example` one — nothing generated
# resolves to a real company, address or mailbox.
# --------------------------------------------------------------------------------------

COMPANY_STEMS = [
    "Nordwind", "Kvarnby", "Stenberg", "Almgren", "Vasaloppet", "Brantevik", "Hagfors",
    "Lindholm", "Sjöberg", "Ekvall", "Ravensburg", "Delft", "Aalborg", "Trondheim",
    "Kaskinen", "Bergslagen", "Öresund", "Silverdal", "Norrsken", "Klarälven", "Tystberga",
    "Fjällvind", "Granlund", "Hammarby", "Ödesmark", "Vindeln", "Bruksvallarna", "Skagen",
    "Lauenburg", "Utrecht", "Kronoberg", "Ljungby", "Mälardalen", "Rosendal", "Sundvall",
    "Torneå", "Ålesund", "Bornholm", "Charlottenlund", "Dalby", "Enköping", "Falsterbo",
]

COMPANY_QUALIFIERS = [
    "Logistik", "Industri", "Teknik", "Verkstad", "Trading", "Systems", "Components",
    "Materials", "Services", "Group", "Partners", "Solutions", "Engineering", "Distribution",
]

# Legal form by country — the single most common cause of "same company, different string".
LEGAL_FORMS = {
    "SE": "AB", "NO": "AS", "DK": "A/S", "FI": "Oy", "DE": "GmbH",
    "NL": "B.V.", "GB": "Ltd", "FR": "SAS", "PL": "Sp. z o.o.",
}

# Expanded spelling of the same legal form — a source system that writes it out in full.
LEGAL_FORM_LONG = {
    "AB": "Aktiebolag", "AS": "Aksjeselskap", "A/S": "Aktieselskab", "Oy": "Osakeyhtiö",
    "GmbH": "Gesellschaft mit beschränkter Haftung", "B.V.": "Besloten Vennootschap",
    "Ltd": "Limited", "SAS": "Société par Actions Simplifiée", "Sp. z o.o.": "Spolka",
}

# city, country, postal code, international dialling code
CITIES = [
    ("Stockholm", "SE", "111 22", "+46 8"), ("Göteborg", "SE", "411 03", "+46 31"),
    ("Malmö", "SE", "211 34", "+46 40"), ("Uppsala", "SE", "753 20", "+46 18"),
    ("Linköping", "SE", "582 22", "+46 13"), ("Umeå", "SE", "903 26", "+46 90"),
    ("Oslo", "NO", "0150", "+47 22"), ("Bergen", "NO", "5003", "+47 55"),
    ("København", "DK", "1050", "+45 33"), ("Aarhus", "DK", "8000", "+45 86"),
    ("Helsinki", "FI", "00100", "+358 9"), ("Tampere", "FI", "33100", "+358 3"),
    ("Berlin", "DE", "10115", "+49 30"), ("Hamburg", "DE", "20095", "+49 40"),
    ("Amsterdam", "NL", "1012 AB", "+31 20"), ("Rotterdam", "NL", "3011 AA", "+31 10"),
    ("London", "GB", "EC1A 1BB", "+44 20"), ("Manchester", "GB", "M1 1AE", "+44 161"),
    ("Paris", "FR", "75001", "+33 1"), ("Lyon", "FR", "69001", "+33 4"),
    ("Warszawa", "PL", "00-001", "+48 22"), ("Kraków", "PL", "30-001", "+48 12"),
]

STREETS = [
    "Storgatan", "Kungsgatan", "Sveavägen", "Hamngatan", "Industrivägen", "Verkstadsgatan",
    "Björkallén", "Ringvägen", "Bruksgatan", "Norra Esplanaden", "Södra Långgatan",
    "Fabriksgatan", "Hantverkargatan", "Sjöbodsvägen", "Terminalgatan", "Lastkajen",
]

# Common abbreviations a source system applies to a street name — pure formatting drift.
STREET_ABBREVIATIONS = {
    "gatan": "g.", "vägen": "v.", "allén": "all.", "Esplanaden": "Espl.", "Norra": "N.",
    "Södra": "S.",
}

INDUSTRIES = [
    "Wholesale trade", "Machinery manufacturing", "Road freight transport", "Construction",
    "Food processing", "Electrical equipment", "Pharmaceuticals", "Retail trade",
    "Business consulting", "Software publishing", "Metal fabrication", "Packaging",
]

FIRST_NAMES = [
    "Anna", "Erik", "Karin", "Johan", "Maria", "Lars", "Sofia", "Anders", "Elin", "Peter",
    "Ingrid", "Mikael", "Hanna", "Per", "Emma", "Nils", "Lisa", "Gustav", "Sara", "Oskar",
]

LAST_NAMES = [
    "Andersson", "Johansson", "Karlsson", "Nilsson", "Eriksson", "Larsson", "Olsson",
    "Persson", "Svensson", "Gustafsson", "Lindqvist", "Berg", "Holm", "Sandberg", "Falk",
]

PRODUCT_CATEGORIES = ["tools", "fasteners", "safety", "electrical", "packaging", "chemicals"]

PRODUCT_NOUNS = {
    "tools": ["Torque wrench", "Impact driver", "Angle grinder", "Caliper", "Hex key set"],
    "fasteners": ["Hex bolt", "Flange nut", "Washer", "Wood screw", "Threaded rod"],
    "safety": ["Safety helmet", "Cut-resistant glove", "Ear defender", "Visor", "Harness"],
    "electrical": ["Cable gland", "Contactor", "Terminal block", "Junction box", "Relay"],
    "packaging": ["Stretch film", "Corrugated box", "Strapping band", "Pallet collar", "Void fill"],
    "chemicals": ["Degreaser", "Thread locker", "Lubricant spray", "Sealant", "Rust remover"],
}

PRODUCT_BRANDS = [
    "Nordkraft", "Vulkan", "Stigma", "Ferrum", "Bruks", "Hexa", "Tegra", "Optimo", "Kärnfast",
]

PRODUCT_MATERIALS = ["steel", "stainless", "brass", "nylon", "aluminium", "zinc-plated"]

SUPPLIER_CATEGORIES = [
    "raw-materials", "packaging", "logistics", "it-services", "facility",
    "professional-services",
]

# --------------------------------------------------------------------------------------
# Data Models. These are the wire shape of `POST /v1/data-models` — attributes carry the
# structural constraints (required / enum / regex / range) that decide, at ingest, whether
# a record is accepted or quarantined with a reason.
# --------------------------------------------------------------------------------------

EMAIL_REGEX = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
COUNTRY_REGEX = r"^[A-Z]{2}$"
POSTAL_REGEX = r"^[0-9A-Za-z][0-9A-Za-z \-]{2,9}$"
VAT_REGEX = r"^[A-Z]{2}[0-9A-Z]{8,12}$"
GTIN_REGEX = r"^\d{13}$"

CUSTOMER_DEFINITION: dict[str, Any] = {
    "attributes": [
        {"name": "customer_number", "type": "string", "required": True, "unique": True,
         "description": "The delivering system's own customer number — its natural key"},
        {"name": "name", "type": "string", "required": True},
        {"name": "legal_name", "type": "string"},
        {"name": "org_number", "type": "string"},
        {"name": "vat_number", "type": "string", "regex": VAT_REGEX},
        {"name": "email", "type": "string", "classification": "pii", "regex": EMAIL_REGEX},
        {"name": "phone", "type": "string", "classification": "pii"},
        {"name": "website", "type": "string"},
        {"name": "street", "type": "string", "classification": "pii"},
        {"name": "postal_code", "type": "string", "regex": POSTAL_REGEX},
        {"name": "city", "type": "string"},
        {"name": "country", "type": "string", "regex": COUNTRY_REGEX},
        {"name": "industry", "type": "string"},
        {"name": "segment", "type": "enum",
         "enum_values": ["enterprise", "mid-market", "small-business", "public-sector"]},
        {"name": "status", "type": "enum",
         "enum_values": ["active", "prospect", "inactive", "churned"]},
        {"name": "annual_revenue_sek", "type": "number", "min_value": 0},
        {"name": "employees", "type": "number", "min_value": 0, "max_value": 500000},
        {"name": "customer_since", "type": "date"},
    ],
    # The business key is the registration number, NOT `customer_number`. Each system has
    # its own customer number, so keying on it mints one entity per system — deterministic
    # resolution would have nothing to resolve. The registration number is the identifier
    # the systems actually share, which is what makes it a business key.
    "keys": [{"name": "org_number", "attributes": ["org_number"]}],
}

SUPPLIER_DEFINITION: dict[str, Any] = {
    "attributes": [
        {"name": "supplier_number", "type": "string", "required": True, "unique": True},
        {"name": "name", "type": "string", "required": True},
        {"name": "org_number", "type": "string"},
        {"name": "vat_number", "type": "string", "regex": VAT_REGEX},
        {"name": "email", "type": "string", "classification": "pii", "regex": EMAIL_REGEX},
        {"name": "phone", "type": "string", "classification": "pii"},
        {"name": "street", "type": "string"},
        {"name": "postal_code", "type": "string", "regex": POSTAL_REGEX},
        {"name": "city", "type": "string"},
        {"name": "country", "type": "string", "regex": COUNTRY_REGEX},
        {"name": "category", "type": "enum", "enum_values": SUPPLIER_CATEGORIES},
        {"name": "payment_terms", "type": "enum",
         "enum_values": ["net-15", "net-30", "net-45", "net-60"]},
        {"name": "status", "type": "enum", "enum_values": ["active", "on-hold", "inactive"]},
        {"name": "preferred", "type": "boolean"},
        {"name": "contact_name", "type": "string", "classification": "pii"},
        {"name": "contract_start", "type": "date"},
        {"name": "spend_ytd_sek", "type": "number", "min_value": 0},
    ],
    "keys": [{"name": "org_number", "attributes": ["org_number"]}],
}

PRODUCT_DEFINITION: dict[str, Any] = {
    "attributes": [
        {"name": "sku", "type": "string", "required": True, "unique": True},
        {"name": "name", "type": "string", "required": True},
        {"name": "description", "type": "string"},
        {"name": "brand", "type": "string"},
        {"name": "category", "type": "enum", "enum_values": PRODUCT_CATEGORIES},
        {"name": "uom", "type": "enum", "enum_values": ["pcs", "box", "pallet", "kg", "m"]},
        {"name": "gtin", "type": "string", "regex": GTIN_REGEX},
        {"name": "list_price", "type": "number", "min_value": 0},
        {"name": "currency", "type": "enum", "enum_values": ["SEK", "EUR", "USD", "NOK", "DKK"]},
        {"name": "weight_kg", "type": "number", "min_value": 0},
        {"name": "status", "type": "enum",
         "enum_values": ["active", "discontinued", "pre-launch"]},
        {"name": "supplier_number", "type": "string",
         "description": "The supplying vendor's number — links a product to a Supplier"},
        {"name": "launch_date", "type": "date"},
    ],
    # Each system has its own article number; the GTIN is the one both print on the box.
    "keys": [{"name": "gtin", "attributes": ["gtin"]}],
}


@dataclass(frozen=True)
class SourceSpec:
    """One originating system delivering into one model.

    `field_map` is the source-field -> model-attribute map stored on the Source, so the
    generated payload speaks the system's own vocabulary and Masterly conforms it at the
    door. An empty map means the system already speaks the model's names (passthrough).
    """

    name: str
    system_type: str
    key_attribute: str
    key_template: str
    coverage: float  # share of the population this system carries
    field_map: dict[str, str] = field(default_factory=dict)
    drops: tuple[str, ...] = ()  # attributes this system simply does not hold
    upper_names: bool = False
    long_legal_form: bool = False
    strip_legal_form: bool = False
    compact_postal: bool = False
    phone_style: str = "international"  # international | national | digits
    noise: float = 0.35  # per-record chance of each cosmetic variation


@dataclass(frozen=True)
class Defect:
    """A deliberate structural violation — what lands a record in quarantine, and why."""

    label: str
    attribute: str
    value: Any  # None means "drop the attribute entirely"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    definition: dict[str, Any]
    description: str
    sources: tuple[SourceSpec, ...]
    defects: tuple[Defect, ...]
    # Probabilistic matching for the records the business key cannot resolve — a system that
    # does not carry the registration number, or a record where it is missing. Blocking is on
    # `city` because the normalizer lowercases and collapses whitespace but does not remove
    # it: the ERP's upper-cased city still blocks with the CRM's, while `111 22` and `11122`
    # would not have.
    match: dict[str, Any] = field(default_factory=dict)


CUSTOMER_SOURCES = (
    SourceSpec(
        name="crm",
        system_type="salesforce",
        key_attribute="customer_number",
        key_template="0015g{index:06d}",
        coverage=0.92,
        field_map={
            "AccountNumber": "customer_number", "Account_Name": "name",
            "Legal_Name__c": "legal_name", "Registration_No__c": "org_number",
            "VAT_Number__c": "vat_number", "Email": "email", "Phone": "phone",
            "Website": "website", "BillingStreet": "street",
            "BillingPostalCode": "postal_code", "BillingCity": "city",
            "BillingCountryCode": "country", "Industry": "industry",
            "Customer_Segment__c": "segment", "Account_Status__c": "status",
            "AnnualRevenue": "annual_revenue_sek", "NumberOfEmployees": "employees",
            "Customer_Since__c": "customer_since",
        },
    ),
    SourceSpec(
        name="erp",
        system_type="sap",
        key_attribute="customer_number",
        key_template="KU{index:07d}",
        coverage=0.74,
        field_map={
            "KUNNR": "customer_number", "NAME1": "name", "NAME2": "legal_name",
            "STCD1": "org_number", "STCEG": "vat_number", "SMTP_ADDR": "email",
            "TELF1": "phone", "STRAS": "street", "PSTLZ": "postal_code", "ORT01": "city",
            "LAND1": "country", "BRSCH": "industry", "KTOKD": "segment",
            "LOEVM": "status", "UMSA1": "annual_revenue_sek", "ERDAT": "customer_since",
        },
        drops=("website", "employees"),
        upper_names=True,
        compact_postal=True,
        phone_style="digits",
    ),
    SourceSpec(
        name="webshop",
        system_type="webshop",
        key_attribute="customer_number",
        key_template="WS-{index:06d}",
        coverage=0.38,
        # Passthrough: this system already speaks the model's attribute names.
        field_map={},
        drops=("org_number", "vat_number", "industry", "annual_revenue_sek", "employees",
               "legal_name", "customer_since"),
        strip_legal_form=True,
        phone_style="national",
        noise=0.5,
    ),
)

SUPPLIER_SOURCES = (
    SourceSpec(
        name="erp-suppliers",
        system_type="sap",
        key_attribute="supplier_number",
        key_template="LI{index:07d}",
        coverage=0.95,
        field_map={
            "LIFNR": "supplier_number", "NAME1": "name", "STCD1": "org_number",
            "STCEG": "vat_number", "SMTP_ADDR": "email", "TELF1": "phone",
            "STRAS": "street", "PSTLZ": "postal_code", "ORT01": "city", "LAND1": "country",
            "MATKL": "category", "ZTERM": "payment_terms", "SPERR": "status",
            "XERSY": "preferred", "ERDAT": "contract_start", "UMSA1": "spend_ytd_sek",
        },
        drops=("contact_name",),
        upper_names=True,
        compact_postal=True,
        phone_style="digits",
    ),
    SourceSpec(
        name="procurement-portal",
        system_type="procurement",
        key_attribute="supplier_number",
        key_template="VEND-{index:05d}",
        coverage=0.55,
        field_map={
            "vendor_id": "supplier_number", "vendor_name": "name",
            "registration_number": "org_number", "vat_id": "vat_number",
            "contact_email": "email", "contact_phone": "phone",
            "contact_person": "contact_name", "address_line": "street",
            "zip": "postal_code", "town": "city", "country_code": "country",
            "spend_category": "category", "terms": "payment_terms",
            "vendor_status": "status", "is_preferred": "preferred",
            "contract_start_date": "contract_start", "ytd_spend": "spend_ytd_sek",
        },
        long_legal_form=True,
        noise=0.45,
    ),
)

PRODUCT_SOURCES = (
    SourceSpec(
        name="pim",
        system_type="pim",
        key_attribute="sku",
        key_template="ART-{index:06d}",
        coverage=0.9,
        field_map={
            "article_number": "sku", "article_name": "name", "long_description": "description",
            "brand_name": "brand", "product_group": "category", "unit": "uom",
            "ean": "gtin", "price": "list_price", "price_currency": "currency",
            "gross_weight": "weight_kg", "lifecycle": "status",
            "vendor_number": "supplier_number", "introduced_on": "launch_date",
        },
    ),
    SourceSpec(
        name="erp-products",
        system_type="sap",
        key_attribute="sku",
        key_template="MAT{index:08d}",
        coverage=0.72,
        field_map={
            "MATNR": "sku", "MAKTX": "name", "MTART": "category", "MEINS": "uom",
            "EAN11": "gtin", "NETPR": "list_price", "WAERS": "currency",
            "BRGEW": "weight_kg", "MSTAE": "status", "LIFNR": "supplier_number",
        },
        drops=("description", "brand", "launch_date"),
        upper_names=True,
    ),
)

MODEL_SPECS: dict[str, ModelSpec] = {
    "customer": ModelSpec(
        name="Customer",
        definition=CUSTOMER_DEFINITION,
        description="Customer organizations as delivered by CRM, ERP and the webshop",
        sources=CUSTOMER_SOURCES,
        defects=(
            Defect("malformed e-mail", "email", "info(at)example"),
            Defect("country spelled out", "country", "Sweden"),
            Defect("unknown status code", "status", "ACTIVE_2"),
            Defect("required name empty", "name", ""),
            Defect("negative employee count", "employees", -12),
            Defect("revenue delivered as text", "annual_revenue_sek", "unknown"),
            Defect("no natural key", "customer_number", None),
        ),
        match={
            "attributes": ["name", "street", "postal_code", "city", "email", "phone"],
            "auto_threshold": 0.86,
            "review_threshold": 0.66,
            "blocking": {"attribute": "city", "strategy": "first-token"},
        },
    ),
    "supplier": ModelSpec(
        name="Supplier",
        definition=SUPPLIER_DEFINITION,
        description="Vendors as delivered by the ERP and the procurement portal",
        sources=SUPPLIER_SOURCES,
        defects=(
            Defect("malformed e-mail", "email", "purchasing@"),
            Defect("unknown payment terms", "payment_terms", "net-90"),
            Defect("required name empty", "name", ""),
            Defect("negative YTD spend", "spend_ytd_sek", -4500),
            Defect("no natural key", "supplier_number", None),
        ),
        match={
            "attributes": ["name", "street", "postal_code", "city", "email", "phone"],
            "auto_threshold": 0.86,
            "review_threshold": 0.66,
            "blocking": {"attribute": "city", "strategy": "first-token"},
        },
    ),
    "product": ModelSpec(
        name="Product",
        definition=PRODUCT_DEFINITION,
        description="Articles as delivered by the PIM and the ERP material master",
        sources=PRODUCT_SOURCES,
        defects=(
            Defect("GTIN too short", "gtin", "73123456"),
            Defect("unknown unit of measure", "uom", "each"),
            Defect("negative price", "list_price", -19.9),
            Defect("required name empty", "name", ""),
            Defect("no natural key", "sku", None),
        ),
        match={
            "attributes": ["name", "brand", "category", "uom"],
            "auto_threshold": 0.9,
            "review_threshold": 0.72,
            "blocking": {"attribute": "category", "strategy": "first-token"},
        },
    ),
}

MODEL_SOURCE_LABELS: dict[str, set[str]] = {
    spec.name: {source.name for source in spec.sources} for spec in MODEL_SPECS.values()
}

# --------------------------------------------------------------------------------------
# Canonical entities — one dict per real-world thing, keyed by model attribute name.
# --------------------------------------------------------------------------------------


def _slug(value: str) -> str:
    """ASCII, lower-case, hyphenated — for the fabricated `.example` domains."""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")


def _unique_company_names(rng: random.Random, count: int) -> list[tuple[str, str]]:
    """`count` distinct (stem, qualifier) pairs, so no two entities share a base name."""
    pairs = [(s, q) for s in COMPANY_STEMS for q in COMPANY_QUALIFIERS]
    rng.shuffle(pairs)
    if count <= len(pairs):
        return pairs[:count]
    out = list(pairs)
    round_number = 2
    while len(out) < count:  # exhausted the space — widen it rather than repeat a name
        for stem, qualifier in pairs:
            out.append((f"{stem} {round_number}", qualifier))
            if len(out) == count:
                break
        round_number += 1
    return out


def _phone(rng: random.Random, dial_code: str) -> str:
    return f"{dial_code} {rng.randint(100, 999)} {rng.randint(10, 99)} {rng.randint(10, 99)}"


def _past_date(rng: random.Random, max_years: int) -> str:
    return (date.today() - timedelta(days=rng.randint(30, 365 * max_years))).isoformat()


def make_customers(rng: random.Random, count: int) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for stem, qualifier in _unique_company_names(rng, count):
        city, country, postal, dial = rng.choice(CITIES)
        legal_form = LEGAL_FORMS[country]
        base = f"{stem} {qualifier}"
        domain = f"{_slug(base)}.example"
        employees = rng.choice([4, 12, 28, 65, 140, 320, 850, 2400, 6100])
        entities.append({
            "name": f"{base} {legal_form}",
            "legal_name": f"{base} {legal_form}",
            "org_number": f"{rng.randint(550000, 559999)}-{rng.randint(1000, 9999)}",
            "vat_number": f"{country}{rng.randint(10**9, 10**10 - 1)}01",
            "email": f"info@{domain}",
            "phone": _phone(rng, dial),
            "website": f"https://www.{domain}",
            "street": f"{rng.choice(STREETS)} {rng.randint(1, 180)}",
            "postal_code": postal,
            "city": city,
            "country": country,
            "industry": rng.choice(INDUSTRIES),
            "segment": (
                "enterprise" if employees > 1000
                else "mid-market" if employees > 100
                else "small-business"
            ),
            "status": rng.choices(
                ["active", "prospect", "inactive", "churned"], weights=[70, 15, 10, 5]
            )[0],
            "annual_revenue_sek": float(employees * rng.randint(900, 2600) * 1000),
            "employees": employees,
            "customer_since": _past_date(rng, 12),
        })
    return entities


def make_suppliers(rng: random.Random, count: int) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for stem, qualifier in _unique_company_names(rng, count):
        city, country, postal, dial = rng.choice(CITIES)
        legal_form = LEGAL_FORMS[country]
        base = f"{stem} {qualifier}"
        domain = f"{_slug(base)}.example"
        entities.append({
            "name": f"{base} {legal_form}",
            "org_number": f"{rng.randint(550000, 559999)}-{rng.randint(1000, 9999)}",
            "vat_number": f"{country}{rng.randint(10**9, 10**10 - 1)}01",
            "email": f"orders@{domain}",
            "phone": _phone(rng, dial),
            "street": f"{rng.choice(STREETS)} {rng.randint(1, 180)}",
            "postal_code": postal,
            "city": city,
            "country": country,
            "category": rng.choice(SUPPLIER_CATEGORIES),
            "payment_terms": rng.choices(
                ["net-15", "net-30", "net-45", "net-60"], weights=[10, 55, 20, 15]
            )[0],
            "status": rng.choices(["active", "on-hold", "inactive"], weights=[80, 8, 12])[0],
            "preferred": rng.random() < 0.25,
            "contact_name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
            "contract_start": _past_date(rng, 8),
            "spend_ytd_sek": round(rng.uniform(15_000, 9_500_000), 2),
        })
    return entities


def make_products(rng: random.Random, count: int) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for index in range(count):
        category = rng.choice(PRODUCT_CATEGORIES)
        noun = rng.choice(PRODUCT_NOUNS[category])
        material = rng.choice(PRODUCT_MATERIALS)
        size = rng.choice(["M6", "M8", "M10", "M12", "8 mm", "12 mm", "1 l", "5 l", "25 m"])
        brand = rng.choice(PRODUCT_BRANDS)
        price = round(rng.uniform(9, 4200), 2)
        entities.append({
            "name": f"{noun} {size} {material}",
            "description": f"{brand} {noun.lower()} in {material}, size {size}.",
            "brand": brand,
            "category": category,
            "uom": rng.choices(["pcs", "box", "pallet", "kg", "m"], weights=[60, 20, 5, 8, 7])[0],
            "gtin": f"73{rng.randint(10**10, 10**11 - 1)}",
            "list_price": price,
            "currency": rng.choices(
                ["SEK", "EUR", "USD", "NOK", "DKK"], weights=[60, 20, 8, 6, 6]
            )[0],
            "weight_kg": round(rng.uniform(0.01, 42.0), 3),
            "status": rng.choices(
                ["active", "discontinued", "pre-launch"], weights=[82, 12, 6]
            )[0],
            "supplier_number": f"LI{rng.randint(1, 9999):07d}",
            "launch_date": _past_date(rng, 10),
            "_index": index,  # stripped before delivery; keeps SKUs stable across runs
        })
    return entities


ENTITY_FACTORIES = {
    "customer": make_customers,
    "supplier": make_suppliers,
    "product": make_products,
}

# --------------------------------------------------------------------------------------
# Per-source variation. The same entity, as each system actually holds and formats it.
# --------------------------------------------------------------------------------------


def _typo(rng: random.Random, value: str) -> str:
    """One plausible keying slip: transpose, drop, or double a character."""
    if len(value) < 4:
        return value
    position = rng.randrange(1, len(value) - 1)
    kind = rng.choice(["transpose", "drop", "double"])
    if kind == "transpose":
        return value[:position] + value[position + 1] + value[position] + value[position + 2:]
    if kind == "drop":
        return value[:position] + value[position + 1:]
    return value[:position] + value[position] + value[position:]


def _abbreviate_street(value: str) -> str:
    for long_form, short_form in STREET_ABBREVIATIONS.items():
        if long_form in value:
            return value.replace(long_form, short_form)
    return value


def _legal_form_of(name: str) -> str | None:
    for form in LEGAL_FORMS.values():
        if name.endswith(f" {form}"):
            return form
    return None


def _reformat_phone(value: str, style: str) -> str:
    if style == "international":
        return value
    digits = re.sub(r"\D", "", value)
    if style == "digits":
        return digits
    # national: drop the country code, restore the leading zero
    country_code_length = 2 if digits.startswith(("46", "47", "45", "44", "49", "31", "33", "48")) \
        else 3
    return "0" + digits[country_code_length:]


def render(
    entity: dict[str, Any], spec: SourceSpec, key: str, rng: random.Random
) -> dict[str, Any]:
    """One system's delivery of one entity: its fields, its formatting, its drift."""
    values = {k: v for k, v in entity.items() if not k.startswith("_")}
    values[spec.key_attribute] = key
    for dropped in spec.drops:
        values.pop(dropped, None)

    name = values.get("name")
    if isinstance(name, str) and name:
        legal_form = _legal_form_of(name)
        if legal_form and spec.strip_legal_form:
            name = name[: -len(legal_form) - 1]
        elif legal_form and spec.long_legal_form:
            name = f"{name[: -len(legal_form) - 1]} {LEGAL_FORM_LONG[legal_form]}"
        if spec.upper_names:
            name = name.upper()
        if rng.random() < spec.noise * 0.4:
            name = _typo(rng, name)
        if rng.random() < spec.noise * 0.3:
            name = f"{name} "  # trailing whitespace, straight from a spreadsheet paste
        values["name"] = name
        if "legal_name" in values and spec.upper_names:
            values["legal_name"] = str(values["legal_name"]).upper()

    if "street" in values:
        street = str(values["street"])
        if rng.random() < spec.noise:
            street = _abbreviate_street(street)
        if spec.upper_names:
            street = street.upper()
        values["street"] = street

    if "postal_code" in values and spec.compact_postal:
        values["postal_code"] = str(values["postal_code"]).replace(" ", "")

    if "phone" in values:
        values["phone"] = _reformat_phone(str(values["phone"]), spec.phone_style)
        if rng.random() < spec.noise * 0.25:
            values["phone"] = _typo(rng, str(values["phone"]))

    if "city" in values and spec.upper_names:
        values["city"] = str(values["city"]).upper()

    if "email" in values and rng.random() < spec.noise * 0.35:
        # A named mailbox instead of the generic one — same company, different contact.
        domain = str(values["email"]).split("@", 1)[-1]
        person = f"{rng.choice(FIRST_NAMES)}.{rng.choice(LAST_NAMES)}"
        values["email"] = f"{_slug(person)}@{domain}"

    # Optional attributes go missing in the field; that is what completeness scoring is for.
    for optional in ("website", "industry", "org_number", "vat_number", "description", "brand"):
        if optional in values and rng.random() < spec.noise * 0.2:
            values.pop(optional)

    return values


def to_wire(values: dict[str, Any], spec: SourceSpec) -> dict[str, Any]:
    """Rename model attributes to the delivering system's own field names."""
    if not spec.field_map:
        return values
    attribute_to_field = {attribute: f for f, attribute in spec.field_map.items()}
    return {attribute_to_field.get(k, k): v for k, v in values.items()}


def apply_defect(values: dict[str, Any], defect: Defect) -> dict[str, Any]:
    broken = dict(values)
    if defect.value is None:
        broken.pop(defect.attribute, None)
    else:
        broken[defect.attribute] = defect.value
    return broken


@dataclass
class Batch:
    """What one source is about to be handed, plus what we expect Masterly to make of it."""

    spec: SourceSpec
    model: str
    records: list[dict[str, Any]]
    clean: int = 0
    duplicates: int = 0
    defective: int = 0


def build_batches(
    model_key: str,
    count: int,
    rng: random.Random,
    *,
    duplicate_rate: float,
    defect_rate: float,
) -> list[Batch]:
    """Generate the population once, then deliver it through every source system."""
    model_spec = MODEL_SPECS[model_key]
    entities = ENTITY_FACTORIES[model_key](rng, count)
    batches: list[Batch] = []

    for spec in model_spec.sources:
        batch = Batch(spec=spec, model=model_spec.name, records=[])
        # Only defects on fields this system actually delivers — corrupting a field it does
        # not hold would look like schema drift rather than a data-quality problem.
        defects = [d for d in model_spec.defects if d.attribute not in spec.drops]
        for index, entity in enumerate(entities):
            if rng.random() > spec.coverage:
                continue  # this system simply does not carry this entity
            key = spec.key_template.format(index=index + 1)
            values = render(entity, spec, key, rng)

            if rng.random() < defect_rate:
                defect = rng.choice(defects)
                batch.records.append(to_wire(apply_defect(values, defect), spec))
                batch.defective += 1
            else:
                batch.records.append(to_wire(values, spec))
                batch.clean += 1

            # The same entity entered twice in the same system under a second key — the
            # intra-source duplicate Identity Resolution has to catch.
            if rng.random() < duplicate_rate:
                twin_key = spec.key_template.format(index=900000 + index + 1)
                twin = render(entity, spec, twin_key, rng)
                twin = apply_twin_drift(twin, rng)
                batch.records.append(to_wire(twin, spec))
                batch.duplicates += 1
        batches.append(batch)
    return batches


def apply_twin_drift(values: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """A re-keyed duplicate is never a byte-for-byte copy — someone typed it again."""
    twin = dict(values)
    if isinstance(twin.get("name"), str) and rng.random() < 0.6:
        twin["name"] = _typo(rng, str(twin["name"]))
    if "street" in twin and rng.random() < 0.5:
        twin["street"] = _abbreviate_street(str(twin["street"]))
    if "phone" in twin and rng.random() < 0.4:
        twin.pop("phone")
    if "email" in twin and rng.random() < 0.3:
        twin.pop("email")
    return twin


# --------------------------------------------------------------------------------------
# Adaptive mode: generate for a model that already exists, from its own definition.
# --------------------------------------------------------------------------------------

_ADAPTIVE_HINTS: list[tuple[str, str]] = [
    ("email", "email"), ("mail", "email"), ("phone", "phone"), ("tel", "phone"),
    ("street", "street"), ("address", "street"), ("postal", "postal"), ("zip", "postal"),
    ("city", "city"), ("town", "city"), ("country", "country"), ("website", "url"),
    ("url", "url"), ("name", "name"), ("price", "money"), ("amount", "money"),
    ("revenue", "money"), ("spend", "money"), ("cost", "money"), ("date", "date"),
    ("number", "code"), ("code", "code"), ("id", "code"), ("sku", "code"),
]


def _hint_for(attribute_name: str) -> str | None:
    lowered = attribute_name.lower()
    for needle, hint in _ADAPTIVE_HINTS:
        if needle in lowered:
            return hint
    return None


def adaptive_value(
    attribute: dict[str, Any], rng: random.Random, index: int
) -> Any:
    """A plausible value for an attribute we have never seen, from its type and its name."""
    attribute_type = attribute.get("type", "string")
    if attribute_type == "enum":
        return rng.choice(attribute.get("enum_values") or ["unknown"])
    if attribute_type == "boolean":
        return rng.random() < 0.3
    if attribute_type in ("date", "datetime"):
        value = _past_date(rng, 6)
        return f"{value}T09:00:00Z" if attribute_type == "datetime" else value
    if attribute_type == "number":
        low = attribute.get("min_value")
        high = attribute.get("max_value")
        return round(rng.uniform(1 if low is None else low, 5000 if high is None else high), 2)
    if attribute_type in ("reference", "nested"):
        return None

    city, country, postal, dial = rng.choice(CITIES)
    stem, qualifier = rng.choice(COMPANY_STEMS), rng.choice(COMPANY_QUALIFIERS)
    match _hint_for(str(attribute.get("name", ""))):
        case "email":
            return f"info@{_slug(f'{stem} {qualifier}')}.example"
        case "phone":
            return _phone(rng, dial)
        case "street":
            return f"{rng.choice(STREETS)} {rng.randint(1, 180)}"
        case "postal":
            return postal
        case "city":
            return city
        case "country":
            return country
        case "url":
            return f"https://www.{_slug(f'{stem} {qualifier}')}.example"
        case "name":
            return f"{stem} {qualifier} {LEGAL_FORMS[country]}"
        case "money":
            return round(rng.uniform(100, 900_000), 2)
        case "date":
            return _past_date(rng, 6)
        case "code":
            return f"DEMO-{index:06d}"
        case _:
            return f"{stem} {qualifier}"


def unique_key_value(attribute: dict[str, Any], index: int) -> tuple[str, bool]:
    """A unique value for a key attribute that also satisfies its format, if it has one.

    A key has to be unique per record, which rules out the name-based heuristics — and it
    often carries a format (a GTIN is exactly thirteen digits). So try a few structurally
    different unique shapes and take the first the constraint accepts. Returns the value and
    whether the format could be satisfied; an unsatisfiable key is still emitted, because a
    record with no key at all quarantines just the same and says less about why.
    """
    candidates = [
        f"DEMO-{index:06d}",
        f"{index:013d}",
        f"{index:08d}",
        f"{index:d}",
    ]
    pattern = attribute.get("regex")
    if not pattern:
        return candidates[0], True
    for candidate in candidates:
        if re.search(pattern, candidate):
            return candidate, True
    return candidates[0], False


def build_adaptive_records(
    definition: dict[str, Any],
    key_attributes: list[str],
    count: int,
    rng: random.Random,
    *,
    defect_rate: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Records shaped by a live model definition. Returns the records plus any warnings.

    An attribute whose format constraint we cannot satisfy is left out when it is optional
    (better an incomplete record than a quarantined one) and filled anyway when it is
    required — with a warning, so the operator knows why those records will quarantine.
    """
    attributes: list[dict[str, Any]] = definition.get("attributes", [])
    warnings: list[str] = []
    skip: set[str] = set()

    probe = random.Random(0)
    for attribute in attributes:
        pattern = attribute.get("regex")
        if not pattern or attribute["name"] in key_attributes:
            continue
        sample = adaptive_value(attribute, probe, 1)
        if isinstance(sample, str) and not re.search(pattern, sample):
            if attribute.get("required"):
                warnings.append(
                    f"'{attribute['name']}' has a format this generator cannot satisfy "
                    f"({pattern}) and is required — those records will quarantine"
                )
            else:
                skip.add(attribute["name"])
                warnings.append(
                    f"'{attribute['name']}' has a format this generator cannot satisfy "
                    f"({pattern}) — leaving it empty"
                )

    records: list[dict[str, Any]] = []
    unsatisfiable_keys: set[str] = set()
    for index in range(1, count + 1):
        record: dict[str, Any] = {}
        for attribute in attributes:
            name = attribute["name"]
            if name in skip:
                continue
            if name in key_attributes:
                value, satisfied = unique_key_value(attribute, index)
                record[name] = value if len(key_attributes) == 1 else f"{value}-{name}"
                if not satisfied and name not in unsatisfiable_keys:
                    unsatisfiable_keys.add(name)
                    warnings.append(
                        f"'{name}' is the natural key and has a format this generator cannot "
                        f"satisfy ({attribute['regex']}) — those records will quarantine"
                    )
                continue
            value = adaptive_value(attribute, rng, index)
            if value is None:
                continue
            if not attribute.get("required") and rng.random() < 0.12:
                continue  # realistic gaps in the optional fields
            record[name] = value
        if rng.random() < defect_rate and attributes:
            target = rng.choice([a for a in attributes if a["name"] not in key_attributes]
                                or attributes)
            record[target["name"]] = "!!invalid!!"
        records.append(record)
    return records, warnings



# --------------------------------------------------------------------------------------
# Getting a connection. Everything after this point rides the SDK; these two calls come
# BEFORE there is a connection to ride — you cannot open a client for an Environment until
# you hold a token and know which Environment you are entitled to.
# --------------------------------------------------------------------------------------


def _post(base_url: str, path: str, payload: dict[str, Any], token: str | None = None) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = httpx.post(f"{base_url.rstrip('/')}{path}", json=payload, headers=headers,
                          timeout=30.0)
    if response.status_code >= 400:
        code, message = "HTTP_ERROR", response.text[:300]
        try:
            envelope = response.json().get("error", {})
            code, message = envelope.get("code", code), envelope.get("message", message)
        except ValueError:
            pass
        raise SystemExit(f"POST {path} failed — {code} ({response.status_code}): {message}")
    return response.json()


def login(base_url: str, email: str, organization: str | None) -> str:
    """Exchange a dev-binding credential for a session token (local and demo installs).

    The dev identity adapter takes the credential as ``dev:<email>``; on a real binding
    (Stytch, OIDC) pass a proper token with --token instead.
    """
    credential = email if email.startswith("dev:") else f"dev:{email}"
    session = _post(base_url, "/v1/auth/sessions", {"idp_token": credential})
    if session.get("organization_id") is None:
        organizations = session.get("organizations", [])
        if organization is None and len(organizations) != 1:
            names = ", ".join(o["organization_id"] for o in organizations) or "none"
            raise SystemExit(
                f"'{email}' belongs to several Organizations — pass --organization "
                f"(one of: {names})"
            )
        target = organization or organizations[0]["organization_id"]
        session = _post(
            base_url,
            "/v1/auth/sessions:exchange",
            {"organization_id": target},
            token=session["session_token"],
        )
    token: str = session["session_token"]
    return token


def resolve_environment(base_url: str, token: str, requested: str | None) -> str:
    """The Environment to connect to. `/v1/environments` is Organization-scoped, so it is
    answered before an Environment is chosen — which is why it is not on the client."""
    client = Client(base_url=base_url, token=token, environment="")
    try:
        page = client.request("GET", "/v1/environments", params={"limit": 200})
    finally:
        client.close()
    environments = page.get("items", [])
    known = {e["environment_id"] for e in environments}
    if requested:
        if requested not in known:
            raise SystemExit(
                f"Environment '{requested}' is not one of yours ({', '.join(sorted(known))})"
            )
        return requested
    active = [e for e in environments if e.get("status") == "active"]
    if len(active) == 1:
        return str(active[0]["environment_id"])
    listing = ", ".join(f"{e['environment_id']} ({e['name']})" for e in environments) or "none"
    raise SystemExit(f"Pass --environment — this Organization has: {listing}")


# --------------------------------------------------------------------------------------
# Bootstrap, over the SDK's configuration surface. Every step is find-or-create, so a
# second run changes nothing.
# --------------------------------------------------------------------------------------


def ensure_workspace(client: Client, name: str) -> str:
    for workspace in client.workspaces.list():
        if workspace["name"] == name:
            return str(workspace["workspace_id"])
    created = client.workspaces.create(name, description="Generated demo data")
    print(f"  created Workspace '{name}'")
    return str(created["workspace_id"])


def ensure_domain(client: Client, workspace_id: str, name: str) -> str:
    for domain in client.domains.list(workspace=workspace_id):
        if domain["name"] == name:
            return str(domain["domain_id"])
    created = client.domains.create(name, workspace=workspace_id,
                                    description="Generated demo data")
    print(f"  created Domain '{name}'")
    return str(created["domain_id"])


def check_model_is_ours(model: dict[str, Any], spec: ModelSpec) -> None:
    """Refuse to deliver into a same-named model that is not the one this script defines.

    Models are addressed by name inside an Environment, so a leftover `Customer` from
    someone else's seed is found by exactly the same lookup as our own. Adopting it silently
    is the worst outcome: the records are generated for OUR definition, so they quarantine in
    bulk against theirs, and the run reports success. Cheap to check, and re-running against
    a model this script created passes it without noise.
    """
    existing = {a["name"] for a in model.get("definition", {}).get("attributes", [])}
    ours = {a["name"] for a in spec.definition["attributes"]}
    missing = sorted(ours - existing)
    demanded = sorted(
        a["name"]
        for a in model.get("definition", {}).get("attributes", [])
        if a.get("required") and a["name"] not in ours
    )
    if not missing and not demanded:
        return
    detail = []
    if missing:
        detail.append(f"it lacks {len(missing)} attribute(s) this script generates: "
                      f"{', '.join(missing[:5])}{' …' if len(missing) > 5 else ''}")
    if demanded:
        detail.append(f"it requires {', '.join(demanded)}, which this script does not generate")
    raise SystemExit(
        f"Data Model '{spec.name}' already exists in this Environment and is not the one this "
        f"script defines — {'; and '.join(detail)}. Delivering into it would quarantine most of "
        f"the batch. Use a clean Environment, remove the existing model, or generate for it on "
        f"its own terms with --model '{spec.name}' --source <name>."
    )


def _keys_of(definition: dict[str, Any]) -> list[list[str]]:
    return [list(k.get("attributes", [])) for k in definition.get("keys", [])]


def ensure_model(client: Client, domain_id: str, spec: ModelSpec) -> dict[str, Any]:
    for listed in client.data_models.list():
        if listed["name"] != spec.name:
            continue
        check_model_is_ours(listed, spec)
        # Ours, but possibly from an older run of this script. A stale business key is not
        # cosmetic — it decides whether deterministic resolution has anything to resolve —
        # so bring the definition up to date. Read first: the write states the revision it
        # replaces, and a concurrent edit is refused rather than overwritten.
        model = client.data_models.get(spec.name)
        if _keys_of(model["definition"]) != _keys_of(spec.definition):
            model = client.data_models.update(
                spec.name, definition=spec.definition, if_match=model["version"]
            )
            # Changing a business key is a breaking change, and a production Environment
            # refuses it outright — which is correct: this script is not for production.
            client.data_models.publish(spec.name, allow_breaking=True)
            print(f"  updated Data Model '{spec.name}' — business key was stale")
        elif model["status"] == "draft":
            client.data_models.publish(spec.name)
            print(f"  published existing Data Model '{spec.name}'")
        return dict(model)
    created = client.data_models.create(
        spec.name,
        domain=domain_id,
        definition=spec.definition,
        kind="entity",
        description=spec.description,
        tags=["demo-data"],
    )
    client.data_models.publish(str(created["model_id"]))
    print(f"  created Data Model '{spec.name}' ({len(spec.definition['attributes'])} attributes)")
    return dict(created)


def ensure_source(client: Client, spec: SourceSpec, model_name: str) -> dict[str, Any]:
    for source in client.sources.list():
        if source["name"] != spec.name:
            continue
        # Same trap as the model: a leftover source under our name feeds a different model,
        # or keys records on a different attribute. Both silently misfile the whole batch.
        if source["target_model"] != model_name:
            raise SystemExit(
                f"Source '{spec.name}' already exists in this Environment and feeds "
                f"'{source['target_model']}', not '{model_name}'. Use a clean Environment, "
                f"remove it, or deliver to it on its own terms with --model/--source."
            )
        key = source.get("mapping", {}).get("source_key")
        if key and key != [spec.key_attribute]:
            raise SystemExit(
                f"Source '{spec.name}' keys its records on {key}, but this script delivers "
                f"'{spec.key_attribute}' as the natural key. Remove the existing source, or "
                f"deliver to it on its own terms with --model/--source."
            )
        # A source with no declared source key quarantines every record it is handed. Repair
        # it through the mapping it already has, so nothing else on the document is lost.
        if not key:
            mapping = {**source.get("mapping", {}), "source_key": [spec.key_attribute]}
            source = client.sources.update(str(source["source_id"]), mapping=mapping)
            print(f"  repaired Source '{spec.name}' — it had no source key")
        return dict(source)
    created = client.sources.create(
        spec.name,
        target_model=model_name,
        source_key=spec.key_attribute,
        field_map=spec.field_map,
        system_type=spec.system_type,
    )
    print(f"  created Source '{spec.name}' -> {model_name}")
    return dict(created)


def ensure_match_config(client: Client, spec: ModelSpec) -> None:
    """Install the probabilistic matching for what the business key cannot resolve.

    Deterministic resolution links the records that carry the shared registration number.
    Everything else — the webshop, which never had one, and records where it went missing —
    reaches identity resolution unlinked, and without a match config it simply mints a new
    entity each time. There is no typed method for this yet, so it goes through the client's
    escape hatch.
    """
    if not spec.match:
        return
    current = client.request("GET", f"/v1/match/config/{spec.name}")
    if current == spec.match:
        return
    client.request("PUT", f"/v1/match/config/{spec.name}", json=spec.match)
    thresholds = f"auto {spec.match['auto_threshold']}, review {spec.match['review_threshold']}"
    print(f"  match config for '{spec.name}' ({thresholds})")


def require_source(client: Client, name: str) -> dict[str, Any]:
    try:
        return client.sources.get(name)
    except LookupError as missing:
        raise SystemExit(f"{missing} in this Environment") from missing


def require_model(client: Client, name: str) -> dict[str, Any]:
    try:
        return client.data_models.get(name)
    except LookupError as missing:
        raise SystemExit(f"{missing} in this Environment") from missing


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def print_plan(batches: list[Batch]) -> None:
    print(f"\n{'source':<22}{'model':<12}{'records':>9}{'clean':>8}{'dup':>7}{'defect':>8}")
    print("-" * 66)
    for batch in batches:
        print(
            f"{batch.spec.name:<22}{batch.model:<12}{len(batch.records):>9}"
            f"{batch.clean:>8}{batch.duplicates:>7}{batch.defective:>8}"
        )
    total = sum(len(b.records) for b in batches)
    print("-" * 66)
    print(f"{'total':<34}{total:>9}\n")


def golden_count(client: Client, model: str) -> int:
    """How many entities Identity Resolution built for a model.

    A one-row page carries the exact `total` where the API can count cheaply, which is the
    whole answer for one request; only where it cannot does this walk the listing. There is
    no typed method for a count, so it goes through the client's own escape hatch.
    """
    page = client.request("GET", "/v1/golden", params={"model": model, "limit": 1})
    total = page.get("total")
    if total is not None:
        return int(total)
    return sum(1 for _ in client.golden.list(model))


def wait_for_pipeline(
    client: Client, sources: list[tuple[str, str]], models: list[str], timeout: int
) -> None:
    """Ingest is asynchronous — poll each source's counters until they stop moving, then
    report what landed: records accepted, records quarantined, and the entities Identity
    Resolution built out of them."""
    print(f"waiting up to {timeout}s for the pipeline to drain...")
    deadline = time.time() + timeout
    previous: dict[str, tuple[int, int]] = {}
    stable_rounds = 0
    while time.time() < deadline:
        time.sleep(3)
        current = {
            label: (stats["records"], stats["quarantined"])
            for source_id, label in sources
            for stats in [client.sources.stats(source_id)]
        }
        stable_rounds = stable_rounds + 1 if current == previous else 0
        previous = current
        if stable_rounds >= 2:
            break

    print(f"\n{'source':<22}{'accepted':>10}{'quarantined':>13}")
    print("-" * 45)
    for label, (records, quarantined) in previous.items():
        print(f"{label:<22}{records:>10}{quarantined:>13}")
    print("-" * 45)
    totals = (sum(r for r, _ in previous.values()), sum(q for _, q in previous.values()))
    print(f"{'total':<22}{totals[0]:>10}{totals[1]:>13}\n")

    if models:
        print(f"{'model':<22}{'source records':>16}{'golden entities':>17}")
        print("-" * 55)
        for model in models:
            delivered = sum(
                records for label, (records, _) in previous.items()
                if label in MODEL_SOURCE_LABELS.get(model, set())
            )
            print(f"{model:<22}{delivered:>16}{golden_count(client, model):>17}")
        print()


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate realistic demo master data and ingest it into Masterly.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    connection = parser.add_argument_group("connection")
    connection.add_argument("--base-url", default=os.environ.get("MASTERLY_BASE_URL"),
                            help="the install's URL, e.g. http://localhost:8001")
    connection.add_argument("--token", default=os.environ.get("MASTERLY_TOKEN"),
                            help="a session token (env: MASTERLY_TOKEN)")
    connection.add_argument("--dev-login", default=os.environ.get("MASTERLY_DEV_LOGIN"),
                            metavar="EMAIL",
                            help="mint a token by e-mail — installs on the dev identity binding")
    connection.add_argument("--service-account-token",
                            default=os.environ.get("MASTERLY_SERVICE_ACCOUNT_TOKEN"),
                            metavar="TOKEN",
                            help="run as a machine: a service account's access token. It is "
                                 "pinned to its Environment and may only deliver into the "
                                 "Sources its `ingest` scope names, so pass those with "
                                 "--source-id (env: MASTERLY_SERVICE_ACCOUNT_TOKEN)")
    connection.add_argument("--organization", help="Organization to scope the session to")
    connection.add_argument("--environment", default=os.environ.get("MASTERLY_ENVIRONMENT"),
                            help="target Environment id; inferred when you have exactly one")

    shape = parser.add_argument_group("what to generate")
    shape.add_argument("--models", nargs="+", choices=sorted(MODEL_SPECS),
                       default=["customer", "supplier", "product"])
    shape.add_argument("--customers", type=int, default=250, help="distinct customer entities")
    shape.add_argument("--suppliers", type=int, default=60, help="distinct supplier entities")
    shape.add_argument("--products", type=int, default=150, help="distinct product entities")
    shape.add_argument("--seed", type=int, default=42, help="same seed, same data")
    shape.add_argument("--duplicate-rate", type=float, default=0.12, metavar="RATE",
                       help="share of entities also delivered as an intra-source duplicate")
    shape.add_argument("--defect-rate", type=float, default=0.06, metavar="RATE",
                       help="share of records carrying a structural defect (they quarantine)")

    existing = parser.add_argument_group("ingest into something that already exists")
    existing.add_argument("--no-bootstrap", action="store_true",
                          help="do not create Workspace/Domain/Models/Sources — they must exist")
    existing.add_argument("--model", metavar="NAME",
                          help="generate for this existing Data Model, from its own definition")
    existing.add_argument("--source", metavar="NAME",
                          help="deliver to this existing Source (required with --model)")
    existing.add_argument("--records", type=int, default=200,
                          help="records to generate in --model mode")
    existing.add_argument("--source-id", action="append", default=[], metavar="NAME=ID",
                          help="deliver the built-in Source NAME to this Source id, instead of "
                               "looking the name up. Repeatable; required with "
                               "--service-account-token, which cannot list an Environment")

    output = parser.add_argument_group("placement and output")
    output.add_argument("--workspace", default="Demo", help="Workspace to bootstrap into")
    output.add_argument("--domain", default="Demo data", help="Domain to bootstrap into")
    output.add_argument("--batch-size", type=int, default=500, help="records per ingest call")
    output.add_argument("--dry-run", action="store_true",
                        help="generate only — print or write the records, contact nothing")
    output.add_argument("--out", metavar="FILE", help="write the generated records to a JSON file")
    output.add_argument("--wait", action="store_true",
                        help="poll each source until the pipeline stops moving, then report")
    output.add_argument("--wait-timeout", type=int, default=120, metavar="SECONDS")
    return parser.parse_args(argv)


def parse_source_ids(pairs: list[str]) -> dict[str, str]:
    """`NAME=src_…` per built-in Source, for a run that is handed ids instead of looking
    names up. A service account is always handed them: resolving a name means listing the
    Environment, and listing is a session's privilege, not a scope's."""
    known = {source.name for spec in MODEL_SPECS.values() for source in spec.sources}
    mapping: dict[str, str] = {}
    for pair in pairs:
        name, _, source_id = pair.partition("=")
        if not name or not source_id:
            raise SystemExit(f"--source-id takes NAME=src_… (got '{pair}')")
        if name not in known:
            raise SystemExit(
                f"'{name}' is not one of the built-in Sources: {', '.join(sorted(known))}"
            )
        if not source_id.startswith("src_"):
            raise SystemExit(
                f"--source-id {name}= wants the Source's id (src_…), not '{source_id}'"
            )
        mapping[name] = source_id
    return mapping


def refuse_session_only_work(args: argparse.Namespace, source_ids: dict[str, str]) -> None:
    """What a service-account token cannot do, said before the run starts rather than as a
    401 halfway through it."""
    if args.token or args.dev_login:
        raise SystemExit(
            "pass one credential: --service-account-token, or --token/--dev-login — not both"
        )
    if args.model:
        raise SystemExit(
            "--model reads a Data Model's definition, which needs a session token. "
            "Generate for the built-in models instead, or run this with --token."
        )
    if args.wait:
        raise SystemExit(
            "--wait polls each Source's counters and counts golden entities, which needs a "
            "session token. Drop --wait, or run this with --token."
        )
    if not source_ids:
        known = sorted({source.name for spec in MODEL_SPECS.values() for source in spec.sources})
        raise SystemExit(
            "a service account cannot list an Environment, so name the Sources it may deliver "
            "into: --source-id NAME=src_… (built-in Sources: " + ", ".join(known) + ")"
        )


def connect(args: argparse.Namespace) -> Client:
    """The connection, in whichever persona the credential is.

    A service account is pinned to one Environment, so there is nothing to resolve: passing
    `--environment` alongside it asserts which Environment the run believes it is loading, and
    the server refuses the delivery rather than loading another one.
    """
    if args.service_account_token:
        asserted = f", asserting {args.environment}" if args.environment else ""
        print(f"connected to {args.base_url} as a service account{asserted}")
        return Client.for_service_account(
            args.base_url, args.service_account_token, environment=args.environment
        )
    token = args.token or login(args.base_url, args.dev_login, args.organization)
    environment = resolve_environment(args.base_url, token, args.environment)
    print(f"connected to {args.base_url} — Environment {environment}")
    return Client(base_url=args.base_url, token=token, environment=environment)


def generate_builtin(args: argparse.Namespace, rng: random.Random) -> list[Batch]:
    counts = {"customer": args.customers, "supplier": args.suppliers, "product": args.products}
    batches: list[Batch] = []
    for model_key in args.models:
        batches.extend(
            build_batches(
                model_key,
                counts[model_key],
                rng,
                duplicate_rate=args.duplicate_rate,
                defect_rate=args.defect_rate,
            )
        )
    return batches


def write_out(path: str, payload: dict[str, list[dict[str, Any]]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    print(f"wrote {sum(len(r) for r in payload.values())} records to {path}")


def run_dry(args: argparse.Namespace, rng: random.Random) -> int:
    if args.model:
        raise SystemExit("--model reads a live definition, so it cannot be combined with --dry-run")
    batches = generate_builtin(args, rng)
    print_plan(batches)
    if args.out:
        write_out(args.out, {b.spec.name: b.records for b in batches})
    else:
        for batch in batches:
            print(f"--- {batch.spec.name} (first 3 of {len(batch.records)}) ---")
            for record in batch.records[:3]:
                print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def plan_adaptive(
    client: Client, args: argparse.Namespace, rng: random.Random
) -> tuple[str, str, list[dict[str, Any]]]:
    """Records for a model that already exists, generated from its own live definition."""
    model = require_model(client, args.model)
    source = require_source(client, args.source)
    if source["target_model"] != model["name"]:
        raise SystemExit(
            f"Source '{source['name']}' feeds '{source['target_model']}', not '{model['name']}'"
        )
    mapping = source.get("mapping", {})
    key_attributes = mapping.get("source_key") or []
    if not key_attributes:
        raise SystemExit(
            f"Source '{source['name']}' declares no source key — every record would quarantine. "
            "Set mapping.source_key first."
        )
    records, warnings = build_adaptive_records(
        model["definition"], key_attributes, args.records, rng, defect_rate=args.defect_rate
    )
    attribute_to_field = {a: f for f, a in (mapping.get("field_map") or {}).items()}
    records = [{attribute_to_field.get(k, k): v for k, v in r.items()} for r in records]
    for warning in warnings:
        print(f"  note: {warning}")
    print(f"\ngenerated {len(records)} records for '{model['name']}' from its definition")
    return str(source["source_id"]), str(source["name"]), records


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rng = random.Random(args.seed)  # demo data, deliberately reproducible

    if args.model and not args.dry_run and not args.source:
        raise SystemExit("--model needs --source: the Source that delivers into that model")
    if args.dry_run:
        return run_dry(args, rng)

    if not args.base_url:
        raise SystemExit("pass --base-url (or set MASTERLY_BASE_URL), or use --dry-run")
    source_ids = parse_source_ids(args.source_id)
    machine = bool(args.service_account_token)
    if machine:
        refuse_session_only_work(args, source_ids)
    elif not args.token and not args.dev_login:
        raise SystemExit(
            "pass --token (or set MASTERLY_TOKEN), --dev-login EMAIL, or "
            "--service-account-token to run as a machine"
        )

    deliveries: list[tuple[str, str, list[dict[str, Any]]]] = []  # (source_id, label, records)
    batches: list[Batch] = []

    with connect(args) as client:
        if args.model:
            deliveries.append(plan_adaptive(client, args, rng))
        else:
            if machine:
                print("service account: skipping bootstrap — configuration is a session's work")
            elif not args.no_bootstrap:
                print("bootstrapping configuration:")
                workspace_id = ensure_workspace(client, args.workspace)
                domain_id = ensure_domain(client, workspace_id, args.domain)
                for model_key in args.models:
                    model_spec = MODEL_SPECS[model_key]
                    ensure_model(client, domain_id, model_spec)
                    ensure_match_config(client, model_spec)
                    for source_spec in model_spec.sources:
                        ensure_source(client, source_spec, model_spec.name)

            batches = generate_builtin(args, rng)
            if machine:
                # Generate first, then keep the named Sources: the seeded population is the
                # same records a full run would have delivered to them.
                skipped = sorted({b.spec.name for b in batches} - set(source_ids))
                batches = [b for b in batches if b.spec.name in source_ids]
                if not batches:
                    raise SystemExit(
                        "--source-id names no Source this run generates for "
                        f"(--models {' '.join(args.models)} generates: {', '.join(skipped)})"
                    )
                if skipped:
                    print(f"not named by --source-id, skipping: {', '.join(skipped)}")
            print_plan(batches)
            for batch in batches:
                if batch.spec.name in source_ids:
                    deliveries.append((source_ids[batch.spec.name], batch.spec.name, batch.records))
                    continue
                source = require_source(client, batch.spec.name)
                deliveries.append((str(source["source_id"]), batch.spec.name, batch.records))

        if args.out:
            write_out(args.out, {label: records for _, label, records in deliveries})

        for source_id, label, records in deliveries:
            try:
                report = client.sources.ingest(source_id, records, batch_size=args.batch_size)
            except ApiError as error:
                raise SystemExit(f"ingest into '{label}' failed — {error}") from error
            print(f"  {label}: {report.records} records accepted in {report.batches} batch(es)")

        if args.wait:
            wait_for_pipeline(
                client,
                [(source_id, label) for source_id, label, _ in deliveries],
                sorted({b.model for b in batches}),
                args.wait_timeout,
            )
        else:
            print("\nIngest is asynchronous — re-run with --wait to see where the records landed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
