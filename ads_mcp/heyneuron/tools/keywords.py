# Copyright 2026 HeyNeuron (BIG GROWTH Sp. z o.o.)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Keyword management tools for Google Ads.

This module exposes MCP tools for creating, updating, pausing and
removing positive and negative keywords in Google Ads accounts.

All mutating tools honour the HeyNeuron safety layer:
 * customer ids are normalised and validated
 * bids are capped by ``ensure_bid_under_cap``
 * dry-run mode short-circuits the actual API call
 * every invocation is recorded in the audit log
"""

from typing import Any, Dict, List, Literal, Optional

from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers
from fastmcp.exceptions import ToolError

from ads_mcp.coordinator import mcp
from ads_mcp.utils import get_googleads_client
from ads_mcp.heyneuron.safety import (
    record,
    ensure_bid_under_cap,
    ensure_customer_id,
    ensure_non_empty,
    dry_run_response,
    is_dry_run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_match_type(client: Any, match_type: Optional[str]) -> Any:
    """Resolves a match type string to the corresponding enum value.

    Defaults to ``EXACT`` when ``match_type`` is ``None`` or empty. This
    conservative default follows the HeyNeuron rule "nie broadcastuj
    negatywów" - always prefer narrow match for negatives when the caller
    does not specify.
    """
    value = (match_type or "EXACT").upper()
    try:
        return client.enums.KeywordMatchTypeEnum[value]
    except KeyError as exc:
        raise ToolError(
            f"Invalid keyword match_type '{match_type}'. "
            "Expected one of: EXACT, PHRASE, BROAD."
        ) from exc


def _split_criterion_id(compound: str) -> tuple[str, str]:
    """Splits an ``ad_group_id~criterion_id`` compound id."""
    if "~" not in compound:
        raise ToolError(
            f"Invalid ad group criterion id '{compound}'. "
            "Expected format: 'ad_group_id~criterion_id'."
        )
    ad_group_id, _, criterion_id = compound.partition("~")
    if not ad_group_id or not criterion_id:
        raise ToolError(
            f"Invalid ad group criterion id '{compound}'. "
            "Expected format: 'ad_group_id~criterion_id'."
        )
    return ad_group_id, criterion_id


def _format_google_ads_errors(ex: GoogleAdsException) -> str:
    """Formats a GoogleAdsException into a human-readable error string."""
    errors = [e.message for e in ex.failure.errors]
    return f"Request ID: {ex.request_id}\n" + "\n".join(errors)


# ---------------------------------------------------------------------------
# add_keywords
# ---------------------------------------------------------------------------


@mcp.tool()
def add_keywords(
    customer_id: str,
    ad_group_id: str,
    keywords: List[Dict[str, Any]],
    cpc_bid_micros: Optional[int] = None,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Adds positive keywords to an ad group.

    Args:
        customer_id: Google Ads customer id (with or without dashes).
        ad_group_id: Target ad group id.
        keywords: List of ``{"text": str, "match_type": "EXACT"|"PHRASE"|"BROAD"}``.
        cpc_bid_micros: Optional CPC bid applied to every created keyword
            (in micros, i.e. 1_000_000 = 1.00 account currency unit).
        dry_run: If true, no API call is made.

    Returns:
        ``{"created": [{"resource_name", "text", "match_type"}]}``
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("keywords", keywords)
    if cpc_bid_micros is not None:
        ensure_bid_under_cap(cpc_bid_micros)

    payload = {
        "ad_group_id": ad_group_id,
        "keywords": keywords,
        "cpc_bid_micros": cpc_bid_micros,
    }

    if is_dry_run(dry_run):
        record("add_keywords", customer_id, payload, dry_run=True)
        return dry_run_response("add_keywords", payload)

    try:
        client = get_googleads_client()
        ag_criterion_service = client.get_service("AdGroupCriterionService")
        ag_service = client.get_service("AdGroupService")
        ad_group_resource = ag_service.ad_group_path(customer_id, ad_group_id)

        operations = []
        for kw in keywords:
            if not isinstance(kw, dict) or "text" not in kw:
                raise ToolError(
                    "Each keyword must be a dict with at least a 'text' field."
                )
            operation = client.get_type("AdGroupCriterionOperation")
            criterion = operation.create
            criterion.ad_group = ad_group_resource
            criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            criterion.keyword.text = kw["text"]
            criterion.keyword.match_type = _resolve_match_type(
                client, kw.get("match_type")
            )
            if cpc_bid_micros is not None:
                criterion.cpc_bid_micros = cpc_bid_micros
            operations.append(operation)

        response = ag_criterion_service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=operations
        )

        created = []
        for result, kw in zip(response.results, keywords):
            created.append(
                {
                    "resource_name": result.resource_name,
                    "text": kw["text"],
                    "match_type": (kw.get("match_type") or "EXACT").upper(),
                }
            )
        result_payload = {"created": created}
    except GoogleAdsException as ex:
        raise ToolError(_format_google_ads_errors(ex)) from ex

    record("add_keywords", customer_id, payload, result=result_payload)
    return result_payload


# ---------------------------------------------------------------------------
# add_negative_keywords
# ---------------------------------------------------------------------------


@mcp.tool()
def add_negative_keywords(
    customer_id: str,
    parent_id: str,
    parent_type: Literal["AD_GROUP", "CAMPAIGN"],
    keywords: List[Dict[str, Any]],
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Adds negative keywords at either ad group or campaign level.

    The default ``match_type`` is ``EXACT`` when not supplied, to avoid
    accidentally broad-blocking useful traffic.

    Args:
        customer_id: Google Ads customer id.
        parent_id: Ad group id or campaign id - depending on ``parent_type``.
        parent_type: ``"AD_GROUP"`` or ``"CAMPAIGN"``.
        keywords: List of ``{"text": str, "match_type": optional}``.
        dry_run: If true, no API call is made.

    Returns:
        ``{"created": [...], "level": "AD_GROUP" | "CAMPAIGN"}``
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("keywords", keywords)

    level = parent_type.upper()
    if level not in ("AD_GROUP", "CAMPAIGN"):
        raise ToolError(
            f"Invalid parent_type '{parent_type}'. Expected AD_GROUP or CAMPAIGN."
        )

    payload = {
        "parent_id": parent_id,
        "parent_type": level,
        "keywords": keywords,
    }

    if is_dry_run(dry_run):
        record("add_negative_keywords", customer_id, payload, dry_run=True)
        response = dry_run_response("add_negative_keywords", payload)
        response.setdefault("level", level)
        return response

    try:
        client = get_googleads_client()

        if level == "AD_GROUP":
            ag_criterion_service = client.get_service("AdGroupCriterionService")
            ad_group_resource = client.get_service("AdGroupService").ad_group_path(
                customer_id, parent_id
            )

            operations = []
            for kw in keywords:
                if not isinstance(kw, dict) or "text" not in kw:
                    raise ToolError(
                        "Each keyword must be a dict with at least a 'text' field."
                    )
                operation = client.get_type("AdGroupCriterionOperation")
                criterion = operation.create
                criterion.ad_group = ad_group_resource
                criterion.negative = True
                criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
                criterion.keyword.text = kw["text"]
                criterion.keyword.match_type = _resolve_match_type(
                    client, kw.get("match_type")
                )
                operations.append(operation)

            response = ag_criterion_service.mutate_ad_group_criteria(
                customer_id=customer_id, operations=operations
            )
        else:  # CAMPAIGN
            campaign_criterion_service = client.get_service(
                "CampaignCriterionService"
            )
            campaign_resource = client.get_service("CampaignService").campaign_path(
                customer_id, parent_id
            )

            operations = []
            for kw in keywords:
                if not isinstance(kw, dict) or "text" not in kw:
                    raise ToolError(
                        "Each keyword must be a dict with at least a 'text' field."
                    )
                operation = client.get_type("CampaignCriterionOperation")
                criterion = operation.create
                criterion.campaign = campaign_resource
                criterion.negative = True
                criterion.keyword.text = kw["text"]
                criterion.keyword.match_type = _resolve_match_type(
                    client, kw.get("match_type")
                )
                operations.append(operation)

            response = campaign_criterion_service.mutate_campaign_criteria(
                customer_id=customer_id, operations=operations
            )

        created = []
        for result, kw in zip(response.results, keywords):
            created.append(
                {
                    "resource_name": result.resource_name,
                    "text": kw["text"],
                    "match_type": (kw.get("match_type") or "EXACT").upper(),
                }
            )
        result_payload = {"created": created, "level": level}
    except GoogleAdsException as ex:
        raise ToolError(_format_google_ads_errors(ex)) from ex

    record("add_negative_keywords", customer_id, payload, result=result_payload)
    return result_payload


# ---------------------------------------------------------------------------
# update_keyword_bids
# ---------------------------------------------------------------------------


@mcp.tool()
def update_keyword_bids(
    customer_id: str,
    ad_group_criterion_ids: List[str],
    new_cpc_bid_micros: int,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Updates the CPC bid for one or more ad group criteria (keywords).

    Args:
        customer_id: Google Ads customer id.
        ad_group_criterion_ids: List of compound ids in the format
            ``"ad_group_id~criterion_id"``.
        new_cpc_bid_micros: The new CPC bid in micros. Validated against
            the HeyNeuron bid cap via ``ensure_bid_under_cap``.
        dry_run: If true, no API call is made.

    Returns:
        ``{"updated": [resource_names], "new_bid_micros": int}``
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("ad_group_criterion_ids", ad_group_criterion_ids)
    ensure_bid_under_cap(new_cpc_bid_micros)

    payload = {
        "ad_group_criterion_ids": ad_group_criterion_ids,
        "new_cpc_bid_micros": new_cpc_bid_micros,
    }

    if is_dry_run(dry_run):
        record("update_keyword_bids", customer_id, payload, dry_run=True)
        return dry_run_response("update_keyword_bids", payload)

    try:
        client = get_googleads_client()
        ag_criterion_service = client.get_service("AdGroupCriterionService")

        operations = []
        for compound_id in ad_group_criterion_ids:
            ad_group_id, criterion_id = _split_criterion_id(compound_id)
            operation = client.get_type("AdGroupCriterionOperation")
            criterion = operation.update
            criterion.resource_name = ag_criterion_service.ad_group_criterion_path(
                customer_id, ad_group_id, criterion_id
            )
            criterion.cpc_bid_micros = new_cpc_bid_micros
            client.copy_from(
                operation.update_mask,
                protobuf_helpers.field_mask(None, criterion._pb),
            )
            operations.append(operation)

        response = ag_criterion_service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=operations
        )
        updated = [r.resource_name for r in response.results]
        result_payload = {"updated": updated, "new_bid_micros": new_cpc_bid_micros}
    except GoogleAdsException as ex:
        raise ToolError(_format_google_ads_errors(ex)) from ex

    record("update_keyword_bids", customer_id, payload, result=result_payload)
    return result_payload


# ---------------------------------------------------------------------------
# pause_keywords
# ---------------------------------------------------------------------------


@mcp.tool()
def pause_keywords(
    customer_id: str,
    ad_group_criterion_ids: List[str],
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Pauses keywords by setting their status to ``PAUSED``.

    Args:
        customer_id: Google Ads customer id.
        ad_group_criterion_ids: List of compound ids in the format
            ``"ad_group_id~criterion_id"``.
        dry_run: If true, no API call is made.

    Returns:
        ``{"paused": [resource_names]}``
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("ad_group_criterion_ids", ad_group_criterion_ids)

    payload = {"ad_group_criterion_ids": ad_group_criterion_ids}

    if is_dry_run(dry_run):
        record("pause_keywords", customer_id, payload, dry_run=True)
        return dry_run_response("pause_keywords", payload)

    try:
        client = get_googleads_client()
        ag_criterion_service = client.get_service("AdGroupCriterionService")

        operations = []
        for compound_id in ad_group_criterion_ids:
            ad_group_id, criterion_id = _split_criterion_id(compound_id)
            operation = client.get_type("AdGroupCriterionOperation")
            criterion = operation.update
            criterion.resource_name = ag_criterion_service.ad_group_criterion_path(
                customer_id, ad_group_id, criterion_id
            )
            criterion.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
            client.copy_from(
                operation.update_mask,
                protobuf_helpers.field_mask(None, criterion._pb),
            )
            operations.append(operation)

        response = ag_criterion_service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=operations
        )
        paused = [r.resource_name for r in response.results]
        result_payload = {"paused": paused}
    except GoogleAdsException as ex:
        raise ToolError(_format_google_ads_errors(ex)) from ex

    record("pause_keywords", customer_id, payload, result=result_payload)
    return result_payload


# ---------------------------------------------------------------------------
# remove_keywords
# ---------------------------------------------------------------------------


@mcp.tool()
def remove_keywords(
    customer_id: str,
    ad_group_criterion_ids: List[str],
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Removes keywords irreversibly (status ``REMOVED``).

    This operation cannot be undone. The same ``ad_group_criterion`` id
    cannot be recreated once removed - a new keyword with a new id must
    be created via :func:`add_keywords` instead.

    Args:
        customer_id: Google Ads customer id.
        ad_group_criterion_ids: List of compound ids in the format
            ``"ad_group_id~criterion_id"``.
        dry_run: If true, no API call is made.

    Returns:
        ``{"removed": [resource_names]}``
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("ad_group_criterion_ids", ad_group_criterion_ids)

    payload = {"ad_group_criterion_ids": ad_group_criterion_ids}

    if is_dry_run(dry_run):
        record("remove_keywords", customer_id, payload, dry_run=True)
        return dry_run_response("remove_keywords", payload)

    try:
        client = get_googleads_client()
        ag_criterion_service = client.get_service("AdGroupCriterionService")

        operations = []
        for compound_id in ad_group_criterion_ids:
            ad_group_id, criterion_id = _split_criterion_id(compound_id)
            operation = client.get_type("AdGroupCriterionOperation")
            operation.remove = ag_criterion_service.ad_group_criterion_path(
                customer_id, ad_group_id, criterion_id
            )
            operations.append(operation)

        response = ag_criterion_service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=operations
        )
        removed = [r.resource_name for r in response.results]
        result_payload = {"removed": removed}
    except GoogleAdsException as ex:
        raise ToolError(_format_google_ads_errors(ex)) from ex

    record("remove_keywords", customer_id, payload, result=result_payload)
    return result_payload
