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

"""HeyNeuron write tools for Google Ads Assets and Performance Max.

This module exposes four MCP tools that the upstream
``googleads/google-ads-mcp`` project does not ship. They cover the
minimum surface area required to bootstrap a Performance Max campaign
end-to-end from an agent workflow:

* :func:`upload_image_asset` - creates an ``IMAGE`` ``Asset`` from a
  remote URL or a local file path.
* :func:`upload_text_asset` - creates a ``TEXT`` ``Asset`` to be reused
  as headlines / descriptions in a Performance Max asset group.
* :func:`create_asset_group` - creates a Performance Max ``AssetGroup``
  in ``PAUSED`` state, linked to an existing PMax campaign.
* :func:`link_asset_to_asset_group` - wires an existing ``Asset`` into
  an ``AssetGroup`` with an explicit ``AssetFieldType``.

Every mutation goes through the HeyNeuron safety layer
(``ensure_customer_id``, audit ``record``, ``dry_run`` preview) so a
runaway agent cannot quietly mutate a client account. Asset groups are
always created ``PAUSED`` - a human review must flip the parent campaign
or the asset group itself before anything serves.
"""

from __future__ import annotations

import urllib.request
from typing import Any, Dict, List, Literal, Optional

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException

from ads_mcp.coordinator import mcp
from ads_mcp.heyneuron.safety import (
    dry_run_response,
    ensure_customer_id,
    ensure_non_empty,
    is_dry_run,
    record,
)
from ads_mcp.utils import get_googleads_client


# Field types that make sense when linking an Asset into a Performance
# Max AssetGroup. Kept in sync with Google's ``AssetFieldTypeEnum``.
AssetFieldTypeLiteral = Literal[
    "HEADLINE",
    "LONG_HEADLINE",
    "DESCRIPTION",
    "BUSINESS_NAME",
    "MARKETING_IMAGE",
    "SQUARE_MARKETING_IMAGE",
    "LOGO",
    "YOUTUBE_VIDEO",
]


def _format_errors(ex: GoogleAdsException) -> str:
    """Formats a ``GoogleAdsException`` into a human readable string."""
    errors = [f"{e.error_code}: {e.message}" for e in ex.failure.errors]
    return f"Request ID: {ex.request_id}\n" + "\n".join(errors)


def _load_image_bytes(
    image_url: Optional[str], image_path: Optional[str]
) -> bytes:
    """Reads the raw bytes for an image from URL or local path.

    Exactly one of ``image_url`` / ``image_path`` must be provided. The
    caller is expected to have validated that already; this helper just
    performs the IO and wraps errors into ``ToolError``.
    """
    if image_url:
        try:
            with urllib.request.urlopen(image_url) as resp:  # noqa: S310
                return resp.read()
        except Exception as ex:  # pragma: no cover - network failure path
            raise ToolError(
                f"Failed to download image from '{image_url}': {ex}"
            ) from ex
    try:
        with open(image_path, "rb") as fp:  # type: ignore[arg-type]
            return fp.read()
    except OSError as ex:
        raise ToolError(
            f"Failed to read local image '{image_path}': {ex}"
        ) from ex


@mcp.tool()
def upload_image_asset(
    customer_id: str,
    name: str,
    image_url: Optional[str] = None,
    image_path: Optional[str] = None,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Uploads an image as a Google Ads ``IMAGE`` asset.

    The image bytes can either be pulled from a publicly reachable URL
    or loaded from a local file path. Exactly one of ``image_url`` or
    ``image_path`` must be provided.

    Args:
        customer_id: 10-digit Google Ads customer id (dashes allowed).
        name: Display name for the resulting asset. Must be non empty.
        image_url: Optional HTTP(S) URL to download the image from.
        image_path: Optional local filesystem path to the image.
        dry_run: If True, returns a preview instead of calling the API.
            If None, falls back to the ``GOOGLE_ADS_MCP_DEFAULT_DRY_RUN``
            env var.

    Returns:
        Dict with ``asset_resource_name`` pointing at the new asset.
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("name", [name])
    if not image_url and not image_path:
        raise ToolError("One of image_url or image_path is required.")
    if image_url and image_path:
        raise ToolError("Provide only one of image_url or image_path.")

    payload = {
        "customer_id": customer_id,
        "name": name,
        "image_url": image_url,
        "image_path": image_path,
    }

    if is_dry_run(dry_run):
        record("upload_image_asset", customer_id, payload, dry_run=True)
        return dry_run_response("upload_image_asset", payload)

    image_bytes = _load_image_bytes(image_url, image_path)

    try:
        client = get_googleads_client()
        asset_service = client.get_service("AssetService")
        operation = client.get_type("AssetOperation")
        asset = operation.create
        asset.name = name
        asset.type_ = client.enums.AssetTypeEnum.IMAGE
        asset.image_asset.data = image_bytes

        response = asset_service.mutate_assets(
            customer_id=customer_id, operations=[operation]
        )
        result = {
            "asset_resource_name": response.results[0].resource_name,
        }
    except GoogleAdsException as ex:
        raise ToolError(_format_errors(ex)) from ex

    record("upload_image_asset", customer_id, payload, result=result)
    return result


@mcp.tool()
def upload_text_asset(
    customer_id: str,
    text: str,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Creates a reusable ``TEXT`` asset.

    Text assets are typically consumed by Performance Max asset groups
    as headlines, long headlines or descriptions. Link the resulting
    resource name via :func:`link_asset_to_asset_group`.

    Args:
        customer_id: 10-digit Google Ads customer id.
        text: Text content for the asset. Must be non empty.
        dry_run: Preview the mutation without applying it.

    Returns:
        Dict with ``asset_resource_name`` pointing at the new asset.
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("text", [text])

    payload = {
        "customer_id": customer_id,
        "text": text,
    }

    if is_dry_run(dry_run):
        record("upload_text_asset", customer_id, payload, dry_run=True)
        return dry_run_response("upload_text_asset", payload)

    try:
        client = get_googleads_client()
        asset_service = client.get_service("AssetService")
        operation = client.get_type("AssetOperation")
        asset = operation.create
        asset.type_ = client.enums.AssetTypeEnum.TEXT
        asset.text_asset.text = text

        response = asset_service.mutate_assets(
            customer_id=customer_id, operations=[operation]
        )
        result = {
            "asset_resource_name": response.results[0].resource_name,
        }
    except GoogleAdsException as ex:
        raise ToolError(_format_errors(ex)) from ex

    record("upload_text_asset", customer_id, payload, result=result)
    return result


@mcp.tool()
def create_asset_group(
    customer_id: str,
    campaign_id: str,
    name: str,
    final_urls: List[str],
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Creates a Performance Max ``AssetGroup`` in ``PAUSED`` state.

    The asset group is attached to an existing Performance Max campaign
    and starts paused to avoid any accidental serving. Asset linking
    (headlines, images, logos, ...) must be performed afterwards via
    :func:`link_asset_to_asset_group`.

    Args:
        customer_id: 10-digit Google Ads customer id.
        campaign_id: Numeric id (string) of the PMax campaign that will
            own this asset group.
        name: Human readable asset group name. Must be non empty.
        final_urls: Non empty list of landing page URLs used as final
            URLs for the asset group.
        dry_run: Preview the mutation without applying it.

    Returns:
        Dict with ``asset_group_resource_name`` and ``status`` = PAUSED.
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty("campaign_id", [campaign_id])
    ensure_non_empty("name", [name])
    if not final_urls:
        raise ToolError("final_urls must contain at least one URL.")
    ensure_non_empty("final_urls", final_urls)

    payload = {
        "customer_id": customer_id,
        "campaign_id": campaign_id,
        "name": name,
        "final_urls": list(final_urls),
        "status": "PAUSED",
    }

    if is_dry_run(dry_run):
        record("create_asset_group", customer_id, payload, dry_run=True)
        return dry_run_response("create_asset_group", payload)

    try:
        client = get_googleads_client()
        asset_group_service = client.get_service("AssetGroupService")
        campaign_service = client.get_service("CampaignService")

        operation = client.get_type("AssetGroupOperation")
        asset_group = operation.create
        asset_group.name = name
        asset_group.campaign = campaign_service.campaign_path(
            customer_id, campaign_id
        )
        asset_group.final_urls.extend(final_urls)
        asset_group.status = client.enums.AssetGroupStatusEnum.PAUSED

        response = asset_group_service.mutate_asset_groups(
            customer_id=customer_id, operations=[operation]
        )
        result = {
            "asset_group_resource_name": response.results[0].resource_name,
            "status": "PAUSED",
        }
    except GoogleAdsException as ex:
        raise ToolError(_format_errors(ex)) from ex

    record("create_asset_group", customer_id, payload, result=result)
    return result


@mcp.tool()
def link_asset_to_asset_group(
    customer_id: str,
    asset_group_resource_name: str,
    asset_resource_name: str,
    field_type: AssetFieldTypeLiteral,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Links an existing ``Asset`` to a Performance Max ``AssetGroup``.

    The ``field_type`` determines how the asset is used in the group
    (HEADLINE, DESCRIPTION, MARKETING_IMAGE, LOGO, ...). Both resources
    must already exist in the same customer account.

    Args:
        customer_id: 10-digit Google Ads customer id.
        asset_group_resource_name: Full resource name of the target
            asset group, e.g. ``customers/123/assetGroups/456``.
        asset_resource_name: Full resource name of the asset to link,
            e.g. ``customers/123/assets/789``.
        field_type: Slot the asset occupies inside the asset group.
        dry_run: Preview the mutation without applying it.

    Returns:
        Dict with ``asset_group_asset_resource_name``.
    """
    customer_id = ensure_customer_id(customer_id)
    ensure_non_empty(
        "asset_group_resource_name", [asset_group_resource_name]
    )
    ensure_non_empty("asset_resource_name", [asset_resource_name])

    try:
        # Validate against the live enum so we fail fast on typos.
        field_type_enum_value = None  # resolved below once we have a client
        _ = AssetFieldTypeLiteral  # keep literal referenced for type tools
        if field_type.upper() not in {
            "HEADLINE",
            "LONG_HEADLINE",
            "DESCRIPTION",
            "BUSINESS_NAME",
            "MARKETING_IMAGE",
            "SQUARE_MARKETING_IMAGE",
            "LOGO",
            "YOUTUBE_VIDEO",
        }:
            raise ToolError(
                f"Unsupported field_type '{field_type}'. Supported: "
                "HEADLINE, LONG_HEADLINE, DESCRIPTION, BUSINESS_NAME, "
                "MARKETING_IMAGE, SQUARE_MARKETING_IMAGE, LOGO, YOUTUBE_VIDEO."
            )
    except AttributeError as ex:
        raise ToolError(f"Invalid field_type '{field_type}': {ex}") from ex

    payload = {
        "customer_id": customer_id,
        "asset_group_resource_name": asset_group_resource_name,
        "asset_resource_name": asset_resource_name,
        "field_type": field_type.upper(),
    }

    if is_dry_run(dry_run):
        record(
            "link_asset_to_asset_group", customer_id, payload, dry_run=True
        )
        return dry_run_response("link_asset_to_asset_group", payload)

    try:
        client = get_googleads_client()
        asset_group_asset_service = client.get_service(
            "AssetGroupAssetService"
        )

        operation = client.get_type("AssetGroupAssetOperation")
        asset_group_asset = operation.create
        asset_group_asset.asset_group = asset_group_resource_name
        asset_group_asset.asset = asset_resource_name
        asset_group_asset.field_type = client.enums.AssetFieldTypeEnum[
            field_type.upper()
        ]
        field_type_enum_value = asset_group_asset.field_type  # noqa: F841

        response = asset_group_asset_service.mutate_asset_group_assets(
            customer_id=customer_id, operations=[operation]
        )
        result = {
            "asset_group_asset_resource_name": response.results[
                0
            ].resource_name,
        }
    except GoogleAdsException as ex:
        raise ToolError(_format_errors(ex)) from ex

    record("link_asset_to_asset_group", customer_id, payload, result=result)
    return result
