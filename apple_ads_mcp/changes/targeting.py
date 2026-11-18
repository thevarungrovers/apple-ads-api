"""3c: targeting -- Search Match, demographics, devices, daypart, locations,
app downloaders, and a campaign's countries.

Every targeting dimension in the Platform API is `{include: [...], exclude:
[...]}` of strings, so one engine serves all of them: a kind names the
dimensions it owns, reads them, writes them, and the before/after in the ledger
is the canonical JSON of exactly those dimensions.

THE PROJECTION is the honest one for targeting: WIDENING (a dimension
unrestricted, an include list grown, an exclude list shrunk, an age bound
loosened, Search Match switched on) can let the campaign spend up to its unused
daily budget, and is counted like an enable. NARROWING projects 0.

An EMPTY include list is written to mean "no restriction". That reading has not
been verified against the live API; the README's live-check list names it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated, Any

from apple_ads_platform import (
    AdGroupTargetingUpdate,
    AdGroupUpdate,
    CampaignTargetingUpdate,
    CampaignUpdate,
    TargetingDataUpdate,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from apple_ads_mcp import client
from apple_ads_mcp.changes.base import (
    APPLY,
    APPLY_DESTRUCTIVE,
    ChangeKind,
    Outcome,
    Proposal,
    item as make_item,
)
from apple_ads_mcp.changes.common import (
    bound_check,
    campaign_headroom,
    serving_warning,
)
from apple_ads_mcp.client import context_header, entity_path, enum_value, unwrap
from apple_ads_mcp.guardrails import check_campaign_in_scope, check_known_status

DAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _dimension(targeting: Any, attr: str) -> dict[str, list[str]]:
    data = getattr(targeting, attr, None) if targeting is not None else None
    include = sorted(str(v) for v in (getattr(data, "include", None) or []))
    exclude = sorted(str(v) for v in (getattr(data, "exclude", None) or []))
    return {"include": include, "exclude": exclude}


def read_state(entity: Any, attrs: tuple[str, ...]) -> dict[str, dict[str, list[str]]]:
    targeting = getattr(entity, "targeting", None)
    return {attr: _dimension(targeting, attr) for attr in attrs}


def encode(state: dict[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def decode(text: str) -> dict[str, Any]:
    return json.loads(text)


def _num(values: list[str]) -> int | None:
    try:
        return int(values[0]) if values else None
    except (TypeError, ValueError):
        return None


def widens(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """True when ``after`` can match traffic ``before`` could not."""
    for attr, b in before.items():
        a = after.get(attr, b)
        if attr == "min_age":
            lo_b, lo_a = _num(b["include"]), _num(a["include"])
            if lo_b is not None and (lo_a is None or lo_a < lo_b):
                return True
            continue
        if attr == "max_age":
            hi_b, hi_a = _num(b["include"]), _num(a["include"])
            if hi_b is not None and (hi_a is None or hi_a > hi_b):
                return True
            continue
        if b["include"] and (not a["include"] or not set(a["include"]) <= set(b["include"])):
            return True
        if set(b["exclude"]) - set(a["exclude"]):
            return True
    return False


def describe(state: dict[str, Any]) -> str:
    """Readable: `device_class: IPHONE | min_age: any`."""
    parts = []
    for attr, value in sorted(state.items()):
        text = ", ".join(value["include"]) if value["include"] else "any"
        if value["exclude"]:
            text += f" (except {', '.join(value['exclude'])})"
        parts.append(f"{attr}: {text}")
    return " | ".join(parts)


def daypart_grid(hours: list[str]) -> str:
    """The 168 hour indices as a weekday x hour grid: '#' serves, '.' does not."""
    chosen = {int(h) for h in hours}
    if not chosen:
        return "every hour of every day"
    lines = ["     " + "".join(str(h % 10) for h in range(24))]
    for day, name in enumerate(DAYS):
        lines.append(f"{name}  " + "".join("#" if day * 24 + h in chosen else "." for h in range(24)))
    return "\n".join(lines)


class _TargetingKind(ChangeKind):
    """Some dimensions of an ad group's (or a campaign's) targeting."""

    entity_type = "ad_group"
    field_name = ""
    attrs: tuple[str, ...] = ()
    apply_annotations = APPLY

    @property
    def ledger_keys(self):  # type: ignore[override]
        return ((self.entity_type, self.field_name),)

    def fetch(self, entity_id):
        return client.get_ad_group(int(entity_id))

    def update(self, state: dict[str, Any]):
        dims = {
            attr: TargetingDataUpdate(include=value["include"], exclude=value["exclude"] or None)
            for attr, value in state.items()
        }
        return "adgroups_id_put", "ad_group_update", AdGroupUpdate(targeting=AdGroupTargetingUpdate(**dims))

    def bound(self, ctx, campaign):
        return campaign_headroom(ctx, campaign)

    def build(self, ctx, entity_id: int, wanted: dict[str, dict[str, list[str]] | None], note: str = "",
              extra_warnings: list[str] | None = None) -> Proposal:
        if self.entity_type == "campaign":
            entity = ctx.campaign(entity_id)
            campaign, parent = entity, None
            campaign_id = entity_id
        else:
            entity = ctx.ad_group(entity_id)
            campaign_id = entity.campaign_id
            campaign, parent = ctx.campaign(campaign_id), entity
        before_state = read_state(entity, self.attrs)
        after_state = {
            attr: (wanted[attr] if wanted.get(attr) is not None else before_state[attr])
            for attr in self.attrs
        }
        for value in after_state.values():
            value["include"] = sorted(dict.fromkeys(str(v) for v in value["include"]))
            value["exclude"] = sorted(dict.fromkeys(str(v) for v in value["exclude"]))
        if after_state == before_state:
            raise ToolError(f"that is already the {self.field_name} targeting; nothing to change.")
        wider = widens(before_state, after_state)
        checks = [
            check_campaign_in_scope(ctx.limits, campaign_id),
            check_known_status(f"{self.entity_type}_status_known", enum_value(entity.status)),
        ]
        projected = Decimal("0")
        if wider:
            projected, bound = bound_check(*self.bound(ctx, campaign))
            checks += bound
        warnings = serving_warning(enum_value(entity.display_status), self.entity_type.replace("_", " "))
        warnings.append(
            "This WIDENS who can see the ads, so it can add spend up to the campaign's "
            "unused daily budget." if wider else "This only NARROWS targeting; it cannot add spend."
        )
        warnings += extra_warnings or []
        path = entity_path(campaign, parent)
        before, after = encode(before_state), encode(after_state)
        return Proposal(
            items=[make_item(
                entity_type=self.entity_type, entity_id=entity_id, entity_name=entity.name,
                path=path, field=self.field_name, before=before, after=after,
                campaign_id=campaign_id, projected=projected,
            )],
            checks=checks, projected=projected, is_enable=wider, warnings=warnings,
            note=note or "Reversible with revert_change, which writes the previous targeting back.",
            entity_type=self.entity_type, entity_id=str(entity_id), entity_name=entity.name,
            path=path, field_name=self.field_name, before=describe(before_state),
            after=describe(after_state), campaign=campaign,
            entity_serving=enum_value(entity.display_status),
        )

    def current(self, ctx, item):
        return encode(read_state(self.fetch(item["entity_id"]), self.attrs))

    def request(self, ctx, items):
        method, arg, body = self.update(decode(items[0]["after"]))
        return method, {"id": str(items[0]["entity_id"]), "x_ap_context": context_header(), arg: body}

    def observe(self, ctx, response, items):
        updated = unwrap(response, f"{self.entity_type} update")
        observed = encode(read_state(updated, self.attrs)) if updated else None
        return [Outcome(observed == items[0]["after"], observed)]

    def inverse(self, ctx, entry):
        return self, make_item(
            entity_type=self.entity_type, entity_id=entry["entity_id"],
            entity_name=entry.get("entity_name"), path=str(entry.get("path")),
            field=self.field_name, before=str(entry["after"]), after=str(entry["before"]),
            campaign_id=entry.get("campaign_id"),
        )

    def revert_projection(self, ctx, entry, item):
        if not widens(decode(item["before"]), decode(item["after"])):
            return Decimal("0"), False
        bound, reason = self.bound(ctx, ctx.campaign(item.get("campaign_id")))
        if bound is None:
            raise ValueError(f"cannot revert: {reason}.")
        return bound, True


def _include(values: list[str] | None, upper: bool = True) -> dict[str, list[str]] | None:
    if values is None:
        return None
    return {"include": [str(v).strip().upper() if upper else str(v).strip() for v in values if str(v).strip()],
            "exclude": []}


class AdGroupDemographics(_TargetingKind):
    name = "ad_group_demographics"
    preview_title = "Preview an ad group's age and gender targeting change (no write)"
    apply_title = "Apply an ad group age/gender targeting change (CHANGES WHO SEES THE ADS)"
    field_name = "targeting.demographics"
    attrs = ("min_age", "max_age", "gender")

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        min_age: Annotated[int | None, Field(description="Minimum age; 0 removes the minimum; omit to keep", ge=0, le=99)] = None,
        max_age: Annotated[int | None, Field(description="Maximum age; 0 removes the maximum; omit to keep", ge=0, le=99)] = None,
        genders: Annotated[list[str] | None, Field(description='["M"], ["F"], or [] for everyone; omit to keep')] = None,
    ) -> Proposal:
        """Show what changing an ad group's age range or gender would do. Writes nothing.

        Loosening either is counted like an enable (the campaign's unused budget);
        tightening projects 0. Apple only applies age and gender to users who
        have shared them, so this narrows less than it looks.
        """
        wanted: dict[str, Any] = {}
        if min_age is not None:
            wanted["min_age"] = {"include": [str(min_age)] if min_age else [], "exclude": []}
        if max_age is not None:
            wanted["max_age"] = {"include": [str(max_age)] if max_age else [], "exclude": []}
        if genders is not None:
            for g in genders:
                if str(g).upper() not in ("M", "F"):
                    raise ToolError(f"INVALID_REQUEST: gender {g!r} must be M or F")
            wanted["gender"] = _include(genders)
        if not wanted:
            raise ToolError("give min_age, max_age or genders.")
        return self.build(ctx, ad_group_id, wanted)


class AdGroupDevices(_TargetingKind):
    name = "ad_group_devices"
    preview_title = "Preview an ad group's device targeting change (no write)"
    apply_title = "Apply an ad group device targeting change (CHANGES WHO SEES THE ADS)"
    field_name = "targeting.devices"
    attrs = ("device_class",)

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        devices: Annotated[list[str], Field(description='["IPHONE"], ["IPAD"] or both')],
    ) -> Proposal:
        """Show what changing which devices an ad group serves on would do. Writes nothing."""
        cleaned = [str(d).strip().upper() for d in devices]
        if not cleaned or any(d not in ("IPHONE", "IPAD") for d in cleaned):
            raise ToolError('INVALID_REQUEST: devices must be one or both of "IPHONE", "IPAD"')
        return self.build(ctx, ad_group_id, {"device_class": _include(cleaned)})


class AdGroupDaypart(_TargetingKind):
    name = "ad_group_daypart"
    preview_title = "Preview an ad group's day-and-hour schedule change (no write)"
    apply_title = "Apply an ad group daypart change (CHANGES WHEN THE ADS SERVE)"
    field_name = "targeting.daypart"
    attrs = ("daypart",)

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        hours: Annotated[
            list[int],
            Field(description="Hour indices 0-167 to serve in: day*24 + hour, Sunday=0, "
                  "in the account's timezone. [] serves every hour"),
        ],
    ) -> Proposal:
        """Show what changing the hours an ad group serves would do. Writes nothing.

        The preview's warnings include the result as a weekday x hour grid.
        """
        if any(h < 0 or h > 167 for h in hours):
            raise ToolError("INVALID_REQUEST: hours must be between 0 and 167")
        wanted = {"include": [str(h) for h in sorted(set(hours))], "exclude": []}
        proposal = self.build(ctx, ad_group_id, {"daypart": wanted})
        proposal.warnings.append("Serves in (# = on):\n" + daypart_grid(wanted["include"]))
        return proposal


class AdGroupLocations(_TargetingKind):
    name = "ad_group_locations"
    preview_title = "Preview an ad group's location targeting change (no write)"
    apply_title = "Apply an ad group location targeting change (CHANGES WHERE THE ADS SERVE)"
    field_name = "targeting.locations"
    attrs = ("admin_area", "locality", "postal_code", "location_group")

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        admin_areas: Annotated[list[str] | None, Field(description="AdminArea ids from search_geos; [] clears; omit to keep")] = None,
        localities: Annotated[list[str] | None, Field(description="Locality ids from search_geos; [] clears; omit to keep")] = None,
        postal_codes: Annotated[list[str] | None, Field(description="PostalCode ids from search_geos; [] clears; omit to keep")] = None,
        location_group_ids: Annotated[list[str] | None, Field(description="Ids from list_location_groups; [] clears; omit to keep")] = None,
    ) -> Proposal:
        """Show what changing an ad group's locations would do. Writes nothing.

        Ids come from search_geos (and list_location_groups); get_geos names
        them. Clearing a list removes that restriction, which WIDENS delivery.
        """
        wanted = {
            "admin_area": _include(admin_areas, upper=False),
            "locality": _include(localities, upper=False),
            "postal_code": _include(postal_codes, upper=False),
            "location_group": _include(location_group_ids, upper=False),
        }
        if all(v is None for v in wanted.values()):
            raise ToolError("give at least one of admin_areas, localities, postal_codes, location_group_ids.")
        return self.build(ctx, ad_group_id, wanted, note=(
            "A radius around a point is not covered here (its format is unverified); "
            "set it in the Apple Ads UI."
        ))


class AdGroupAppDownloaders(_TargetingKind):
    name = "ad_group_app_downloaders"
    preview_title = "Preview an ad group's app-downloader (customer type) targeting change (no write)"
    apply_title = "Apply an ad group app-downloader targeting change (CHANGES WHO SEES THE ADS)"
    field_name = "targeting.app_downloaders"
    attrs = ("app_downloader",)

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        include_app_ids: Annotated[list[str], Field(description="Show only to people who downloaded these apps; [] for anyone")] = [],  # noqa: B006
        exclude_app_ids: Annotated[list[str], Field(description="Never show to people who downloaded these (e.g. your own app, for new users only)")] = [],  # noqa: B006
    ) -> Proposal:
        """Show what changing customer-type targeting would do. Writes nothing.

        Excluding your own app's id reaches new users only; including it reaches
        existing users only; neither reaches everyone.
        """
        wanted = {"include": [str(i).strip() for i in include_app_ids if str(i).strip()],
                  "exclude": [str(i).strip() for i in exclude_app_ids if str(i).strip()]}
        return self.build(ctx, ad_group_id, {"app_downloader": wanted})

    def update(self, state):
        value = state["app_downloader"]
        body = AdGroupUpdate(targeting=AdGroupTargetingUpdate(app_downloader=TargetingDataUpdate(
            include=value["include"], exclude=value["exclude"])))
        return "adgroups_id_put", "ad_group_update", body


class CampaignCountries(_TargetingKind):
    name = "campaign_countries"
    preview_title = "Preview a campaign's countries or regions change (no write)"
    apply_title = "Apply a campaign country change (CHANGES WHERE A WHOLE CAMPAIGN SERVES)"
    apply_annotations = APPLY_DESTRUCTIVE
    entity_type = "campaign"
    field_name = "targeting.countries"
    attrs = ("country_or_region",)

    def fetch(self, entity_id):
        return client.get_campaign(int(entity_id))

    def update(self, state):
        value = state["country_or_region"]
        body = CampaignUpdate(targeting=CampaignTargetingUpdate(country_or_region=TargetingDataUpdate(
            include=value["include"], exclude=value["exclude"] or None)))
        return "campaigns_id_put", "campaign_update", body

    def bound(self, ctx, campaign):
        return campaign_headroom(ctx, campaign)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[int, Field(description="Apple campaign id")],
        countries: Annotated[list[str], Field(description='Two-letter codes, e.g. ["US", "CA"]')],
    ) -> Proposal:
        """Show what changing a campaign's countries or regions would do. Writes nothing.

        Where Apple allows it to change at all. Adding a country WIDENS delivery
        (counted like an enable). The preview says what a new country implies:
        its storefront and currency, and any regulation questions it carries.
        """
        cleaned = [str(c).strip().upper() for c in countries if str(c).strip()]
        if not cleaned or any(len(c) != 2 for c in cleaned):
            raise ToolError('INVALID_REQUEST: countries must be two-letter codes, e.g. ["US"]')
        campaign = ctx.campaign(campaign_id)
        current = set(read_state(campaign, self.attrs)["country_or_region"]["include"])
        added = sorted(set(cleaned) - current)
        extra = []
        if added:
            extra.append(
                f"Adding {', '.join(added)}: each is a separate App Store storefront. "
                "Spend there is still billed in the account currency, but check that "
                "the app is available there (app_eligibility) and that the keywords "
                "make sense in its language."
            )
            if {"FR"} & set(added) or {"CN"} & set(added):
                extra.append(
                    "France and mainland China carry regulation questions (Sapin law, "
                    "CAC) that Apple may require answered on the campaign; this tool "
                    "does not answer them."
                )
        return self.build(ctx, campaign_id, {"country_or_region": _include(cleaned)},
                          extra_warnings=extra)


class AdGroupSearchMatch(ChangeKind):
    name = "ad_group_search_match"
    preview_title = "Preview switching Search Match on or off for an ad group (no write)"
    apply_title = "Apply a Search Match change (CHANGES WHICH SEARCHES MATCH)"
    apply_annotations = APPLY
    ledger_keys = (("ad_group", "search_match"),)

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        enabled: Annotated[bool, Field(description="True to switch Search Match on, False for off")],
    ) -> Proposal:
        """Show what switching Search Match would do. Writes nothing.

        Search Match lets Apple match searches from your App Store listing, with
        no keyword of yours involved. Switching it ON widens delivery (counted
        like an enable, at the campaign's unused budget); OFF projects 0.
        """
        ad_group = ctx.ad_group(ad_group_id)
        campaign = ctx.campaign(ad_group.campaign_id)
        before = "on" if ad_group.automated_keywords_opt_in else "off"
        after = "on" if enabled else "off"
        if before == after:
            raise ToolError(f"Search Match is already {after} for ad group {ad_group_id}.")
        checks = [
            check_campaign_in_scope(ctx.limits, ad_group.campaign_id),
            check_known_status("ad_group_status_known", enum_value(ad_group.status)),
        ]
        projected = Decimal("0")
        if enabled:
            projected, bound = bound_check(*campaign_headroom(ctx, campaign))
            checks += bound
        warnings = serving_warning(enum_value(ad_group.display_status), "ad group")
        if enabled:
            warnings.append(
                "Apple will match searches it picks from your listing, priced off this "
                "ad group's default bid. Watch search_term_report for what it matches."
            )
        path = entity_path(campaign, ad_group)
        return Proposal(
            items=[make_item(
                entity_type="ad_group", entity_id=ad_group_id, entity_name=ad_group.name, path=path,
                field="search_match", before=before, after=after,
                campaign_id=ad_group.campaign_id, projected=projected,
            )],
            checks=checks, projected=projected, is_enable=enabled, warnings=warnings,
            entity_type="ad_group", entity_id=str(ad_group_id), entity_name=ad_group.name,
            path=path, field_name="search_match", before=before, after=after, campaign=campaign,
            entity_serving=enum_value(ad_group.display_status),
        )

    def current(self, ctx, item):
        return "on" if client.get_ad_group(int(item["entity_id"])).automated_keywords_opt_in else "off"

    def request(self, ctx, items):
        return "adgroups_id_put", {
            "id": str(items[0]["entity_id"]),
            "x_ap_context": context_header(),
            "ad_group_update": AdGroupUpdate(automated_keywords_opt_in=items[0]["after"] == "on"),
        }

    def observe(self, ctx, response, items):
        updated = unwrap(response, "ad group update")
        observed = ("on" if updated.automated_keywords_opt_in else "off") if updated else None
        return [Outcome(observed == items[0]["after"], observed)]

    def inverse(self, ctx, entry):
        return self, make_item(
            entity_type="ad_group", entity_id=entry["entity_id"], entity_name=entry.get("entity_name"),
            path=str(entry.get("path")), field="search_match", before=str(entry["after"]),
            after=str(entry["before"]), campaign_id=entry.get("campaign_id"),
        )

    def revert_projection(self, ctx, entry, item):
        if item["after"] != "on":
            return Decimal("0"), False
        bound, reason = campaign_headroom(ctx, ctx.campaign(item.get("campaign_id")))
        if bound is None:
            raise ValueError(f"cannot revert: {reason}.")
        return bound, True
