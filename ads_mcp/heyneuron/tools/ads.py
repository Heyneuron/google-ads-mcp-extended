# Copyright 2026 HeyNeuron (BIG GROWTH Sp. z o.o.)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""HeyNeuron write tools for Google Ads ad group, ad, and asset management.

This module exposes four MCP tools that the upstream
``googleads/google-ads-mcp`` project does not ship:

* :func:`create_ad_group` - creates an ad group inside an existing campaign.
* :func:`create_responsive_search_ad` - creates a Responsive Search Ad. The
  ad is **always** created with ``status=PAUSED`` so a human can review it
  before it starts serving impressions.
* :func:`create_sitelink_asset` - creates a sitelink ``Asset`` that can be
  linked to one or more campaigns or ad groups.
* :func:`link_asset_to_campaign` - links an existing asset (sitelink,
  callout, structured snippet, ...) to a campaign as a ``CampaignAsset``.

Every mutation goes through the HeyNeuron safety layer (``ensure_customer_id``,
``ensure_bid_under_cap``, audit ``record``, ``dry_run`` preview) so that an
LLM-driven mistake cannot silently start spending a client's media budget.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException

from ads_mcp.coordinator import mcp
from ads_mcp.heyneuron.safety import (
    dry_run_response,
    ensure_bid_under_cap,
    ensure_customer_id,
    ensure_non_empty,
    is_dry_run,
    record,
)
from ads_mcp.utils import get_googleads_client


def _format_errors(ex: GoogleAdsException) -> str:
    """Formats a GoogleAdsException into a human readable error string."""
    errors = [f"{e.error_code}: {e.message}" for e in ex.failure.errors]
    return f"Request ID: {ex.request_id}\n" + "\n".join(errors)


def _resolve_asset_field_type(client, field_type: str):
    """Looks up an ``AssetFieldType`` enum from a user string."""
    try:
        return client.enums.AssetFieldTypeEnum[field_type.upper()]
    except KeyError as ex:
        raise ToolError(
            f"Unknown field_type '{field_type}'. Expected one of SITELINK, "
            "CALLOUT, STRUCTURED_SNIPPET."
        ) from ex


@mcp.tool()
def create_ad_group(
    customer_id: str,
    campaign_id: str,
    name: str,
    cpc_bid_micros: Optional[int] = None,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Creates a new ad group in the given campaign.

    Ad groups are containers and do not directly trigger spend, so the ad
    group is created with ``status=ENABLED``. The ads inside the ad group
    are what controls serving: those are created ``PAUSED`` by
    :func:`create_responsive_search_ad` and must be enabled explicitly.

    Args:
        customer_id: 10-digit Google Ads customer id (dashes allowed).
        campaign_id: Numeric campaign id (string to avoid JSON rounding).
        name: Human readable ad group name. Must be unique in the campaign.
        cpc_bid_micros: Optional default CPC bid in micros
            (1_000_000 micros = 1 unit of account currency). Enforced
            against the HeyNeuron bid cap.
        dry_run: If True, returns a preview instead of calling the API.
            If None, falls back to ``GOOGLE_ADS_MCP_DEFAULT_DRY_RUN``.

    Returns:
        Dict with ``ad_group_resource_name``.
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("campaign_id", [campaign_id])
    ensure_non_empty("name", [name])
    if cpc_bid_micros is not None:
        if cpc_bid_micros <= 0:
            raise ToolError("cpc_bid_micros must be a positive integer.")
        ensure_bid_under_cap(cpc_bid_micros)

    payload = {
        "customer_id": customer_id,
        "campaign_id": campaign_id,
        "name": name,
        "cpc_bid_micros": cpc_bid_micros,
        "status": "ENABLED",
    }

    if is_dry_run(dry_run):
        record("create_ad_group", customer_id, payload, dry_run=True)
        return dry_run_response("create_ad_group", payload)

    try:
        client = get_googleads_client()
        ad_group_service = client.get_service("AdGroupService")
        campaign_service = client.get_service("CampaignService")
        operation = client.get_type("AdGroupOperation")

        ad_group = operation.create
        ad_group.name = name
        ad_group.campaign = campaign_service.campaign_path(
            customer_id, campaign_id
        )
        ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
        ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
        if cpc_bid_micros is not None:
            ad_group.cpc_bid_micros = cpc_bid_micros

        response = ad_group_service.mutate_ad_groups(
            customer_id=customer_id, operations=[operation]
        )
        result = {
            "ad_group_resource_name": response.results[0].resource_name,
        }
    except GoogleAdsException as ex:
        raise ToolError(_format_errors(ex)) from ex

    record("create_ad_group", customer_id, payload, result=result)
    return result


@mcp.tool()
def create_responsive_search_ad(
    customer_id: str,
    ad_group_id: str,
    headlines: List[str],
    descriptions: List[str],
    final_urls: List[str],
    path1: Optional[str] = None,
    path2: Optional[str] = None,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Creates a Responsive Search Ad (RSA) in the given ad group.

    SAFETY: the ad is **always** created with ``status=PAUSED``. The caller
    must enable it explicitly through a separate mutation once a human has
    reviewed the copy. An RSA requires 3-15 headlines and 2-4 descriptions
    per the Google Ads API; the tool enforces that up front.

    Args:
        customer_id: 10-digit Google Ads customer id.
        ad_group_id: Numeric ad group id (string).
        headlines: List of 3-15 headline strings (max 30 chars each).
        descriptions: List of 2-4 description strings (max 90 chars each).
        final_urls: List of destination URLs for the ad.
        path1: Optional first display-URL path segment (max 15 chars).
        path2: Optional second display-URL path segment (max 15 chars).
        dry_run: Preview the mutation without applying it.

    Returns:
        Dict with ``ad_group_ad_resource_name`` and ``status`` = ``"PAUSED"``.
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("ad_group_id", [ad_group_id])
    ensure_non_empty("headlines", headlines)
    ensure_non_empty("descriptions", descriptions)
    ensure_non_empty("final_urls", final_urls)
    if len(headlines) < 3 or len(headlines) > 15:
        raise ToolError("RSA requires 3-15 headlines.")
    if len(descriptions) < 2 or len(descriptions) > 4:
        raise ToolError("RSA requires 2-4 descriptions.")

    payload = {
        "customer_id": customer_id,
        "ad_group_id": ad_group_id,
        "headlines": headlines,
        "descriptions": descriptions,
        "final_urls": final_urls,
        "path1": path1,
        "path2": path2,
        "status": "PAUSED",
    }

    if is_dry_run(dry_run):
        record("create_responsive_search_ad", customer_id, payload, dry_run=True)
        return dry_run_response("create_responsive_search_ad", payload)

    try:
        client = get_googleads_client()
        ad_group_ad_service = client.get_service("AdGroupAdService")
        ad_group_service = client.get_service("AdGroupService")
        operation = client.get_type("AdGroupAdOperation")

        ad_group_ad = operation.create
        ad_group_ad.ad_group = ad_group_service.ad_group_path(
            customer_id, ad_group_id
        )
        ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED

        ad = ad_group_ad.ad
        ad.final_urls.extend(final_urls)

        rsa = ad.responsive_search_ad
        for headline in headlines:
            asset = client.get_type("AdTextAsset")
            asset.text = headline
            rsa.headlines.append(asset)
        for description in descriptions:
            asset = client.get_type("AdTextAsset")
            asset.text = description
            rsa.descriptions.append(asset)
        if path1:
            rsa.path1 = path1
        if path2:
            rsa.path2 = path2

        response = ad_group_ad_service.mutate_ad_group_ads(
            customer_id=customer_id, operations=[operation]
        )
        result = {
            "ad_group_ad_resource_name": response.results[0].resource_name,
            "status": "PAUSED",
        }
    except GoogleAdsException as ex:
        raise ToolError(_format_errors(ex)) from ex

    record("create_responsive_search_ad", customer_id, payload, result=result)
    return result


@mcp.tool()
def create_sitelink_asset(
    customer_id: str,
    link_text: str,
    final_urls: List[str],
    description_line_1: Optional[str] = None,
    description_line_2: Optional[str] = None,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Creates a sitelink ``Asset`` for later linking to a campaign or ad group.

    Sitelinks are reusable assets: after creation, link the returned
    resource name to one or more campaigns via :func:`link_asset_to_campaign`.

    Args:
        customer_id: 10-digit Google Ads customer id.
        link_text: Visible link text (max 25 chars).
        final_urls: List of destination URLs for the sitelink.
        description_line_1: Optional first description line (max 35 chars).
            Both description lines must be supplied together.
        description_line_2: Optional second description line (max 35 chars).
        dry_run: Preview the mutation without applying it.

    Returns:
        Dict with ``asset_resource_name``.
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("link_text", [link_text])
    ensure_non_empty("final_urls", final_urls)
    if bool(description_line_1) != bool(description_line_2):
        raise ToolError(
            "description_line_1 and description_line_2 must be provided "
            "together or not at all."
        )

    payload = {
        "customer_id": customer_id,
        "link_text": link_text,
        "final_urls": final_urls,
        "description_line_1": description_line_1,
        "description_line_2": description_line_2,
    }

    if is_dry_run(dry_run):
        record("create_sitelink_asset", customer_id, payload, dry_run=True)
        return dry_run_response("create_sitelink_asset", payload)

    try:
        client = get_googleads_client()
        asset_service = client.get_service("AssetService")
        operation = client.get_type("AssetOperation")

        asset = operation.create
        asset.final_urls.extend(final_urls)
        sitelink = asset.sitelink_asset
        sitelink.link_text = link_text
        if description_line_1 and description_line_2:
            sitelink.description1 = description_line_1
            sitelink.description2 = description_line_2

        response = asset_service.mutate_assets(
            customer_id=customer_id, operations=[operation]
        )
        result = {
            "asset_resource_name": response.results[0].resource_name,
        }
    except GoogleAdsException as ex:
        raise ToolError(_format_errors(ex)) from ex

    record("create_sitelink_asset", customer_id, payload, result=result)
    return result


@mcp.tool()
def link_asset_to_campaign(
    customer_id: str,
    campaign_id: str,
    asset_resource_name: str,
    field_type: Literal["SITELINK", "CALLOUT", "STRUCTURED_SNIPPET"],
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Links an existing asset to a campaign as a ``CampaignAsset``.

    The asset must already exist (see :func:`create_sitelink_asset`) and
    its type must match ``field_type``. This mutation is idempotent at the
    (campaign, asset, field_type) level - the Google Ads API will return
    an error if the same link already exists.

    Args:
        customer_id: 10-digit Google Ads customer id.
        campaign_id: Numeric campaign id (string).
        asset_resource_name: Fully qualified asset resource name, e.g.
            ``customers/123/assets/456``.
        field_type: How the asset is attached to the campaign. One of
            ``SITELINK``, ``CALLOUT``, ``STRUCTURED_SNIPPET``.
        dry_run: Preview the mutation without applying it.

    Returns:
        Dict with ``campaign_asset_resource_name``.
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("campaign_id", [campaign_id])
    ensure_non_empty("asset_resource_name", [asset_resource_name])
    if field_type.upper() not in {"SITELINK", "CALLOUT", "STRUCTURED_SNIPPET"}:
        raise ToolError(
            f"field_type must be SITELINK, CALLOUT, or STRUCTURED_SNIPPET "
            f"(got '{field_type}')."
        )

    payload = {
        "customer_id": customer_id,
        "campaign_id": campaign_id,
        "asset_resource_name": asset_resource_name,
        "field_type": field_type.upper(),
    }

    if is_dry_run(dry_run):
        record("link_asset_to_campaign", customer_id, payload, dry_run=True)
        return dry_run_response("link_asset_to_campaign", payload)

    try:
        client = get_googleads_client()
        campaign_asset_service = client.get_service("CampaignAssetService")
        campaign_service = client.get_service("CampaignService")
        operation = client.get_type("CampaignAssetOperation")

        campaign_asset = operation.create
        campaign_asset.campaign = campaign_service.campaign_path(
            customer_id, campaign_id
        )
        campaign_asset.asset = asset_resource_name
        campaign_asset.field_type = _resolve_asset_field_type(
            client, field_type
        )

        response = campaign_asset_service.mutate_campaign_assets(
            customer_id=customer_id, operations=[operation]
        )
        result = {
            "campaign_asset_resource_name": response.results[0].resource_name,
        }
    except GoogleAdsException as ex:
        raise ToolError(_format_errors(ex)) from ex

    record("link_asset_to_campaign", customer_id, payload, result=result)
    return result
