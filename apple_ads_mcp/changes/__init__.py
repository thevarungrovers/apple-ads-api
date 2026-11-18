"""Change kinds: every write the server can make, one class per sort of change.

See base.py for what a kind is and engine.py for the sequence every kind runs
through. `all_kinds()` is the registry, in the order the tools are published.
"""

from __future__ import annotations


def all_kinds() -> list:
    from apple_ads_mcp.changes.adgroups import AdGroupDefaultBid, AdGroupStatusKind
    from apple_ads_mcp.changes.campaigns import CampaignDailyBudget, CampaignStatusKind
    from apple_ads_mcp.changes.keywords import (
        KeywordBid,
        KeywordBidsBulk,
        KeywordStatusKind,
        NegativeKeywordsAdd,
        NegativeKeywordStatusKind,
    )

    return [
        KeywordBid(),
        KeywordBidsBulk(),
        KeywordStatusKind(),
        AdGroupStatusKind(),
        AdGroupDefaultBid(),
        CampaignStatusKind(),
        CampaignDailyBudget(),
        NegativeKeywordsAdd(),
        NegativeKeywordStatusKind(),
    ]
