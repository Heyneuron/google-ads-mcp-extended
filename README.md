# google-ads-mcp-extended

> A fork of the official [`googleads/google-ads-mcp`](https://github.com/googleads/google-ads-mcp)
> with write operations, safety guardrails, and an audit log - maintained by
> [HeyNeuron](https://heyneuron.com) (BIG GROWTH Sp. z o.o.).

The upstream Google Ads MCP server is read-only: it exposes `search` (GAQL) and
a handful of metadata helpers, which is great for audits and reporting but not
enough to actually *manage* accounts from an AI agent. This fork adds the write
operations that agencies need, with every mutation wrapped in:

1. **Safe defaults** - newly created campaigns, ads, and asset groups are
   always `PAUSED`. You enable them explicitly.
2. **Hard caps** - budget and bid ceilings enforced in-process, refusing
   mutations that would exceed configured limits.
3. **Dry-run** - every write tool accepts `dry_run=True` to preview the
   mutation without calling the Google Ads API.
4. **Append-only audit log** - every mutation (real or dry-run) is written
   to `~/.google-ads-mcp/audit.log.jsonl`.

## What you get

All upstream tools (`search`, `get_resource_metadata`, `list_accessible_customers`,
plus the discovery / metrics / segments / release-notes resources) continue to
work unchanged. On top of them, this fork adds:

### Campaign management
- `create_campaign` - create a Search / Display / Performance Max campaign
  (always starts PAUSED, always with a dedicated daily budget).
- `update_campaign_status` - enable / pause / remove a campaign.
- `update_campaign_budget` - change the daily budget of an existing campaign.

### Keyword management
- `add_keywords` - add positive keywords to an ad group.
- `add_negative_keywords` - add negative keywords at ad-group or campaign
  level. Defaults to EXACT match to avoid accidentally over-blocking.
- `update_keyword_bids` - change CPC bids on specific keywords.
- `pause_keywords` / `remove_keywords` - batch pause or remove.

### Ad + extension management
- `create_ad_group` - create a new ad group inside a campaign.
- `create_responsive_search_ad` - create an RSA with 3-15 headlines and
  2-4 descriptions (always PAUSED).
- `create_sitelink_asset` - create a reusable sitelink asset.
- `link_asset_to_campaign` - attach an existing asset (sitelink / callout /
  structured snippet) to a campaign.

### Assets + Performance Max
- `upload_image_asset` - upload an image asset from a URL or local file.
- `upload_text_asset` - create a text asset for use in Performance Max.
- `create_asset_group` - create a Performance Max asset group (PAUSED).
- `link_asset_to_asset_group` - attach an asset to an asset group with a
  specific field type (HEADLINE, LOGO, MARKETING_IMAGE, etc.).

## Safety model

Every mutation tool in this fork follows the same pattern:

1. Validate the customer id (10 digits).
2. Run all hard-cap guards (budget, bid).
3. If `dry_run=True` (or the `GOOGLE_ADS_MCP_DEFAULT_DRY_RUN` env var is set),
   log the intended mutation and return the preview without calling the API.
4. Otherwise execute the mutation, capture the result, and append an audit
   entry.

### Configurable guards

| Env var | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_ADS_MCP_MAX_CAMPAIGN_BUDGET_MICROS` | `500_000_000` (500 units) | Hard cap on any `create_campaign` / `update_campaign_budget` call |
| `GOOGLE_ADS_MCP_MAX_BID_MICROS` | `50_000_000` (50 units) | Hard cap on keyword CPC bids |
| `GOOGLE_ADS_MCP_GUARDS_DISABLED` | unset | Set to `1` to disable all hard caps (not recommended) |
| `GOOGLE_ADS_MCP_DEFAULT_DRY_RUN` | unset | Set to `1` to make every mutation default to dry-run |
| `GOOGLE_ADS_MCP_AUDIT_LOG` | `~/.google-ads-mcp/audit.log.jsonl` | Path to the audit log file |

All monetary amounts are in *micros* - i.e. 1,000,000 micros = 1 unit of your
account currency. `500_000_000` micros is 500 PLN on a PLN account, 500 USD on
a USD account, and so on.

## Installation

```bash
# Install from source
git clone https://github.com/Heyneuron/google-ads-mcp-extended.git
cd google-ads-mcp-extended
uv sync
```

## Configuration

This server requires the same configuration as upstream:

1. A Google Ads **developer token** (Explorer or Basic access is fine).
2. **OAuth2 user credentials** (Google Ads API does not support service
   accounts). The easiest path is `gcloud auth application-default login`.
3. A **Manager Account (MCC)** with the client accounts you want to manage
   linked beneath it.

Set the following environment variables:

```bash
export GOOGLE_ADS_DEVELOPER_TOKEN="your-dev-token"
export GOOGLE_ADS_LOGIN_CUSTOMER_ID="1234567890"   # your MCC id, no dashes
```

Optional safety overrides:

```bash
export GOOGLE_ADS_MCP_MAX_CAMPAIGN_BUDGET_MICROS="200000000"  # 200 units
export GOOGLE_ADS_MCP_DEFAULT_DRY_RUN="1"                     # always preview
```

## Claude Code / MCP client config

Add an entry to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "google-ads": {
      "command": "uv",
      "args": ["--directory", "/path/to/google-ads-mcp-extended", "run", "google-ads-mcp"],
      "env": {
        "GOOGLE_ADS_DEVELOPER_TOKEN": "your-dev-token",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "1234567890"
      }
    }
  }
}
```

## Credits

This project is a fork of the official
[`googleads/google-ads-mcp`](https://github.com/googleads/google-ads-mcp) by
Google LLC. All upstream tools and resources are preserved; the HeyNeuron
additions live under `ads_mcp/heyneuron/`.

## License

Apache License 2.0. See [LICENSE](LICENSE). Upstream code (c) Google LLC;
extensions (c) 2026 BIG GROWTH Sp. z o.o. (HeyNeuron).

## Disclaimer

This is an independent project. It is not an official Google product and is
not endorsed by Google LLC. Use at your own risk - the safety guardrails are
a best-effort second line of defence, not a substitute for reviewing what an
AI agent is about to do to a client's advertising account.
