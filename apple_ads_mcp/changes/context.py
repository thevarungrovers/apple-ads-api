"""What every change kind is handed: the limits, the counters, and shared reads.

One object rather than a dozen closures, so a kind is a plain class that can be
tested with a fake Apple, and so the engine and the kinds agree on where the
write call, the counters and the per-preview cache live.

THE PER-PREVIEW CACHE. A preview of twenty keyword bids used to fetch the same
campaign and the same campaign keyword report twenty times -- about four calls
per keyword. The engine forks a fresh context for every preview, apply and
revert, so within one tool call each campaign, ad group and 7-day report is read
once; across calls nothing is cached, which is what the staleness check needs.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from apple_ads_mcp import client


class ChangeContext:
    def __init__(
        self,
        state,
        call: Callable[..., Any],
        writes_disabled: Callable[[], str | None],
    ) -> None:
        self.state = state
        # Both are handed in rather than imported so they are resolved through
        # tools_write at CALL time: the write-path tests replace tools_write.call
        # and tools_write.writes_disabled, and a reference captured here at
        # import would keep calling the real ones.
        self._call = call
        self._writes_disabled = writes_disabled
        self._campaigns: dict[int, Any] = {}
        self._ad_groups: dict[int, Any] = {}
        self._reports: dict[tuple[str, Any], tuple[list[dict], str | None]] = {}

    def fork(self) -> "ChangeContext":
        """A context with an empty cache, for one tool call."""
        return ChangeContext(self.state, self._call, self._writes_disabled)

    # -- state -------------------------------------------------------------------

    @property
    def limits(self):
        return self.state.limits

    @property
    def counters(self):
        return self.state.counters

    @property
    def previews(self):
        return self.state.previews

    @property
    def identity(self):
        """Who creations are named after: the state's, read once from .env."""
        from apple_ads_mcp.naming import Identity

        identity = getattr(self.state, "identity", None)
        return identity if identity is not None else Identity.load()

    def name_for_create(self, entity_type: str, requested: str | None):
        """The exact name a creation sends (naming.stamp), or a ToolError.

        Returns the Stamped result: ``.final`` goes into the item, the request
        and the preview; ``.checks()`` into the proposal's checks.
        """
        from mcp.server.mcpserver.exceptions import ToolError

        from apple_ads_mcp import naming

        try:
            return naming.stamp(self.identity, requested, entity_type=entity_type)
        except naming.NameRefused as exc:
            raise ToolError(str(exc)) from exc

    def name_for_rename(self, entity_type: str, entity_id: Any, current: str | None,
                        requested: str | None):
        """The exact name a rename sends (naming.rename): only the agent's own
        entities -- its prefix, or a ledger row showing this server created it."""
        from mcp.server.mcpserver.exceptions import ToolError

        from apple_ads_mcp import ledger, naming

        try:
            return naming.rename(
                self.identity, current, requested, entity_type=entity_type,
                ledger_owned=ledger.created_by_us(entity_type, entity_id, self.identity),
            )
        except naming.NameRefused as exc:
            raise ToolError(str(exc)) from exc

    @property
    def daily(self):
        """Today's usage, read from the ledger on every use -- never cached."""
        from apple_ads_mcp.guardrails import DailyUsage

        return DailyUsage(self.limits)

    # -- calls -------------------------------------------------------------------

    def call(self, method: str, /, **kwargs: Any) -> Any:
        return self._call(method, **kwargs)

    def writes_disabled(self) -> str | None:
        return self._writes_disabled()

    # -- cached reads ------------------------------------------------------------

    def campaign(self, campaign_id: Any) -> Any:
        if campaign_id in (None, ""):
            return None
        key = int(campaign_id)
        if key not in self._campaigns:
            self._campaigns[key] = client.get_campaign(key)
        return self._campaigns[key]

    def ad_group(self, ad_group_id: Any) -> Any:
        if ad_group_id in (None, ""):
            return None
        key = int(ad_group_id)
        if key not in self._ad_groups:
            self._ad_groups[key] = client.get_ad_group(key)
        return self._ad_groups[key]

    def remember_ad_group(self, ad_group: Any) -> None:
        if ad_group is not None and getattr(ad_group, "id", None) is not None:
            self._ad_groups[int(ad_group.id)] = ad_group

    def remember_campaign(self, campaign: Any) -> None:
        if campaign is not None and getattr(campaign, "id", None) is not None:
            self._campaigns[int(campaign.id)] = campaign

    def metrics_7d(
        self, method: str, id_key: str, entity_id: Any, campaign_id: Any = None
    ) -> tuple[Decimal, int, int, int, str | None]:
        """(spend, taps, impressions, installs, error) for one entity, 7 days.

        The report is fetched once per (report, campaign) in this context.
        """
        from apple_ads_mcp.tools_read import pick_metrics, report_rows_7d

        scope = None if id_key == "campaignId" else campaign_id
        key = (method, scope)
        if key not in self._reports:
            self._reports[key] = report_rows_7d(method, id_key, scope)
        rows, error = self._reports[key]
        if error:
            return Decimal("0"), 0, 0, 0, error
        return pick_metrics(rows, entity_id, id_key)
