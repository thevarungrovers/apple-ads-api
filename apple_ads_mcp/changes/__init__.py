"""Change kinds: every write the server can make, one class per sort of change.

See base.py for what a kind is and engine.py for the sequence every kind runs
through. `all_kinds()` is the registry, in the order the tools are published.
"""

from __future__ import annotations


def all_kinds() -> list:
    from apple_ads_mcp.changes.adgroups import AdGroupDefaultBid, AdGroupStatusKind
    from apple_ads_mcp.changes.bidding import (
        AdGroupBidStrategy,
        AdGroupCpaGoal,
        CampaignBidStrategy,
        CampaignSchedule,
    )
    from apple_ads_mcp.changes.campaigns import CampaignDailyBudget, CampaignStatusKind
    from apple_ads_mcp.changes.keywords import (
        AddKeywords,
        KeywordBid,
        KeywordBidsBulk,
        KeywordMatchTypeKind,
        KeywordStatusKind,
        NegativeKeywordsAdd,
        NegativeKeywordStatusBulk,
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
        # 3a: keywords and search terms
        AddKeywords(),
        KeywordMatchTypeKind(),
        NegativeKeywordStatusBulk(),
        # 3b: bidding and goals
        AdGroupBidStrategy(),
        AdGroupCpaGoal(),
        CampaignBidStrategy(),
        CampaignSchedule(),
    ]
