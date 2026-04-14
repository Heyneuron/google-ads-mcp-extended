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

"""HeyNeuron write tools for Google Ads Campaign management.

This module exposes three MCP tools that the upstream
``googleads/google-ads-mcp`` project does not ship:

* :func:`create_campaign` - creates a new campaign (always ``PAUSED``)
  alongside a dedicated non-shared ``CampaignBudget``.
* :func:`update_campaign_status` - enables / pauses / removes an existing
  campaign via ``FieldMask``.
* :func:`update_campaign_budget` - rewires a campaign's daily budget
  amount in micros on its existing non-shared budget.

Every mutation goes through the HeyNeuron safety layer
(``ensure_customer_id``, ``ensure_budget_under_cap``, audit ``record``,
``dry_run`` preview) so a runaway agent cannot quietly burn a client's
media budget. The tools deliberately never auto-enable campaigns - the
caller must explicitly flip ``status`` to ``ENABLED`` through
:func:`update_campaign_status` once a human has reviewed the setup.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Literal, Optional

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers

from ads_mcp.coordinator import mcp
from ads_mcp.heyneuron.safety import (
    dry_run_response,
    ensure_budget_under_cap,
    ensure_customer_id,
    ensure_non_empty,
    is_dry_run,
    record,
)
from ads_mcp.utils import get_googleads_client, get_googleads_service


def _format_errors(ex: GoogleAdsException) -> str:
    """Formats a GoogleAdsException into a human readable error string."""
    errors = [f"{e.error_code}: {e.message}" for e in ex.failure.errors]
    return f"Request ID: {ex.request_id}\n" + "\n".join(errors)


def _set_bidding_strategy(client, campaign, bid_strategy_type: str) -> None:
    """Attaches a standard bidding strategy to a ``Campaign`` proto.

    Only the bidding strategies that make sense for a freshly created
    campaign are supported here. Shared / portfolio strategies must be
    attached separately via ``bidding_strategy`` resource name.
    """
    strategy = (bid_strategy_type or "").upper()
    if strategy == "MAXIMIZE_CONVERSIONS":
        campaign.maximize_conversions = client.get_type("MaximizeConversions")
    elif strategy == "MAXIMIZE_CONVERSION_VALUE":
        campaign.maximize_conversion_value = client.get_type(
            "MaximizeConversionValue"
        )
    elif strategy == "MANUAL_CPC":
        manual_cpc = client.get_type("ManualCpc")
        campaign.manual_cpc = manual_cpc
    elif strategy == "TARGET_SPEND":
        campaign.target_spend = client.get_type("TargetSpend")
    elif strategy == "TARGET_CPA":
        campaign.target_cpa = client.get_type("TargetCpa")
    elif strategy == "TARGET_ROAS":
        campaign.target_roas = client.get_type("TargetRoas")
    else:
        raise ToolError(
            f"Unsupported bid_strategy_type '{bid_strategy_type}'. Supported: "
            "MAXIMIZE_CONVERSIONS, MAXIMIZE_CONVERSION_VALUE, MANUAL_CPC, "
            "TARGET_SPEND, TARGET_CPA, TARGET_ROAS."
        )


def _resolve_channel_type(client, campaign_type: str):
    """Looks up an ``AdvertisingChannelType`` enum from a user string."""
    try:
        return client.enums.AdvertisingChannelTypeEnum[campaign_type.upper()]
    except KeyError as ex:
        raise ToolError(
            f"Unknown campaign_type '{campaign_type}'. Expected one of "
            "SEARCH, DISPLAY, SHOPPING, VIDEO, PERFORMANCE_MAX, "
            "LOCAL_SERVICES, DEMAND_GEN, MULTI_CHANNEL, DISCOVERY."
        ) from ex


def _resolve_campaign_status(client, status: str):
    """Looks up a ``CampaignStatus`` enum from a user string."""
    try:
        return client.enums.CampaignStatusEnum[status.upper()]
    except KeyError as ex:
        raise ToolError(
            f"Unknown campaign status '{status}'. Expected one of ENABLED, "
            "PAUSED, REMOVED."
        ) from ex


@mcp.tool()
def create_campaign(
    customer_id: str,
    name: str,
    budget_micros: int,
    campaign_type: str = "SEARCH",
    bid_strategy_type: str = "MAXIMIZE_CONVERSIONS",
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Creates a new Google Ads campaign in ``PAUSED`` state.

    For safety, every campaign created through this tool starts PAUSED.
    The caller must explicitly flip it to ``ENABLED`` via
    :func:`update_campaign_status` after a human review. A dedicated
    non-shared ``CampaignBudget`` is created in the same call and wired
    to the new campaign.

    Args:
        customer_id: 10-digit Google Ads customer id (dashes allowed).
        name: Human readable campaign name. Must be unique in the account.
        budget_micros: Daily budget in micros (1_000_000 micros = 1 unit
            of account currency). Enforced against the safety cap.
        campaign_type: Advertising channel type. Defaults to ``SEARCH``.
            Supported values mirror ``AdvertisingChannelTypeEnum``.
        bid_strategy_type: Standard bidding strategy.
            Defaults to ``MAXIMIZE_CONVERSIONS``.
        dry_run: If True, returns a preview instead of calling the API.
            If None, falls back to the ``GOOGLE_ADS_MCP_DEFAULT_DRY_RUN``
            env var.

    Returns:
        Dict with ``campaign_resource_name``, ``budget_resource_name`` and
        ``status`` = ``"PAUSED"``.
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("name", [name])
    if budget_micros <= 0:
        raise ToolError("budget_micros must be a positive integer.")
    ensure_budget_under_cap(budget_micros)

    payload = {
        "customer_id": customer_id,
        "name": name,
        "budget_micros": budget_micros,
        "campaign_type": campaign_type,
        "bid_strategy_type": bid_strategy_type,
        "status": "PAUSED",
    }

    if is_dry_run(dry_run):
        record("create_campaign", customer_id, payload, dry_run=True)
        return dry_run_response("create_campaign", payload)

    try:
        client = get_googleads_client()

        # 1. Create a dedicated non-shared CampaignBudget.
        budget_service = client.get_service("CampaignBudgetService")
        budget_operation = client.get_type("CampaignBudgetOperation")
        budget = budget_operation.create
        budget.name = f"{name} Budget {uuid.uuid4().hex[:8]}"
        budget.amount_micros = budget_micros
        budget.delivery_method = (
            client.enums.BudgetDeliveryMethodEnum.STANDARD
        )
        budget.explicitly_shared = False

        budget_response = budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[budget_operation]
        )
        budget_resource_name = budget_response.results[0].resource_name

        # 2. Create the Campaign referencing the fresh budget.
        campaign_service = client.get_service("CampaignService")
        campaign_operation = client.get_type("CampaignOperation")
        campaign = campaign_operation.create
        campaign.name = name
        campaign.advertising_channel_type = _resolve_channel_type(
            client, campaign_type
        )
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        campaign.campaign_budget = budget_resource_name
        # Network settings default to search network only for SEARCH;
        # callers wanting fine grained control should update later.
        if campaign_type.upper() == "SEARCH":
            campaign.network_settings.target_google_search = True
            campaign.network_settings.target_search_network = True
            campaign.network_settings.target_content_network = False
            campaign.network_settings.target_partner_search_network = False

        _set_bidding_strategy(client, campaign, bid_strategy_type)

        campaign_response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[campaign_operation]
        )
        campaign_resource_name = campaign_response.results[0].resource_name

        result = {
            "campaign_resource_name": campaign_resource_name,
            "budget_resource_name": budget_resource_name,
            "status": "PAUSED",
        }
    except GoogleAdsException as ex:
        raise ToolError(_format_errors(ex)) from ex

    record("create_campaign", customer_id, payload, result=result)
    return result


@mcp.tool()
def update_campaign_status(
    customer_id: str,
    campaign_id: str,
    status: Literal["ENABLED", "PAUSED", "REMOVED"],
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Enables, pauses, or removes an existing campaign.

    This is the only path through which a HeyNeuron-managed campaign
    should ever transition to ``ENABLED`` - creation always starts
    ``PAUSED`` to keep an LLM from silently starting spend.

    Args:
        customer_id: 10-digit Google Ads customer id.
        campaign_id: Numeric campaign id (string to avoid JSON rounding).
        status: Target campaign status.
        dry_run: Preview the mutation without applying it.

    Returns:
        Dict with ``campaign_resource_name`` and ``new_status``.
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("campaign_id", [campaign_id])
    if status.upper() not in {"ENABLED", "PAUSED", "REMOVED"}:
        raise ToolError(
            f"status must be ENABLED, PAUSED or REMOVED (got '{status}')."
        )

    payload = {
        "customer_id": customer_id,
        "campaign_id": campaign_id,
        "status": status.upper(),
    }

    if is_dry_run(dry_run):
        record("update_campaign_status", customer_id, payload, dry_run=True)
        return dry_run_response("update_campaign_status", payload)

    try:
        client = get_googleads_client()
        campaign_service = client.get_service("CampaignService")

        resource_name = campaign_service.campaign_path(
            customer_id, campaign_id
        )

        campaign_operation = client.get_type("CampaignOperation")
        campaign = campaign_operation.update
        campaign.resource_name = resource_name
        campaign.status = _resolve_campaign_status(client, status)

        client.copy_from(
            campaign_operation.update_mask,
            protobuf_helpers.field_mask(None, campaign._pb),
        )

        response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[campaign_operation]
        )
        result = {
            "campaign_resource_name": response.results[0].resource_name,
            "new_status": status.upper(),
        }
    except GoogleAdsException as ex:
        raise ToolError(_format_errors(ex)) from ex

    record("update_campaign_status", customer_id, payload, result=result)
    return result


@mcp.tool()
def update_campaign_budget(
    customer_id: str,
    campaign_id: str,
    new_budget_micros: int,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Updates the daily budget amount for an existing campaign.

    The tool first resolves the ``campaign_budget`` resource name attached
    to the campaign via a GAQL ``SELECT`` on the ``campaign`` resource,
    then mutates the ``CampaignBudget`` directly so that the change is
    effective on the next impression batch.

    Args:
        customer_id: 10-digit Google Ads customer id.
        campaign_id: Numeric campaign id (string).
        new_budget_micros: New daily budget in micros. Enforced against
            the configured HeyNeuron safety cap.
        dry_run: Preview the mutation without applying it.

    Returns:
        Dict with ``budget_resource_name`` and ``new_amount_micros``.
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("campaign_id", [campaign_id])
    if new_budget_micros <= 0:
        raise ToolError("new_budget_micros must be a positive integer.")
    ensure_budget_under_cap(new_budget_micros)

    payload = {
        "customer_id": customer_id,
        "campaign_id": campaign_id,
        "new_budget_micros": new_budget_micros,
    }

    if is_dry_run(dry_run):
        record("update_campaign_budget", customer_id, payload, dry_run=True)
        return dry_run_response("update_campaign_budget", payload)

    try:
        client = get_googleads_client()

        # 1. Resolve the budget resource name for this campaign.
        ga_service = get_googleads_service("GoogleAdsService")
        query = (
            "SELECT campaign.campaign_budget FROM campaign "
            f"WHERE campaign.id = {int(campaign_id)}"
        )
        search_response = ga_service.search(
            customer_id=customer_id, query=query
        )

        budget_resource_name: Optional[str] = None
        for row in search_response:
            budget_resource_name = row.campaign.campaign_budget
            break

        if not budget_resource_name:
            raise ToolError(
                f"Campaign id {campaign_id} not found for customer "
                f"{customer_id} or it has no attached budget."
            )

        # 2. Mutate the budget.
        budget_service = client.get_service("CampaignBudgetService")
        budget_operation = client.get_type("CampaignBudgetOperation")
        budget = budget_operation.update
        budget.resource_name = budget_resource_name
        budget.amount_micros = new_budget_micros

        client.copy_from(
            budget_operation.update_mask,
            protobuf_helpers.field_mask(None, budget._pb),
        )

        budget_response = budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[budget_operation]
        )
        result = {
            "budget_resource_name": budget_response.results[0].resource_name,
            "new_amount_micros": new_budget_micros,
        }
    except GoogleAdsException as ex:
        raise ToolError(_format_errors(ex)) from ex

    record("update_campaign_budget", customer_id, payload, result=result)
    return result
