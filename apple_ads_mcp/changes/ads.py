"""3d: ads and custom product page creatives.

A CREATIVE points at an app's product page (the default one, or a custom
product page made in App Store Connect). It has no status and cannot serve on
its own. An AD attaches a creative to an ad group; it is created PAUSED, and
enabling it is a separate ad_status change, projected like any enable.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from apple_ads_platform import (
    AdCreate,
    AdStatus,
    AdUpdate,
    CreativeCreate,
    CreativeType,
    DestinationCreate,
    DestinationParameter,
    DestinationType,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from apple_ads_mcp import client
from apple_ads_mcp.changes.base import (
    APPLY,
    APPLY_DESTRUCTIVE,
    CREATED,
    NOTHING,
    ChangeKind,
    Outcome,
    Proposal,
    item as make_item,
)
from apple_ads_mcp.changes.common import (
    SETTABLE_STATUSES,
    StatusKind,
    bound_check,
    metrics_block,
    metrics_warning,
    name_clash_warning,
    parse_choice,
    serving_warning,
)
from apple_ads_mcp.client import context_header, entity_path, enum_value, landed, unwrap
from apple_ads_mcp.guardrails import Check, check_campaign_in_scope, check_known_status
from apple_ads_mcp.previews import project_pause


def _created(response, what: str, after: str, status_of=None) -> list[Outcome]:
    entity = unwrap(response, what)
    new_id = getattr(entity, "id", None)
    observed = enum_value(getattr(entity, "status", None)) if status_of else ("present" if new_id else None)
    return [Outcome(
        new_id is not None and landed(observed, after),
        observed,
        created_id=str(new_id) if new_id is not None else None,
    )]


class AdStatusKind(StatusKind):
    name = "ad_status"
    preview_title = "Preview pausing or enabling an ad (no write)"
    apply_title = "Apply an ad pause or enable (CHANGES LIVE DELIVERY)"
    apply_annotations = APPLY
    entity_type = "ad"
    label = "ad"
    get_method = "ads_id_get"
    put_method = "ads_id_put"
    update_arg = "ad_update"
    ledger_keys = (("ad", "status"),)

    def update_body(self, status):
        return AdUpdate(status=AdStatus(status))

    def propose(
        self,
        ctx,
        ad_id: Annotated[int, Field(description="Apple ad id")],
        status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> Proposal:
        """Show what pausing or enabling one ad would do. Writes nothing.

        Enabling is projected at the campaign's unused daily budget and counted
        against the daily total, like any enable.
        """
        target = parse_choice(status, SETTABLE_STATUSES)
        ad = self.fetch(ad_id)
        before = enum_value(ad.status)
        if before == target:
            raise ToolError(f"ad {ad_id} is already {target}; nothing to change.")
        ad_group = ctx.ad_group(ad.ad_group_id)
        campaign = ctx.campaign(ad.campaign_id)
        spend, taps, impressions, installs, metrics_error = ctx.metrics_7d(
            "apps_ad_reports", "adId", ad_id, ad.campaign_id
        )
        bound = []
        if target == "PAUSED":
            projected = project_pause(spend)
        else:
            projected, bound = bound_check(*self.enable_bound(ctx, ad.campaign_id))
        path = entity_path(campaign, ad_group) + f" > Ad {ad.name!r}"
        return Proposal(
            items=[make_item(
                entity_type="ad", entity_id=ad_id, entity_name=ad.name, path=path,
                field="status", before=before, after=target, campaign_id=ad.campaign_id,
                projected=projected,
            )],
            checks=[
                check_campaign_in_scope(ctx.limits, ad.campaign_id),
                check_known_status("ad_status_known", before),
                *bound,
            ],
            projected=projected, is_enable=target == "ENABLED",
            warnings=serving_warning(enum_value(ad.display_status), "ad") + metrics_warning(metrics_error),
            note="Reversible: apply the opposite status to undo, or use revert_change.",
            entity_type="ad", entity_id=str(ad_id), entity_name=ad.name, path=path,
            field_name="status", before=before or "unknown", after=target, campaign=campaign,
            entity_serving=enum_value(ad.display_status),
            metrics=metrics_block(spend, taps, impressions, installs),
        )


class CreateCreative(ChangeKind):
    name = "create_creative"
    creates_named = ("creative",)
    preview_title = "Preview creating a creative for a product page (no write)"
    apply_title = "Create a creative (cannot serve until an ad attaches it; cannot be deleted here)"
    apply_annotations = APPLY_DESTRUCTIVE
    cost = 3
    ledger_keys = (("creative", CREATED),)
    not_revertible = (
        "a creative has no status and is never deleted from here. It cannot serve "
        "on its own: pause the ads that use it (ad_status) to stop it."
    )
    success_note = (
        "Created. It serves nothing until create_ad attaches it to an ad group, and "
        "that ad is created PAUSED."
    )

    def propose(
        self,
        ctx,
        name: Annotated[str, Field(description="Creative name")],
        adam_id: Annotated[str, Field(description="The app's Apple id (adamId)")],
        product_page_id: Annotated[
            str | None,
            Field(description="Custom product page id from list_product_pages; omit for the default page"),
        ] = None,
    ) -> Proposal:
        """Show what creating a creative would do. Writes nothing.

        A CUSTOM_PRODUCT_PAGE creative when product_page_id is given, else a
        DEFAULT_PRODUCT_PAGE one. It has no status and cannot serve until an ad
        attaches it, so the projection is 0. Costs 3 applies.

        Pass the BASE name only: the configured prefix (<OWNER>-<agent>-) is
        added, and the preview shows the exact name that will be sent.
        """
        stamped = ctx.name_for_create("creative", name)
        name = stamped.final
        creative_type = "CUSTOM_PRODUCT_PAGE" if product_page_id else "DEFAULT_PRODUCT_PAGE"
        checks = stamped.checks()
        warnings = [w for w in (stamped.warning(),) if w]
        warnings += ["NOT idempotent: applying this twice creates two creatives."]
        warnings += name_clash_warning("creative", name)
        if product_page_id:
            try:
                page = unwrap(client.call("get_product_page_by_id", x_ap_context=context_header(),
                                          product_page_id=str(product_page_id)), "product page")
                state = enum_value(getattr(page, "state", None))
                checks.append(Check(
                    "product_page_exists", page is not None,
                    f"product page {product_page_id} is {state or 'present'}",
                ))
                if str(getattr(page, "adam_id", adam_id)) != str(adam_id):
                    checks.append(Check("product_page_app", False,
                                        f"product page {product_page_id} belongs to another app"))
            except ToolError as exc:
                checks.append(Check("product_page_exists", False, str(exc)[:200]))
        path = f"Creative {name!r}"
        after = "present"
        return Proposal(
            items=[make_item(
                entity_type="creative", entity_id=None, entity_name=name, path=path,
                field=CREATED, before=NOTHING, after=after, campaign_id=None,
                adam_id=str(adam_id), product_page_id=product_page_id, creative_type=creative_type,
                **stamped.item_fields(ctx.identity),
            )],
            checks=checks, projected=Decimal("0"), warnings=warnings,
            created_names=[("creative", name)], naming=stamped.block(),
            note="A creative cannot be deleted from here; it is inert until an ad uses it.",
            entity_type="creative", entity_id="(new)", entity_name=name, path=path,
            field_name="exists", before="absent", after=f"{creative_type} for app {adam_id}"
            + (f", page {product_page_id}" if product_page_id else ""),
        )

    def current(self, ctx, item):
        return None

    def request(self, ctx, items):
        item = items[0]
        return "creatives_post", {
            "x_ap_context": context_header(),
            "creative_create": CreativeCreate(
                name=item["entity_name"],
                creative_type=CreativeType(item["creative_type"]),
                destination=DestinationCreate(
                    destination_type=DestinationType("APP_STORE_PRODUCT_PAGE"),
                    parameters=DestinationParameter(
                        adam_id=item["adam_id"], product_page_id=item.get("product_page_id")
                    ),
                ),
            ),
        }

    def observe(self, ctx, response, items):
        return _created(response, "creative", items[0]["after"])


class CreateAd(ChangeKind):
    name = "create_ad"
    creates_named = ("ad",)
    preview_title = "Preview creating an ad in an ad group, created PAUSED (no write)"
    apply_title = "Create an ad (created PAUSED; never deleted -- pausing is the undo)"
    apply_annotations = APPLY_DESTRUCTIVE
    cost = 3
    ledger_keys = (("ad", CREATED),)
    success_note = "Created PAUSED. Enable it with preview_ad_status when ready."

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Ad group to put the ad in")],
        creative_id: Annotated[int, Field(description="Creative id from create_creative or list_creatives")],
        name: Annotated[str, Field(description="Ad name")],
    ) -> Proposal:
        """Show what creating an ad would do. Writes nothing.

        The ad is created PAUSED, so the projection is 0; enabling it is a
        separate ad_status change. Costs 3 applies.

        Pass the BASE name only: the configured prefix (<OWNER>-<agent>-) is
        added, and the preview shows the exact name that will be sent.
        """
        stamped = ctx.name_for_create("ad", name)
        name = stamped.final
        ad_group = ctx.ad_group(ad_group_id)
        campaign = ctx.campaign(ad_group.campaign_id)
        checks = [check_campaign_in_scope(ctx.limits, ad_group.campaign_id), *stamped.checks()]
        try:
            creative = unwrap(client.call("creatives_id_get", id=str(creative_id),
                                          x_ap_context=context_header()), "creative")
            checks.append(Check("creative_exists", creative is not None,
                                f"creative {creative_id} is {enum_value(getattr(creative, 'creative_type', None))}"))
        except ToolError as exc:
            checks.append(Check("creative_exists", False, str(exc)[:200]))
        path = entity_path(campaign, ad_group) + f" > Ad {name!r}"
        return Proposal(
            items=[make_item(
                entity_type="ad", entity_id=None, entity_name=name, path=path, field=CREATED,
                before=NOTHING, after="PAUSED", campaign_id=ad_group.campaign_id,
                ad_group_id=ad_group_id, creative_id=creative_id,
                **stamped.item_fields(ctx.identity),
            )],
            checks=checks, projected=Decimal("0"),
            created_names=[("ad", name)], naming=stamped.block(),
            warnings=[w for w in (stamped.warning(),) if w]
            + ["NOT idempotent: applying this twice creates two ads."]
            + name_clash_warning("ad", name, ad_group_id),
            entity_type="ad", entity_id="(new)", entity_name=name, path=path,
            field_name="exists", before="absent", after=f"ad with creative {creative_id}, PAUSED",
            campaign=campaign, entity_serving="(created PAUSED)",
        )

    def current(self, ctx, item):
        return None

    def request(self, ctx, items):
        item = items[0]
        return "ads_post", {
            "x_ap_context": context_header(),
            "ad_create": AdCreate(
                ad_group_id=int(item["ad_group_id"]), creative_id=int(item["creative_id"]),
                name=item["entity_name"], status=AdStatus("PAUSED"),
            ),
        }

    def observe(self, ctx, response, items):
        return _created(response, "ad", items[0]["after"], status_of=True)

    def inverse(self, ctx, entry):
        return AdStatusKind().undo_creation(entry)
