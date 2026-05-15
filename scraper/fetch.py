#!/usr/bin/env python3
"""Build Fort Wayne / Allen County dashboard records.

This first live pipeline is intentionally conservative:
- it reads public Allen County/Fort Wayne source pages;
- it normalizes any safely parsed sheriff-sale PDF rows into dashboard records;
- it keeps tax delinquent/probate/code adapters explicit when a live source
  cannot be parsed safely yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from io import BytesIO
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from zipfile import ZipFile


ROOT = "https://www.allencounty.in.gov"
SHERIFF_ROOT = "https://www.allencountysheriff.org"
DELINQUENT_URL = f"{ROOT}/824/Delinquent-Property-List"
DELINQUENT_DOCUMENT_URL = f"{ROOT}/DocumentCenter/View/11377/2025-Delinquent-Property"
TAX_SALE_URL = f"{ROOT}/321/Tax-Sale"
SHERIFF_URL = f"{SHERIFF_ROOT}/sheriff-sale/"
SHERIFF_ARCHIVE_URL = f"{SHERIFF_ROOT}/2026-sheriff-sales/"
ASSESSOR_URL = f"{ROOT}/164/Assessor"
ASSESSOR_PARCEL_LAYER_URL = "https://gis1.acimap.us/imapweb/rest/services/QueryLayers/QueryLayers/MapServer/10"
FORT_WAYNE_311_URL = "https://www.cityoffortwayne.org/311"
FORT_WAYNE_311_INCIDENTS_URL = "https://fortwayne-citizen-services.thesmartcity311.com/getrecentIncidents"
COURT_CALENDAR_API = "https://public.courts.in.gov/CourtCal/api"
RECORDER_SEARCH_URL = "https://inallen.fidlar.com/INAllen/DirectSearch/"
RECORDER_API_BASE = "https://inallen.fidlar.com/INAllen/Scrap.WebService.DirectSearch/"
ALLEN_COUNTY_ID = 2
TAX_RECORD_LIMIT = 500
ASSESSOR_BATCH_SIZE = 10
ASSESSOR_FIELDS = [
    "GISPublished.SDE.Parcel_Poly.PIN",
    "GISPublished.SDE.Parcel_Poly.GIS_ID",
    "GISPublished.SDE.Parcel_Poly.PRCLID",
    "GISPublished.SDE.CurrentOwner.OwnerofRecord",
    "GISPublished.SDE.CurrentOwner.MailingAddress1",
    "GISPublished.SDE.CurrentOwner.MailingAddress2",
    "GISPublished.SDE.CurrentOwner.MailingCity",
    "GISPublished.SDE.CurrentOwner.MailingState",
    "GISPublished.SDE.CurrentOwner.MailingZip",
    "GISPublished.SDE.CurrentOwner.PropertyAddress1",
    "GISPublished.SDE.CurrentOwner.PropertyAddress2",
    "GISPublished.SDE.CurrentOwner.PropertyCity",
    "GISPublished.SDE.CurrentOwner.PropertyState",
    "GISPublished.SDE.CurrentOwner.PropertyZip",
]
RECORDER_DEED_TERMS = (
    "deed",
    "transfer on death",
    "affidavit",
    "survivorship",
    "personal representative",
    "executor",
    "administrator",
    "estate",
    "trust",
)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = dict(attrs)
        self._href = attrs_dict.get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((" ".join("".join(self._text).split()), self._href))
            self._href = None
            self._text = []


@dataclass
class SourceStatus:
    source: str
    url: str
    status: str
    detail: str


def fetch_text(url: str, timeout: int = 30) -> str:
    req = Request(url, headers={"User-Agent": "fort-wayne-allen-scraper/0.1"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_bytes(url: str, timeout: int = 45) -> bytes:
    req = Request(url, headers={"User-Agent": "fort-wayne-allen-scraper/0.1"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_json(url: str, timeout: int = 30) -> object:
    return json.loads(fetch_text(url, timeout=timeout))


def post_json(url: str, payload: object, headers: dict[str, str] | None = None, timeout: int = 30) -> object:
    data = json.dumps(payload).encode("utf-8")
    req_headers = {"User-Agent": "fort-wayne-allen-scraper/0.1", "Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = Request(url, data=data, headers=req_headers, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def post_form(url: str, payload: str, timeout: int = 30) -> object:
    req = Request(
        url,
        data=payload.encode("utf-8"),
        headers={"User-Agent": "fort-wayne-allen-scraper/0.1", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def extract_links(url: str) -> tuple[list[tuple[str, str]], str]:
    html = fetch_text(url)
    parser = LinkParser()
    parser.feed(html)
    return [(text, urljoin(url, href)) for text, href in parser.links if href], html


def slug(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")


def address_key(address: str) -> str:
    key = slug(address)
    replacements = {
        "-street": "-st",
        "-avenue": "-ave",
        "-boulevard": "-blvd",
        "-drive": "-dr",
        "-road": "-rd",
        "-court": "-ct",
        "-lane": "-ln",
        "-trail": "-trl",
        "-way": "-wy",
        "-place": "-pl",
    }
    for old, new in replacements.items():
        key = key.replace(old, new)
    return key


def parse_money(value: str) -> int:
    cleaned = re.sub(r"[^0-9.]", "", value or "")
    if not cleaned:
        return 0
    return int(float(cleaned))


def split_city_state_zip(address: str) -> tuple[str, str, str, str]:
    cleaned = " ".join(address.split())
    match = re.search(r"\b(FORT WAYNE|NEW HAVEN|MONROEVILLE|GRABILL|LEO|HUNTERTOWN|WOODBURN),?\s+IN\s+(\d{5})\b", cleaned, re.I)
    if not match:
        return cleaned.title(), "Fort Wayne", "IN", ""
    street = cleaned[: match.start()].strip(" ,").title()
    city = match.group(1).title()
    return street, city, "IN", match.group(2)


def record_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def make_record(
    *,
    owner_name: str,
    property_address: str,
    property_city: str,
    property_state: str,
    property_zip: str,
    parcel_id: str = "",
    lead_type: str,
    lead_type_key: str,
    filed_date: str,
    amount: int,
    estimated_value: int = 0,
    public_records_url: str,
    distress_sources: list[str],
    tags: list[str],
    notes: str,
    mailing_address: str = "",
    mailing_city: str = "",
    mailing_state: str = "",
    mailing_zip: str = "",
    source_status: str = "live",
) -> dict:
    all_sources = list(dict.fromkeys(distress_sources))
    all_tags = list(dict.fromkeys(tags))
    distress_count = len(all_sources)
    score = min(100, 45 + distress_count * 16 + (10 if amount else 0))
    return {
        "id": record_id("FW", lead_type_key, owner_name, property_address, filed_date),
        "owner_name": owner_name or "Unknown Owner",
        "property_address": property_address,
        "property_city": property_city,
        "property_state": property_state or "IN",
        "property_zip": property_zip,
        "mailing_address": mailing_address,
        "mailing_city": mailing_city,
        "mailing_state": mailing_state,
        "mailing_zip": mailing_zip,
        "parcel_id": parcel_id,
        "lead_type": lead_type,
        "lead_type_key": lead_type_key,
        "filed_date": filed_date,
        "amount": amount,
        "score": score,
        "subject_to_score": max(0, score - 18),
        "distress_count": distress_count,
        "estimated_value": estimated_value,
        "estimated_equity": max(0, estimated_value - amount) if estimated_value else 0,
        "hot_stack": distress_count >= 2 or score >= 80,
        "distress_sources": all_sources,
        "tags": all_tags,
        "public_records_url": public_records_url,
        "notes": notes,
        "source_status": source_status,
    }


def extract_pdf_text(pdf_url: str) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError("pypdf is required to parse sheriff-sale PDFs") from exc

    data = fetch_bytes(pdf_url)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        reader = PdfReader(str(tmp_path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    finally:
        tmp_path.unlink(missing_ok=True)


def sheriff_pdf_urls() -> tuple[list[str], SourceStatus]:
    try:
        links, _ = extract_links(SHERIFF_ARCHIVE_URL)
        pdfs = [href for text, href in links if href.lower().endswith(".pdf") and "2026" in (text + href)]
        return pdfs, SourceStatus("sheriff_sales", SHERIFF_ARCHIVE_URL, "live", f"Found {len(pdfs)} 2026 sheriff-sale PDFs")
    except Exception as exc:
        return [], SourceStatus("sheriff_sales", SHERIFF_ARCHIVE_URL, "error", str(exc))


def parse_sheriff_rows(text: str, pdf_url: str) -> list[dict]:
    records: list[dict] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not re.match(r"\d{1,2}/\d{1,2}/\d{4}\s+02[A-Z0-9-]+", line):
            continue
        if " CANCELLED " in f" {line.upper()} ":
            continue
        match = re.match(r"(?P<sale>\d{1,2}/\d{1,2}/\d{4})\s+(?P<case>02[A-Z0-9-]+)\s+(?P<rest>.+)", line)
        if not match:
            continue
        rest = match.group("rest")
        money_match = re.search(r"\$\s*[\d,]+(?:\.\d{2})?", rest)
        if not money_match:
            continue
        address_text = rest[: money_match.start()].strip()
        street, city, state, zip_code = split_city_state_zip(address_text)
        amount = parse_money(money_match.group(0))
        sale_date = datetime.strptime(match.group("sale"), "%m/%d/%Y").date().isoformat()
        records.append(
            make_record(
                owner_name="Unknown Owner",
                property_address=street,
                property_city=city,
                property_state=state,
                property_zip=zip_code,
                lead_type="Sheriff Sale / Foreclosure",
                lead_type_key="foreclosure",
                filed_date=sale_date,
                amount=amount,
                public_records_url=pdf_url,
                distress_sources=["foreclosure", "sheriff_sale"],
                tags=["foreclosure", "sheriff-sale"],
                notes=f"Live sheriff-sale PDF row. Case {match.group('case')}. Judgment/amount seen: ${amount:,}. Defendant/owner name was not present in the sheriff-sale schedule row; assessor lookup by address is still needed.",
            )
        )
        records[-1]["case_number"] = match.group("case")
        records[-1]["owner_lookup_status"] = "TODO: lookup owner/defendant via Allen County assessor or court case detail by address/case number"
    return records


def owner_index(records: Iterable[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for record in records:
        owner = record.get("owner_name", "")
        addr = record.get("property_address", "")
        if not owner or owner == "Unknown Owner" or not addr:
            continue
        index.setdefault(address_key(addr), record)
    return index


def arcgis_quote(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


def assessor_query(where: str) -> list[dict]:
    query = urlencode(
        {
            "f": "json",
            "where": where,
            "outFields": ",".join(ASSESSOR_FIELDS),
            "returnGeometry": "false",
        },
        safe=",()'",
    )
    payload = fetch_json(f"{ASSESSOR_PARCEL_LAYER_URL}/query?{query}", timeout=20)
    if not isinstance(payload, dict):
        return []
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    features = payload.get("features") or []
    if not isinstance(features, list):
        return []
    return [feature.get("attributes", {}) for feature in features if isinstance(feature, dict)]


def attr_value(attrs: dict, suffix: str) -> str:
    for key, value in attrs.items():
        if key.endswith(suffix) and value is not None:
            return " ".join(str(value).split())
    return ""


def assessor_record(attrs: dict) -> dict:
    mailing_lines = [attr_value(attrs, "MailingAddress1"), attr_value(attrs, "MailingAddress2")]
    property_lines = [attr_value(attrs, "PropertyAddress1"), attr_value(attrs, "PropertyAddress2")]
    return {
        "pin": attr_value(attrs, "PIN"),
        "parcel_id": attr_value(attrs, "GIS_ID") or attr_value(attrs, "PRCLID"),
        "prclid": attr_value(attrs, "PRCLID"),
        "owner_name": attr_value(attrs, "OwnerofRecord"),
        "mailing_address": " ".join(line for line in mailing_lines if line),
        "mailing_city": attr_value(attrs, "MailingCity"),
        "mailing_state": attr_value(attrs, "MailingState"),
        "mailing_zip": attr_value(attrs, "MailingZip"),
        "property_address": " ".join(line for line in property_lines if line),
        "property_city": attr_value(attrs, "PropertyCity"),
        "property_state": attr_value(attrs, "PropertyState"),
        "property_zip": attr_value(attrs, "PropertyZip"),
    }


def unique_assessor_records(rows: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        key = row.get("parcel_id") or row.get("pin") or row.get("prclid") or row.get("property_address")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def address_lookup_variants(value: str) -> list[str]:
    cleaned = " ".join((value or "").upper().split())
    if not cleaned:
        return []
    variants = {cleaned}
    replacements = {
        " ST": " STREET",
        " AVE": " AVENUE",
        " BLVD": " BOULEVARD",
        " DR": " DRIVE",
        " RD": " ROAD",
        " CT": " COURT",
        " LN": " LANE",
        " TRL": " TRAIL",
        " WY": " WAY",
        " PL": " PLACE",
    }
    for short, long in replacements.items():
        if cleaned.endswith(short):
            variants.add(cleaned[: -len(short)] + long)
        if cleaned.endswith(long):
            variants.add(cleaned[: -len(long)] + short)
    return list(variants)


def fetch_assessor_records(records: list[dict], statuses: list[SourceStatus]) -> list[dict]:
    parcel_ids = sorted({record.get("parcel_id", "") for record in records if record.get("parcel_id")})
    rows: list[dict] = []
    errors = 0
    try:
        for offset in range(0, len(parcel_ids), ASSESSOR_BATCH_SIZE):
            batch = parcel_ids[offset : offset + ASSESSOR_BATCH_SIZE]
            where = "GISPublished.SDE.Parcel_Poly.GIS_ID IN (" + ",".join(arcgis_quote(parcel) for parcel in batch) + ")"
            try:
                rows.extend(assessor_record(attrs) for attrs in assessor_query(where))
            except Exception:
                errors += 1
        address_keys_found = {
            address_key(row.get("property_address", "")) for row in rows if row.get("property_address")
        }
        address_records = [
            record
            for record in records
            if record.get("property_address")
            and record.get("property_address") != "Unknown Address"
            and address_key(record.get("property_address", "")) not in address_keys_found
        ]
        for record in address_records:
            candidates = []
            for variant in address_lookup_variants(record.get("property_address", "")):
                where = f"UPPER(GISPublished.SDE.CurrentOwner.PropertyAddress1) = {arcgis_quote(variant)}"
                try:
                    candidates.extend(assessor_record(attrs) for attrs in assessor_query(where))
                except Exception:
                    errors += 1
                    continue
            wanted = address_key(record.get("property_address", ""))
            exact = [candidate for candidate in candidates if address_key(candidate.get("property_address", "")) == wanted]
            if len(exact) == 1:
                rows.extend(exact)
        rows = unique_assessor_records(rows)
        statuses.append(
            SourceStatus(
                "assessor_property",
                ASSESSOR_PARCEL_LAYER_URL,
                "live_partial" if errors else "live",
                f"Parsed {len(rows)} public assessor/property records by exact parcel or address lookup; {errors} lookup batches failed or timed out.",
            )
        )
        print(f"property_assessor_records_parsed={len(rows)} property_assessor_lookup_errors={errors}")
        return rows
    except Exception as exc:
        statuses.append(SourceStatus("assessor_property", ASSESSOR_PARCEL_LAYER_URL, "error", str(exc)))
        print(f"property_assessor_records_parsed=0 property_assessor_error={exc}")
        return []


def apply_assessor_fields(record: dict, match: dict, *, overwrite_owner: bool) -> None:
    if overwrite_owner and match.get("owner_name"):
        record["owner_name"] = match["owner_name"]
    for source_key, record_key in (
        ("parcel_id", "parcel_id"),
        ("property_address", "property_address"),
        ("property_city", "property_city"),
        ("property_state", "property_state"),
        ("property_zip", "property_zip"),
        ("mailing_address", "mailing_address"),
        ("mailing_city", "mailing_city"),
        ("mailing_state", "mailing_state"),
        ("mailing_zip", "mailing_zip"),
    ):
        value = match.get(source_key, "")
        if value:
            record[record_key] = value
    tags = list(record.get("tags") or [])
    tags.append("property-record-matched")
    record["tags"] = list(dict.fromkeys(tags))


def apply_assessor_enrichment(records: list[dict], statuses: list[SourceStatus]) -> dict[str, int]:
    assessor_records = fetch_assessor_records(records, statuses)
    by_parcel = {row.get("parcel_id"): row for row in assessor_records if row.get("parcel_id")}
    by_address: dict[str, list[dict]] = {}
    by_owner: dict[str, list[dict]] = {}
    for row in assessor_records:
        if row.get("property_address"):
            by_address.setdefault(address_key(row["property_address"]), []).append(row)
        if row.get("owner_name"):
            by_owner.setdefault(slug(row["owner_name"]), []).append(row)
    counts = {
        "parcel": 0,
        "address": 0,
        "sheriff": 0,
        "code": 0,
        "probate": 0,
    }
    for record in records:
        match = None
        matched_by = ""
        parcel_id = record.get("parcel_id", "")
        if parcel_id and parcel_id in by_parcel:
            match = by_parcel[parcel_id]
            matched_by = "parcel"
        elif record.get("property_address") and record.get("property_address") != "Unknown Address":
            candidates = by_address.get(address_key(record.get("property_address", "")), [])
            if len(candidates) == 1:
                match = candidates[0]
                matched_by = "address"
        elif record.get("lead_type_key") == "probate" and record.get("owner_name"):
            candidates = by_owner.get(slug(record.get("owner_name", "")), [])
            if len(candidates) == 1:
                match = candidates[0]
                matched_by = "address"
                counts["probate"] += 1
                tags = list(record.get("tags") or [])
                tags = [tag for tag in tags if tag != "probate-name-only"]
                tags.extend(["probate-property-matched", "property-record-matched"])
                record["tags"] = list(dict.fromkeys(tags))
        if not match:
            continue
        counts[matched_by] += 1
        overwrite_owner = record.get("lead_type_key") != "probate" and (record.get("owner_name") in {"", "Unknown Owner"} or matched_by == "parcel")
        apply_assessor_fields(record, match, overwrite_owner=overwrite_owner)
        if record.get("lead_type_key") == "foreclosure":
            counts["sheriff"] += 1
            record["owner_lookup_status"] = "matched_from_public_assessor_property_record"
        if record.get("lead_type_key") == "code_violation":
            counts["code"] += 1
        if record.get("lead_type_key") == "probate":
            # Probate rows only become hot later if another public distress signal, such as absentee, stacks.
            record["hot_stack"] = bool(record.get("hot_stack"))
    print(
        f"records_enriched_by_parcel={counts['parcel']} records_enriched_by_address={counts['address']} "
        f"sheriff_records_owner_enriched={counts['sheriff']} code_records_owner_enriched={counts['code']} "
        f"probate_records_property_matched={counts['probate']}"
    )
    return counts


def absentee_address_key(street: str, city: str = "", state: str = "", zip_code: str = "") -> str:
    parts = [street or "", city or "", state or "", zip_code or ""]
    return address_key(" ".join(part for part in parts if part))


def is_entity_owner(owner: str) -> bool:
    return bool(re.search(r"\b(llc|inc|corp|corporation|co|company|trust|holdings?|partners?|lp|llp|ltd)\b", owner or "", re.I))


def institutional_owner_tags(owner: str) -> list[str]:
    text = f" {owner or ''} ".lower()
    tags: list[str] = []
    institutional_terms = (
        " bank",
        "credit union",
        "mortgage",
        " loan",
        "loan services",
        "servicing",
        "federal national mortgage",
        "fannie mae",
        "freddie mac",
        "secretary of housing",
        "hud",
        "us bank",
        "trust national association",
        "foreclosure trustee",
    )
    if not any(term in text for term in institutional_terms):
        return []
    tags.append("institutional-owner")
    if "secretary of housing" in text or "hud" in text:
        tags.append("hud-owned")
    elif " bank" in text or "credit union" in text or "trust national association" in text or "us bank" in text:
        tags.append("bank-owned")
    elif "mortgage" in text or " loan" in text or "loan services" in text or "servicing" in text or "fannie mae" in text or "freddie mac" in text:
        tags.append("lender-owned")
    return tags


def apply_institutional_owner_tags(records: list[dict]) -> dict[str, int]:
    counts = {"institutional": 0, "bank": 0, "hud": 0, "lender": 0}
    for record in records:
        tags = institutional_owner_tags(record.get("owner_name", ""))
        if not tags:
            continue
        merged = list(dict.fromkeys([*(record.get("tags") or []), *tags]))
        record["tags"] = merged
        counts["institutional"] += 1
        if "bank-owned" in tags:
            counts["bank"] += 1
        if "hud-owned" in tags:
            counts["hud"] += 1
        if "lender-owned" in tags:
            counts["lender"] += 1
    print(
        f"institutional_owner_count={counts['institutional']} bank_owned_count={counts['bank']} "
        f"hud_owned_count={counts['hud']} lender_owned_count={counts['lender']}"
    )
    return counts


def apply_absentee_detection(records: list[dict], statuses: list[SourceStatus]) -> None:
    checked = with_mailing = absentee = out_of_state = missing = 0
    for record in records:
        if not record.get("property_address") or record.get("property_address") == "Unknown Address":
            continue
        checked += 1
        mailing_address = record.get("mailing_address", "")
        if not mailing_address:
            missing += 1
            continue
        with_mailing += 1
        property_key = absentee_address_key(
            record.get("property_address", ""),
            record.get("property_city", ""),
            record.get("property_state", ""),
            record.get("property_zip", ""),
        )
        mailing_key = absentee_address_key(
            mailing_address,
            record.get("mailing_city", ""),
            record.get("mailing_state", ""),
            record.get("mailing_zip", ""),
        )
        if not property_key or not mailing_key or property_key == mailing_key:
            continue
        absentee += 1
        tags = list(record.get("tags") or [])
        sources = list(record.get("distress_sources") or [])
        tags.append("absentee-owner")
        sources.append("absentee")
        mailing_state = (record.get("mailing_state") or "").strip().upper()
        if mailing_state and mailing_state != "IN":
            out_of_state += 1
            tags.append("out-of-state-owner")
            sources.append("out_of_state_owner")
        if is_entity_owner(record.get("owner_name", "")):
            tags.append("entity-owner")
        record["tags"] = list(dict.fromkeys(tags))
        record["distress_sources"] = list(dict.fromkeys(sources))
        record["distress_count"] = len(record["distress_sources"])
        record["score"] = min(100, record.get("score", 0) + (10 if mailing_state and mailing_state != "IN" else 5))
        record["subject_to_score"] = max(0, record["score"] - 18)
        record["hot_stack"] = True
    detail = (
        f"Checked {checked} property-bearing records; {with_mailing} had mailing addresses; "
        f"detected {absentee} absentee owners and {out_of_state} out-of-state owners."
    )
    if checked and not with_mailing:
        detail += " Current public source rows did not expose usable mailing-address fields."
    statuses.append(SourceStatus("absentee_owner", ASSESSOR_PARCEL_LAYER_URL, "stubbed" if not with_mailing else "live", detail))
    print(
        f"absentee_property_records_checked={checked} absentee_with_mailing_address={with_mailing} "
        f"absentee_detected={absentee} out_of_state_owners={out_of_state} "
        f"absentee_missing_mailing_address={missing}"
    )


def name_tokens(value: str) -> list[str]:
    suffixes = {"jr", "sr", "ii", "iii", "iv", "v"}
    cleaned = re.sub(r"\b(estate|decedent|deceased|trust|llc|inc|corp|corporation|co|company)\b", " ", (value or "").lower())
    tokens = re.findall(r"[a-z0-9]+", cleaned)
    return [token for token in tokens if token not in suffixes]


def name_variants(value: str) -> set[str]:
    tokens = name_tokens(value)
    if len(tokens) < 2:
        return set()
    variants = {"-".join(tokens)}
    first, last = tokens[0], tokens[-1]
    middle = tokens[1:-1]
    variants.add("-".join([first, last]))
    variants.add("-".join([last, first]))
    if middle:
        variants.add("-".join([first, middle[0], last]))
        variants.add("-".join([last, first, middle[0]]))
        if len(middle[0]) > 1:
            variants.add("-".join([first, middle[0][0], last]))
            variants.add("-".join([last, first, middle[0][0]]))
    return {variant for variant in variants if variant}


def probate_owner_search_terms(name: str) -> list[str]:
    tokens = name_tokens(name)
    if len(tokens) < 2:
        return []
    first, last = tokens[0], tokens[-1]
    middle = tokens[1:-1]
    terms = [
        last,
        f"{last} {first}",
        f"{last} {first[0]}",
        " ".join(tokens),
        " ".join([last, first, *middle]),
    ]
    return list(dict.fromkeys(term.upper() for term in terms if term.strip()))


def probate_safe_owner_match(probate_name: str, assessor_owner: str) -> bool:
    probate_tokens = name_tokens(probate_name)
    owner_tokens = set(name_tokens(assessor_owner))
    if len(probate_tokens) < 2 or not owner_tokens:
        return False
    first, last = probate_tokens[0], probate_tokens[-1]
    if first not in owner_tokens or last not in owner_tokens:
        return False
    if len(probate_tokens) > 2 and probate_tokens[1] in owner_tokens:
        return True
    return True


def assessor_owner_search(term: str) -> list[dict]:
    where = f"UPPER(GISPublished.SDE.CurrentOwner.OwnerofRecord) LIKE {arcgis_quote(term + '%')}"
    return [assessor_record(attrs) for attrs in assessor_query(where)]


def probate_candidate_summary(candidate: dict) -> str:
    address = ", ".join(
        part
        for part in [
            candidate.get("property_address", ""),
            " ".join(
                part
                for part in [
                    candidate.get("property_city", ""),
                    candidate.get("property_state", ""),
                    candidate.get("property_zip", ""),
                ]
                if part
            ),
        ]
        if part
    ).strip()
    details = []
    if candidate.get("owner_name"):
        details.append(str(candidate.get("owner_name")))
    if candidate.get("parcel_id"):
        details.append(f"Parcel {candidate.get('parcel_id')}")
    if candidate.get("mailing_address"):
        mailing = ", ".join(
            part
            for part in [
                candidate.get("mailing_address", ""),
                " ".join(
                    part
                    for part in [
                        candidate.get("mailing_city", ""),
                        candidate.get("mailing_state", ""),
                        candidate.get("mailing_zip", ""),
                    ]
                    if part
                ),
            ]
            if part
        ).strip()
        if mailing and mailing != address:
            details.append(f"Mailing {mailing}")
    if details:
        return f"{address} ({'; '.join(details)})" if address else "; ".join(details)
    return address


def set_probate_candidate_review(record: dict, candidates: list[dict], status: str) -> bool:
    summaries = [summary for summary in (probate_candidate_summary(candidate) for candidate in candidates[:5]) if summary]
    record["probate_candidate_count"] = len(candidates)
    record["probate_property_candidate_count"] = len(candidates)
    record["probate_match_status"] = status
    if summaries:
        record["probate_candidate_addresses"] = "; ".join(summaries)
        return True
    return False


def apply_probate_property_owner_search(records: list[dict], statuses: list[SourceStatus]) -> None:
    probate_rows = [record for record in records if record.get("lead_type_key") == "probate"]
    searches = candidates_found = matched = multiple = name_only = candidate_addresses_stored = 0
    try:
        for record in probate_rows:
            if record.get("property_address") and record.get("property_address") != "Unknown Address":
                record["probate_match_status"] = record.get("probate_match_status") or "confirmed-existing-property-match"
                continue
            candidates: list[dict] = []
            for term in probate_owner_search_terms(record.get("owner_name", "")):
                searches += 1
                try:
                    candidates.extend(assessor_owner_search(term))
                except Exception:
                    continue
            candidates = unique_assessor_records(
                candidate
                for candidate in candidates
                if probate_safe_owner_match(record.get("owner_name", ""), candidate.get("owner_name", ""))
            )
            candidates_found += len(candidates)
            if len(candidates) == 1:
                apply_assessor_fields(record, candidates[0], overwrite_owner=False)
                tags = [tag for tag in (record.get("tags") or []) if tag != "probate-name-only"]
                tags.append("probate-property-matched")
                record["tags"] = list(dict.fromkeys(tags))
                if set_probate_candidate_review(record, candidates, "confirmed-single-assessor-owner-match"):
                    candidate_addresses_stored += 1
                record["notes"] = (
                    f"{record.get('notes', '')} Public assessor owner search returned one safe property match: "
                    f"{record.get('probate_candidate_addresses', '')}."
                ).strip()
                record["score"] = max(record.get("score", 0), 72)
                record["subject_to_score"] = max(0, record["score"] - 18)
                matched += 1
            elif len(candidates) > 1:
                tags = [tag for tag in (record.get("tags") or []) if tag != "probate-name-only"]
                tags.append("needs-property-review")
                record["tags"] = list(dict.fromkeys(tags))
                if set_probate_candidate_review(record, candidates, "needs-property-review"):
                    candidate_addresses_stored += 1
                    record["notes"] = (
                        f"{record.get('notes', '')} Public assessor owner search returned {len(candidates)} possible "
                        f"property candidates; review before using: {record.get('probate_candidate_addresses', '')}."
                    ).strip()
                multiple += 1
            else:
                record["probate_candidate_count"] = 0
                record["probate_property_candidate_count"] = 0
                record["probate_match_status"] = "probate-name-only"
                name_only += 1
        statuses.append(
            SourceStatus(
                "probate_property_owner_search",
                ASSESSOR_PARCEL_LAYER_URL,
                "live",
                f"Checked {len(probate_rows)} probate rows; attempted {searches} full assessor owner searches; found {candidates_found} safe owner candidates.",
            )
        )
    except Exception as exc:
        statuses.append(SourceStatus("probate_property_owner_search", ASSESSOR_PARCEL_LAYER_URL, "error", str(exc)))
    print(
        f"probate_property_rows_checked={len(probate_rows)} property_owner_searches_attempted={searches} "
        f"property_candidates_found={candidates_found} exact_safe_property_matches={matched} "
        f"property_multiple_candidate_review_rows={multiple} probate_candidate_addresses_stored={candidate_addresses_stored} "
        f"property_name_only_rows={name_only}"
    )


def recorder_name_queries(name: str) -> list[tuple[str, str]]:
    tokens = name_tokens(name)
    if len(tokens) < 2:
        return []
    first = tokens[0].title()
    last = tokens[-1].title()
    middle = " ".join(token.title() for token in tokens[1:-1])
    queries = [(first, last)]
    if middle:
        queries.append((f"{first} {middle}", last))
        queries.append(("", f"{last} {first} {middle}"))
    queries.append(("", f"{last} {first}"))
    return list(dict.fromkeys(queries))


def recorder_exact_name_match(name: str, value: str) -> bool:
    wanted = name_variants(name)
    found = name_variants(value)
    return bool(wanted and found and wanted.intersection(found))


def recorder_login_token() -> str:
    payload = post_form(
        f"{RECORDER_API_BASE}token",
        "grant_type=password&username=anonymous&password=anonymous",
        timeout=20,
    )
    return str(payload.get("access_token") or "") if isinstance(payload, dict) else ""


def recorder_search(token: str, first_name: str, last_business_name: str) -> list[dict]:
    criteria = {
        "FirstName": first_name,
        "LastBusinessName": last_business_name,
        "StartDate": "",
        "EndDate": "",
        "DocumentName": "",
        "DocumentType": "",
        "SubdivisionName": "",
        "SubdivisionLot": "",
        "SubdivisionBlock": "",
        "MunicipalityName": "",
        "TractSection": "",
        "TractTownship": "",
        "TractRange": "",
        "TractQuarter": "",
        "TractQuarterQuarter": "",
        "AddressHouseNo": "",
        "AddressStreet": "",
        "AddressCity": "",
        "AddressZip": "",
        "ParcelNumber": "",
        "Book": "",
        "Page": "",
        "ReferenceNumber": "",
    }
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = post_json(f"{RECORDER_API_BASE}breeze/Search", criteria, headers=headers, timeout=20)
    if not isinstance(payload, dict):
        return []
    return [row for row in payload.get("DocResults", []) if isinstance(row, dict)]


def row_value(row: dict, *names: str) -> str:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return " ".join(str(value).split())
    return ""


def recorder_document(row: dict, probate_name: str) -> dict | None:
    doc_type = row_value(row, "DocumentType", "DocumentTypeName", "Type")
    doc_name = row_value(row, "DocumentName", "Name")
    grantor = row_value(row, "Grantor", "Grantors", "From")
    grantee = row_value(row, "Grantee", "Grantees", "To")
    haystack = " ".join([doc_type, doc_name, grantor, grantee]).lower()
    if not any(term in haystack for term in RECORDER_DEED_TERMS):
        return None
    if not (recorder_exact_name_match(probate_name, grantor) or recorder_exact_name_match(probate_name, doc_name)):
        return None
    return {
        "grantor": grantor or doc_name,
        "grantee": grantee,
        "recorder_document_type": doc_type,
        "recorder_document_number": row_value(row, "DocumentNumber", "DocumentNo", "DocNumber", "InstrumentNumber"),
        "recorder_recorded_date": row_value(row, "RecordedDateTime", "RecordedDate", "RecordingDate"),
        "parcel_id": row_value(row, "ParcelNumber", "Parcel", "PIN", "APN"),
        "property_address": row_value(row, "PropertyAddress", "Address"),
        "source_url": RECORDER_SEARCH_URL,
    }


def assessor_match_for_recorder_doc(doc: dict) -> tuple[dict | None, int]:
    candidates: list[dict] = []
    parcel = doc.get("parcel_id", "")
    if parcel:
        where = (
            "GISPublished.SDE.Parcel_Poly.GIS_ID = {p} OR "
            "GISPublished.SDE.Parcel_Poly.PIN = {p} OR "
            "GISPublished.SDE.Parcel_Poly.PRCLID = {p}"
        ).format(p=arcgis_quote(parcel))
        candidates.extend(assessor_record(attrs) for attrs in assessor_query(where))
    if not candidates and doc.get("property_address"):
        for variant in address_lookup_variants(doc["property_address"]):
            where = f"UPPER(GISPublished.SDE.CurrentOwner.PropertyAddress1) = {arcgis_quote(variant)}"
            candidates.extend(assessor_record(attrs) for attrs in assessor_query(where))
    candidates = unique_assessor_records(candidates)
    if len(candidates) == 1:
        return candidates[0], 1
    return None, len(candidates)


def apply_recorder_probate_enrichment(records: list[dict], statuses: list[SourceStatus]) -> None:
    probate_rows = [record for record in records if record.get("lead_type_key") == "probate"]
    searches = matches = property_matched = multiple = name_only = grantor_grantee = 0
    try:
        token = recorder_login_token()
        if not token:
            raise RuntimeError("Recorder DirectSearch anonymous token was not returned")
        for record in probate_rows:
            row_matches: list[dict] = []
            for first, last in recorder_name_queries(record.get("owner_name", "")):
                searches += 1
                try:
                    row_matches.extend(recorder_search(token, first, last))
                except Exception:
                    continue
            docs = [doc for row in row_matches if (doc := recorder_document(row, record.get("owner_name", "")))]
            if not docs:
                if not record.get("property_address") or record.get("property_address") == "Unknown Address":
                    name_only += 1
                continue
            matches += len(docs)
            doc = docs[0]
            for key in ("grantor", "grantee", "recorder_document_type", "recorder_document_number", "recorder_recorded_date"):
                if doc.get(key):
                    record[key] = doc[key]
            record["recorder_source_url"] = doc.get("source_url", RECORDER_SEARCH_URL)
            if record.get("grantor") or record.get("grantee"):
                grantor_grantee += 1
            assessor_match, candidate_count = assessor_match_for_recorder_doc(doc)
            if assessor_match:
                apply_assessor_fields(record, assessor_match, overwrite_owner=False)
                tags = [tag for tag in (record.get("tags") or []) if tag != "probate-name-only"]
                tags.extend(["probate-property-matched", "recorder-matched"])
                record["tags"] = list(dict.fromkeys(tags))
                record["hot_stack"] = True
                property_matched += 1
            elif len(docs) > 1 or candidate_count > 1:
                multiple += 1
                record["tags"] = list(dict.fromkeys([*(record.get("tags") or []), "needs-property-review"]))
            else:
                record["tags"] = list(dict.fromkeys([*(record.get("tags") or []), "recorder-matched"]))
                name_only += 1
        statuses.append(
            SourceStatus(
                "recorder_probate",
                RECORDER_SEARCH_URL,
                "live_partial",
                f"Checked {len(probate_rows)} probate rows; attempted {searches} Recorder searches; found {matches} name-tied deed/transfer candidates.",
            )
        )
    except Exception as exc:
        name_only = len(probate_rows)
        statuses.append(SourceStatus("recorder_probate", RECORDER_SEARCH_URL, "blocked", str(exc)))
    print(
        f"probate_rows_checked={len(probate_rows)} recorder_searches_attempted={searches} "
        f"recorder_matches_found={matches} probate_recorder_property_matched={property_matched} "
        f"probate_recorder_multiple_possible_matches={multiple} probate_recorder_name_only={name_only} "
        f"grantor_grantee_fields_populated={grantor_grantee}"
    )


def owner_name_index(records: Iterable[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for record in records:
        owner = record.get("owner_name", "")
        if not owner or owner == "Unknown Owner":
            continue
        for key in name_variants(owner):
            index.setdefault(key, []).append(record)
    return index


def match_owner_name(name: str, index: dict[str, list[dict]]) -> tuple[dict | None, int]:
    candidates: dict[str, dict] = {}
    for key in name_variants(name):
        for record in index.get(key, []):
            candidate_key = record.get("parcel_id") or record.get("property_address") or record.get("id", "")
            candidates[candidate_key] = record
    if len(candidates) == 1:
        return next(iter(candidates.values())), 1
    return None, len(candidates)


def enrich_sheriff_owners(records: list[dict], property_index: dict[str, dict]) -> int:
    matched = 0
    for record in records:
        if record.get("owner_name") != "Unknown Owner":
            continue
        match = property_index.get(address_key(record.get("property_address", "")))
        if not match:
            record["owner_lookup_status"] = "TODO: lookup owner/defendant via Allen County assessor or court case detail by address/case number"
            continue
        record["owner_name"] = match["owner_name"]
        record["parcel_id"] = match.get("parcel_id", record.get("parcel_id", ""))
        record["owner_lookup_status"] = "matched_from_tax_delinquent_address"
        record["tags"] = list(dict.fromkeys([*(record.get("tags") or []), "owner-matched", "tax-delinquent"]))
        record["distress_sources"] = list(dict.fromkeys([*(record.get("distress_sources") or []), "tax_delinquent"]))
        record["distress_count"] = len(record["distress_sources"])
        record["hot_stack"] = True
        record["notes"] = f"{record.get('notes', '')} Owner matched from Allen County delinquent property row by normalized address.".strip()
        matched += 1
    return matched


def sheriff_sale_records(statuses: list[SourceStatus], property_index: dict[str, dict] | None = None) -> list[dict]:
    pdfs, status = sheriff_pdf_urls()
    statuses.append(status)
    records: list[dict] = []
    for pdf_url in pdfs[:6]:
        try:
            text = extract_pdf_text(pdf_url)
            records.extend(parse_sheriff_rows(text, pdf_url))
        except Exception as exc:
            statuses.append(SourceStatus("sheriff_pdf", pdf_url, "stubbed", str(exc)))
    matched = enrich_sheriff_owners(records, property_index or {})
    named = sum(1 for record in records if record.get("owner_name") != "Unknown Owner")
    unknown = len(records) - named
    print(f"sheriff_records={len(records)} sheriff_owner_address_matches={matched} sheriff_named_owners={named} sheriff_unknown_owners={unknown}")
    if unknown:
        statuses.append(
            SourceStatus(
                "sheriff_owner_lookup",
                ASSESSOR_URL,
                "stubbed",
                f"{matched} sheriff records matched owner by tax/property address; {unknown} still need assessor or case-detail lookup.",
            )
        )
    return records


def xlsx_rows(url: str) -> list[dict[str, str]]:
    data = fetch_bytes(url)
    with ZipFile(BytesIO(data)) as archive:
        ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = [
            "".join(t.text or "" for t in si.findall(".//a:t", ns))
            for si in shared_root.findall("a:si", ns)
        ]
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        parsed_rows: list[list[str]] = []
        for row in sheet.findall(".//a:row", ns):
            values: dict[int, str] = {}
            for cell in row.findall("a:c", ns):
                ref = cell.attrib.get("r", "")
                col_match = re.match(r"([A-Z]+)", ref)
                if not col_match:
                    continue
                col = 0
                for ch in col_match.group(1):
                    col = col * 26 + ord(ch) - ord("A") + 1
                value_node = cell.find("a:v", ns)
                if value_node is None or value_node.text is None:
                    value = ""
                elif cell.attrib.get("t") == "s":
                    value = shared[int(value_node.text)]
                else:
                    value = value_node.text
                values[col - 1] = " ".join(value.split())
            if values:
                parsed_rows.append([values.get(i, "") for i in range(max(values) + 1)])
        if not parsed_rows:
            return []
        headers = [slug(header).replace("-", "_") for header in parsed_rows[0]]
        return [dict(zip(headers, row)) for row in parsed_rows[1:] if any(row)]


def parse_tax_address(value: str) -> tuple[str, str, str, str]:
    cleaned = " ".join((value or "").split())
    return split_city_state_zip(cleaned)


def tax_delinquent_records(statuses: list[SourceStatus]) -> list[dict]:
    try:
        links, html = extract_links(DELINQUENT_URL)
        delinquent_links = [href for text, href in links if "delinquent" in (text + href).lower()]
        rows = xlsx_rows(DELINQUENT_DOCUMENT_URL)
        candidates = []
        for row in rows:
            parcel = row.get("parcel_property_number", "")
            tax_type = row.get("tax_type", "")
            amount = parse_money(row.get("delinquent_amt", ""))
            address = row.get("property_address", "")
            owner = row.get("pay_yr_owner_of_record", "")
            if tax_type.lower() != "real" or not amount or not address:
                continue
            street, city, state, zip_code = parse_tax_address(address)
            candidates.append(
                make_record(
                    owner_name=owner or "Unknown Owner",
                    property_address=street,
                    property_city=city,
                    property_state=state,
                    property_zip=zip_code,
                    parcel_id=parcel,
                    lead_type="Tax Delinquent",
                    lead_type_key="tax_delinquent",
                    filed_date=datetime.now(UTC).date().isoformat(),
                    amount=amount,
                    public_records_url=DELINQUENT_DOCUMENT_URL,
                    distress_sources=["tax_delinquent"],
                    tags=["tax-delinquent"] + (["high-tax-delinquency"] if amount >= 2500 else []),
                    notes=f"Live Allen County delinquent property row. Tax type: {tax_type}. Delinquent amount: ${amount:,}.",
                )
            )
        candidates.sort(key=lambda record: record.get("amount", 0), reverse=True)
        records = candidates[:TAX_RECORD_LIMIT]
        statuses.append(
            SourceStatus(
                "tax_delinquent",
                DELINQUENT_DOCUMENT_URL,
                "live",
                f"Parsed {len(candidates)} real-property delinquent rows from {len(rows)} spreadsheet rows; exported top {len(records)} by delinquent amount. Page had {len(delinquent_links)} delinquent links.",
            )
        )
        print(f"tax_delinquent_records={len(records)} tax_delinquent_candidates={len(candidates)}")
        return records
    except Exception as exc:
        statuses.append(SourceStatus("tax_delinquent", DELINQUENT_URL, "error", str(exc)))
        print(f"tax_delinquent_records=0 tax_error={exc}")
        return [
        make_record(
            owner_name="Tax Delinquent Adapter Pending",
            property_address="Allen County Delinquent Property List",
            property_city="Fort Wayne",
            property_state="IN",
            property_zip="",
            lead_type="Tax Delinquent Source Stub",
            lead_type_key="tax_delinquent",
            filed_date=datetime.now(UTC).date().isoformat(),
            amount=0,
            public_records_url=DELINQUENT_URL,
            distress_sources=["tax_delinquent"],
            tags=["tax-delinquent", "adapter-stub"],
            notes="Live Allen County delinquent property page is reachable; row-level parser is intentionally stubbed until the linked document format is locked down.",
            source_status="stub",
        )
        ]


def code_distress_tags(row: dict, repeated: bool, stacked: bool) -> tuple[list[str], int] | None:
    text = " ".join(str(row.get(key) or "") for key in ("department", "casetype", "casesubtype", "incidentstatus"))
    lowered = text.lower()
    tags: list[str] = ["code-violation"]
    score = 70
    if "minimum housing" in lowered or "commercial standards" in lowered:
        tags.extend(["housing-building-code", "orders-to-repair"])
        score = 84
    if "open structure" in lowered:
        tags.extend(["open-structure", "unsecured-property"])
        score = 88
    if "debris" in lowered or "cistern" in lowered:
        tags.extend(["trash-debris", "nuisance"])
        score = max(score, 74)
    if "abandoned vehicle" in lowered:
        if not (repeated or stacked):
            return None
        tags.extend(["abandoned-vehicle", "property-neglect"])
        score = max(score, 72)
    if "grass" in lowered or "weed" in lowered or "overgrowth" in lowered:
        if not (repeated or stacked):
            return None
        tags.extend(["overgrown", "nuisance"])
        score = max(score, 70)
    if "bulk trash" in lowered or lowered.strip() in {"solid waste garbage", "garbage"}:
        if not (repeated or stacked):
            return None
        tags.extend(["trash-debris"])
        score = max(score, 70)
    if not any(tag != "code-violation" for tag in tags):
        return None
    if stacked:
        tags.append("hot-stack")
        score = min(88, score + 8)
    return list(dict.fromkeys(tags)), score


def is_property_specific_address(address: str) -> bool:
    cleaned = " ".join((address or "").split()).strip()
    if not cleaned or " & " in cleaned:
        return False
    lowered = cleaned.lower()
    public_space_terms = (
        " park",
        "trail",
        "greenway",
        "riverfront",
        "right of way",
        "right-of-way",
        "sidewalk",
        "alley",
    )
    if any(term in lowered for term in public_space_terms):
        return False
    return bool(re.match(r"^\d+\s+[A-Za-z0-9]", cleaned))


def parse_incident_date(value: str) -> str:
    if not value:
        return datetime.now(UTC).date().isoformat()
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").date().isoformat()
    except ValueError:
        return datetime.now(UTC).date().isoformat()


def code_violation_records(
    statuses: list[SourceStatus],
    tax_index: dict[str, dict],
    foreclosure_index: dict[str, dict],
) -> list[dict]:
    raw_count = ignored = non_property = with_address = owner_matched = still_unknown = tax_stacked = foreclosure_stacked = 0
    try:
        rows = fetch_json(FORT_WAYNE_311_INCIDENTS_URL)
        if not isinstance(rows, list):
            raise RuntimeError("Fort Wayne 311 endpoint did not return a list")
        raw_count = len(rows)
        address_counts: dict[str, int] = {}
        for row in rows:
            addr = str(row.get("addressline1") or "").strip()
            if addr:
                address_counts[address_key(addr)] = address_counts.get(address_key(addr), 0) + 1
        records = []
        for row in rows:
            addr = str(row.get("addressline1") or "").strip()
            if addr:
                with_address += 1
            key = address_key(addr)
            if not is_property_specific_address(addr):
                non_property += 1
                continue
            stacked_tax = key in tax_index
            stacked_foreclosure = key in foreclosure_index
            stacked = stacked_tax or stacked_foreclosure
            classified = code_distress_tags(row, address_counts.get(key, 0) > 1, stacked)
            if not classified:
                ignored += 1
                continue
            tags, score = classified
            match = tax_index.get(key)
            owner = match.get("owner_name", "Unknown Owner") if match else "Unknown Owner"
            if match:
                owner_matched += 1
            else:
                still_unknown += 1
            if stacked_tax:
                tax_stacked += 1
                tags.append("tax-delinquent")
            if stacked_foreclosure:
                foreclosure_stacked += 1
                tags.append("foreclosure")
            street, city, state, zip_code = split_city_state_zip(addr)
            case_type = str(row.get("casetype") or "Code / Nuisance").strip()
            status = str(row.get("incidentstatus") or "").strip()
            refno = str(row.get("refno") or row.get("id") or "").strip()
            filed_date = parse_incident_date(str(row.get("createdtime") or ""))
            notes = f"Live Fort Wayne 311/code row. Case {refno}. Type: {case_type}. Status: {status}."
            if stacked_tax:
                notes += " Address also appears in Allen County tax delinquent data."
            if stacked_foreclosure:
                notes += " Address also appears in sheriff-sale/foreclosure data."
            distress_sources = ["code_violation"] + [tag.replace("-", "_") for tag in tags if tag not in {"code-violation", "hot-stack"}]
            record = make_record(
                owner_name=owner,
                property_address=street,
                property_city=city,
                property_state=state,
                property_zip=zip_code,
                parcel_id=match.get("parcel_id", "") if match else "",
                lead_type="Code / Nuisance",
                lead_type_key="code_violation",
                filed_date=filed_date,
                amount=0,
                public_records_url=FORT_WAYNE_311_URL,
                distress_sources=list(dict.fromkeys(distress_sources)),
                tags=list(dict.fromkeys(tags)),
                notes=notes,
            )
            record["score"] = score
            record["subject_to_score"] = max(0, score - 18)
            record["hot_stack"] = stacked or score >= 80
            record["case_number"] = refno
            record["source_status"] = "live"
            records.append(record)
        statuses.append(
            SourceStatus(
                "code_violation",
                FORT_WAYNE_311_INCIDENTS_URL,
                "live",
                f"Parsed {raw_count} public 311 rows; excluded {non_property} non-property/intersection rows; kept {len(records)} property-distress code rows after noise filtering.",
            )
        )
        print(
            "code_raw_records={raw} code_non_property_excluded={non_property} "
            "code_ignored_low_distress={ignored} code_distress_kept={kept} "
            "code_records_with_addresses={with_addr} code_owner_matched={matched} "
            "code_stacked_with_tax={tax_stacked} code_stacked_with_foreclosure={foreclosure_stacked} "
            "code_unknown_owner={unknown}".format(
                raw=raw_count,
                non_property=non_property,
                ignored=ignored,
                kept=len(records),
                with_addr=with_address,
                matched=owner_matched,
                tax_stacked=tax_stacked,
                foreclosure_stacked=foreclosure_stacked,
                unknown=still_unknown,
            )
        )
        return records or code_violation_stub(statuses, "No public 311 rows passed distress filtering.")
    except Exception as exc:
        statuses.append(SourceStatus("code_violation", FORT_WAYNE_311_INCIDENTS_URL, "error", str(exc)))
        print(
            f"code_raw_records={raw_count} code_non_property_excluded={non_property} "
            f"code_ignored_low_distress={ignored} code_distress_kept=0 "
            f"code_records_with_addresses={with_address} code_owner_matched={owner_matched} "
            f"code_stacked_with_tax={tax_stacked} code_stacked_with_foreclosure={foreclosure_stacked} "
            f"code_unknown_owner={still_unknown} code_error={exc}"
        )
        return code_violation_stub(statuses, "Public Fort Wayne 311/code endpoint could not be parsed safely.")


def code_violation_stub(statuses: list[SourceStatus], detail: str) -> list[dict]:
    statuses.append(SourceStatus("code_violation", FORT_WAYNE_311_URL, "stubbed", detail))
    return [
        make_record(
            owner_name="Code Nuisance Adapter Pending",
            property_address="Fort Wayne Code / Nuisance Records",
            property_city="Fort Wayne",
            property_state="IN",
            property_zip="",
            lead_type="Code / Nuisance Source Stub",
            lead_type_key="code_violation",
            filed_date=datetime.now(UTC).date().isoformat(),
            amount=0,
            public_records_url=FORT_WAYNE_311_URL,
            distress_sources=["code_violation"],
            tags=["code-violation", "nuisance", "adapter-stub"],
            notes="Code/nuisance adapter placeholder. Dashboard-compatible structure remains available when the public live source returns no safe distress rows.",
            source_status="stub",
        )
    ]


def court_calendar_date_range() -> list[str]:
    payload = fetch_json(f"{COURT_CALENDAR_API}/Hearing/DateRange")
    data = payload.get("payload", {}) if isinstance(payload, dict) else {}
    start = datetime.strptime(data.get("startDate", ""), "%m/%d/%Y").date()
    end = datetime.strptime(data.get("endDate", ""), "%m/%d/%Y").date()
    out = []
    day = start
    while day <= end:
        out.append(day.strftime("%m/%d/%Y"))
        day += timedelta(days=1)
    return out


def probate_subject(case_style: str) -> str:
    style = " ".join((case_style or "").replace("\n", " ").split())
    patterns = [
        r"in re:?\s+the estate of\s+(.+)",
        r"in re:?\s+estate of\s+(.+)",
        r"estate of\s+(.+)",
        r"in re:?\s+the guardianship of\s+(.+)",
        r"in re:?\s+the trust of\s+(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, style, re.I)
        if match:
            return match.group(1).strip(" .")
    return style.strip()


def probate_tags(row: dict, matched_property: bool) -> list[str] | None:
    case_number = str(row.get("caseNumber") or "")
    style = str(row.get("caseStyle") or "")
    text = f"{case_number} {style} {row.get('hearingDesc') or ''}".lower()
    tags = ["probate"]
    if re.search(r"-(eu|es)-", case_number, re.I) or "estate" in text:
        tags.extend(["estate", "decedent"])
    elif re.search(r"-gu-", case_number, re.I) or "guardianship" in text:
        if not matched_property:
            return None
        tags.append("guardianship")
    elif "trust" in text and matched_property:
        tags.append("trust")
    else:
        return None
    return list(dict.fromkeys(tags))


def probate_records(statuses: list[SourceStatus], tax_name_index: dict[str, list[dict]]) -> list[dict]:
    raw = kept = matched = multiple = name_only = hot_stacked = 0
    try:
        records = []
        for date_text in court_calendar_date_range():
            url = f"{COURT_CALENDAR_API}/Hearing/List?countyID={ALLEN_COUNTY_ID}&date={date_text}&skip=0&take=500"
            payload = fetch_json(url)
            rows = payload.get("payload", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                continue
            raw += len(rows)
            for row in rows:
                if str(row.get("caseCategoryKey") or "") != "PR":
                    continue
                subject = probate_subject(str(row.get("caseStyle") or ""))
                property_match, candidate_count = match_owner_name(subject, tax_name_index)
                tags = probate_tags(row, bool(property_match))
                if not tags or not subject:
                    continue
                kept += 1
                if property_match:
                    matched += 1
                    hot_stacked += 1
                    tags.extend(["probate-property-matched", "hot-stack", "tax-delinquent"])
                elif candidate_count > 1:
                    multiple += 1
                    tags.append("needs-property-review")
                else:
                    name_only += 1
                    tags.append("probate-name-only")
                hearing_date = parse_incident_date(str(row.get("sessionDate") or ""))
                case_number = str(row.get("caseNumber") or "").strip()
                hearing = str(row.get("hearingDesc") or "").strip()
                court = str(row.get("courtName") or "Allen County Court").strip()
                notes = f"Live Indiana court calendar probate row. Case {case_number}. Hearing: {hearing}. Court: {court}. Hearing date: {hearing_date}."
                if property_match:
                    notes += " Matched to Allen County tax/property owner name."
                elif candidate_count > 1:
                    notes += f" Multiple possible tax/property owner matches found ({candidate_count}); property address needs manual review."
                else:
                    notes += " No safe tax/property owner match found; property address left unknown."
                record = make_record(
                    owner_name=subject,
                    property_address=property_match.get("property_address", "Unknown Address") if property_match else "Unknown Address",
                    property_city=property_match.get("property_city", "Fort Wayne") if property_match else "Fort Wayne",
                    property_state=property_match.get("property_state", "IN") if property_match else "IN",
                    property_zip=property_match.get("property_zip", "") if property_match else "",
                    parcel_id=property_match.get("parcel_id", "") if property_match else "",
                    lead_type="Probate / Estate",
                    lead_type_key="probate",
                    filed_date=hearing_date,
                    amount=property_match.get("amount", 0) if property_match else 0,
                    public_records_url=str(row.get("caseURL") or "https://public.courts.in.gov/CourtCal/"),
                    distress_sources=["probate"] + (["tax_delinquent"] if property_match else []),
                    tags=list(dict.fromkeys(tags)),
                    notes=notes,
                )
                record["case_number"] = case_number
                record["source_status"] = "live"
                record["hot_stack"] = bool(property_match)
                if property_match:
                    record["score"] = min(100, record.get("score", 0) + 10)
                    record["subject_to_score"] = max(0, record["score"] - 18)
                else:
                    record["score"] = min(record.get("score", 0), 62)
                    record["subject_to_score"] = max(0, record["score"] - 18)
                records.append(record)
        statuses.append(
            SourceStatus(
                "probate",
                f"{COURT_CALENDAR_API}/Hearing/List",
                "live",
                f"Scanned {raw} Allen County public hearing rows; kept {kept} probate/estate rows; {matched} safely matched to tax/property owner records.",
            )
        )
        print(
            f"probate_raw_records={raw} probate_records_kept={kept} "
            f"probate_property_matched={matched} probate_multiple_possible_matches={multiple} "
            f"probate_name_only={name_only} probate_hot_stacked={hot_stacked}"
        )
        return records or probate_stub(statuses, "No probate/estate rows were found in the public court calendar range.")
    except Exception as exc:
        statuses.append(SourceStatus("probate", f"{COURT_CALENDAR_API}/Hearing/List", "error", str(exc)))
        print(
            f"probate_raw_records={raw} probate_records_kept={kept} "
            f"probate_property_matched={matched} probate_multiple_possible_matches={multiple} "
            f"probate_name_only={name_only} probate_hot_stacked={hot_stacked} probate_error={exc}"
        )
        return probate_stub(statuses, "Public Indiana court calendar probate rows could not be parsed safely.")


def probate_stub(statuses: list[SourceStatus], detail: str) -> list[dict]:
    statuses.append(SourceStatus("probate", "Allen County court/probate records", "stubbed", detail))
    return [
        make_record(
            owner_name="Probate Adapter Pending",
            property_address="Allen County Probate Records",
            property_city="Fort Wayne",
            property_state="IN",
            property_zip="",
            lead_type="Probate / Estate Source Stub",
            lead_type_key="probate",
            filed_date=datetime.now(UTC).date().isoformat(),
            amount=0,
            public_records_url="https://public.courts.in.gov/CourtCal/",
            distress_sources=["probate"],
            tags=["probate", "adapter-stub"],
            notes="Probate/estate adapter placeholder. Public court calendar parsing returned no safe estate rows.",
            source_status="stub",
        )
    ]


def placeholder_records(statuses: list[SourceStatus]) -> list[dict]:
    statuses.extend(
        [
            SourceStatus("assessor", ASSESSOR_URL, "live", "Public assessor/property enrichment uses Allen County iMap parcel layer for exact parcel/address matches."),
        ]
    )
    return []


def dedupe(records: Iterable[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for record in records:
        property_key = record.get("parcel_id") or record.get("property_address", "")
        if record.get("lead_type_key") == "probate" and property_key == "Unknown Address":
            property_key = record.get("case_number") or property_key
        key = (
            slug(record.get("lead_type_key", "")),
            slug(property_key),
            slug(record.get("filed_date", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


def print_probate_review_summary(records: list[dict]) -> None:
    probate_rows = [record for record in records if record.get("lead_type_key") == "probate"]
    confirmed = [
        record
        for record in probate_rows
        if "probate-property-matched" in (record.get("tags") or [])
        and record.get("property_address")
        and record.get("property_address") != "Unknown Address"
    ]
    needs_review = [record for record in probate_rows if "needs-property-review" in (record.get("tags") or [])]
    candidate_addresses = [record for record in probate_rows if record.get("probate_candidate_addresses")]
    name_only = [
        record
        for record in probate_rows
        if "probate-name-only" in (record.get("tags") or []) and not record.get("probate_candidate_addresses")
    ]
    grantor = [record for record in probate_rows if record.get("grantor")]
    grantee = [record for record in probate_rows if record.get("grantee")]
    print(
        f"probate_total={len(probate_rows)} probate_confirmed_property_matched={len(confirmed)} "
        f"probate_needs_review={len(needs_review)} probate_candidate_addresses_stored={len(candidate_addresses)} "
        f"probate_name_only={len(name_only)} probate_grantor_populated={len(grantor)} "
        f"probate_grantee_populated={len(grantee)}"
    )


def build_records() -> dict:
    statuses: list[SourceStatus] = []
    records = []
    tax_records = tax_delinquent_records(statuses)
    tax_owner_index = owner_index(tax_records)
    sheriff_records = sheriff_sale_records(statuses, tax_owner_index)
    foreclosure_index = {address_key(record.get("property_address", "")): record for record in sheriff_records if record.get("property_address")}
    records.extend(sheriff_records)
    records.extend(tax_records)
    records.extend(code_violation_records(statuses, tax_owner_index, foreclosure_index))
    records.extend(probate_records(statuses, owner_name_index(tax_records)))
    records.extend(placeholder_records(statuses))
    records = dedupe(records)
    apply_probate_property_owner_search(records, statuses)
    apply_recorder_probate_enrichment(records, statuses)
    apply_assessor_enrichment(records, statuses)
    apply_absentee_detection(records, statuses)
    apply_institutional_owner_tags(records)
    print_probate_review_summary(records)
    sheriff = [record for record in records if record.get("lead_type_key") == "foreclosure"]
    print(f"final_records={len(records)} final_sheriff_records={len(sheriff)} final_tax_delinquent_records={sum(1 for record in records if record.get('lead_type_key') == 'tax_delinquent')} final_code_records={sum(1 for record in records if record.get('lead_type_key') == 'code_violation')} final_probate_records={sum(1 for record in records if record.get('lead_type_key') == 'probate')}")
    return {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "Fort Wayne / Allen County, Indiana public-source pipeline",
        "status": "live_partial_with_stubs",
        "source_status": [status.__dict__ for status in statuses],
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/records.json")
    args = parser.parse_args()
    payload = build_records()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(payload['records'])} records to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
