"""
GST e-way bill generation, via a GST Suvidha Provider (GSP).

You cannot call GSTN directly as an unregistered application — production
access goes through a licensed GSP (e.g. ClearTax, MasterGST, Cygnet) who
handle the government auth handshake. This adapter assumes that shape.

GST_RATE_SLABS below are the standard slabs; item-to-slab mapping (HSN code
lookup) is intentionally left to a real GST-rate-master data source — do not
hardcode an "average" rate in production.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.integrations.errors import NotConfiguredError, UpstreamError

VALID_GST_RATES = {0.0, 0.1, 0.25, 3.0, 5.0, 12.0, 18.0, 28.0}


@dataclass
class EWayBillRequest:
    supplier_gstin: str
    recipient_gstin: str
    doc_number: str
    doc_date: str  # DD/MM/YYYY per GSTN spec
    item_description: str
    hsn_code: str
    taxable_value: float
    gst_rate: float
    transport_mode: str  # "Road", "Rail", "Air", "Ship"
    distance_km: int


@dataclass
class EWayBillResponse:
    ewb_number: str
    valid_until: str
    raw: dict


def validate_gst_rate(rate: float) -> bool:
    """Deterministic check — not an LLM call. Guardrail agent uses this directly."""
    return round(rate, 2) in VALID_GST_RATES


def generate_e_way_bill(req: EWayBillRequest) -> EWayBillResponse:
    if not (settings.gsp_client_id and settings.gsp_client_secret and settings.gsp_base_url):
        raise NotConfiguredError(
            "GST e-way bill", ["GSP_CLIENT_ID", "GSP_CLIENT_SECRET", "GSP_BASE_URL"]
        )

    if not validate_gst_rate(req.gst_rate):
        raise ValueError(f"GST rate {req.gst_rate} is not a valid slab: {VALID_GST_RATES}")

    # TODO(deploy): real call through your GSP's SDK/REST API, e.g.
    #   resp = httpx.post(f"{settings.gsp_base_url}/ewaybill/generate",
    #                      auth=(settings.gsp_client_id, settings.gsp_client_secret),
    #                      json=asdict(req), timeout=20)
    #   resp.raise_for_status()
    #   data = resp.json()
    #   return EWayBillResponse(data["ewbNo"], data["validUpto"], data)
    raise UpstreamError(
        "E-way bill generation is stubbed pending GSP integration. "
        "This call requires human-in-the-loop approval regardless (see "
        "app/agents/guardrails.py) — never auto-submit tax filings without it."
    )
