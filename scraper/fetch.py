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
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin
from urllib.request import Request, urlopen


ROOT = "https://www.allencounty.in.gov"
SHERIFF_ROOT = "https://www.allencountysheriff.org"
DELINQUENT_URL = f"{ROOT}/824/Delinquent-Property-List"
TAX_SALE_URL = f"{ROOT}/321/Tax-Sale"
SHERIFF_URL = f"{SHERIFF_ROOT}/sheriff-sale/"
SHERIFF_ARCHIVE_URL = f"{SHERIFF_ROOT}/2026-sheriff-sales/"
ASSESSOR_URL = f"{ROOT}/164/Assessor"
FORT_WAYNE_311_URL = "https://www.cityoffortwayne.org/311"


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


def extract_links(url: str) -> tuple[list[tuple[str, str]], str]:
    html = fetch_text(url)
    parser = LinkParser()
    parser.feed(html)
    return [(text, urljoin(url, href)) for text, href in parser.links if href], html


def slug(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")


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
    absentee = bool(mailing_address and property_address and mailing_address.lower() != property_address.lower())
    all_sources = list(dict.fromkeys(distress_sources + (["absentee"] if absentee else [])))
    all_tags = list(dict.fromkeys(tags + (["absentee-owner"] if absentee else [])))
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
                notes=f"Live sheriff-sale PDF row. Case {match.group('case')}. Judgment/amount seen: ${amount:,}.",
            )
        )
    return records


def sheriff_sale_records(statuses: list[SourceStatus]) -> list[dict]:
    pdfs, status = sheriff_pdf_urls()
    statuses.append(status)
    records: list[dict] = []
    for pdf_url in pdfs[:6]:
        try:
            text = extract_pdf_text(pdf_url)
            records.extend(parse_sheriff_rows(text, pdf_url))
        except Exception as exc:
            statuses.append(SourceStatus("sheriff_pdf", pdf_url, "stubbed", str(exc)))
    return records


def tax_delinquent_records(statuses: list[SourceStatus]) -> list[dict]:
    try:
        links, html = extract_links(DELINQUENT_URL)
        delinquent_links = [href for text, href in links if "delinquent" in (text + href).lower()]
        detail = f"Live page reachable. Found {len(delinquent_links)} delinquent-document links. Parser not enabled until file format is confirmed."
        statuses.append(SourceStatus("tax_delinquent", DELINQUENT_URL, "stubbed", detail))
    except Exception as exc:
        statuses.append(SourceStatus("tax_delinquent", DELINQUENT_URL, "error", str(exc)))
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


def placeholder_records(statuses: list[SourceStatus]) -> list[dict]:
    today = datetime.now(UTC).date().isoformat()
    statuses.extend(
        [
            SourceStatus("probate", "Allen County court/probate records", "stubbed", "No safe public row parser wired yet."),
            SourceStatus("code_violation", FORT_WAYNE_311_URL, "stubbed", "No safe public code/nuisance row parser wired yet."),
            SourceStatus("assessor", ASSESSOR_URL, "stubbed", "Property/mailing match adapter pending; used later for absentee detection."),
        ]
    )
    return [
        make_record(
            owner_name="Probate Adapter Pending",
            property_address="Allen County Probate Records",
            property_city="Fort Wayne",
            property_state="IN",
            property_zip="",
            lead_type="Probate / Estate Source Stub",
            lead_type_key="probate",
            filed_date=today,
            amount=0,
            public_records_url="https://www.allencounty.in.gov/",
            distress_sources=["probate"],
            tags=["probate", "adapter-stub"],
            notes="Probate/estate adapter placeholder. Live source mapping exists, but no row-level scrape is enabled yet.",
            source_status="stub",
        ),
        make_record(
            owner_name="Code Nuisance Adapter Pending",
            property_address="Fort Wayne Code / Nuisance Records",
            property_city="Fort Wayne",
            property_state="IN",
            property_zip="",
            lead_type="Code / Nuisance Source Stub",
            lead_type_key="code_violation",
            filed_date=today,
            amount=0,
            public_records_url=FORT_WAYNE_311_URL,
            distress_sources=["code_violation"],
            tags=["code-violation", "nuisance", "adapter-stub"],
            notes="Code/nuisance adapter placeholder. Dashboard-compatible structure is ready for a future live Fort Wayne source.",
            source_status="stub",
        ),
    ]


def dedupe(records: Iterable[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for record in records:
        key = (
            slug(record.get("lead_type_key", "")),
            slug(record.get("property_address", "")),
            slug(record.get("filed_date", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


def build_records() -> dict:
    statuses: list[SourceStatus] = []
    records = []
    records.extend(sheriff_sale_records(statuses))
    records.extend(tax_delinquent_records(statuses))
    records.extend(placeholder_records(statuses))
    records = dedupe(records)
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
