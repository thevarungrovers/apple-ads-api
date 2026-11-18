"""The one preview -> apply -> revert sequence every change kind goes through.

Before this existed each of the eight pairs carried its own copy of the apply
sequence, and revert_change dispatched through a hand-written dict of seven
more. With the sequence written once, a kind supplies only what is specific to
it (see :mod:`.base`) and cannot skip a gate.

THE APPLY SEQUENCE, in this order:

  1. take the token        single-use, consumed before any check, so a token
                           that fails cannot be retried until something shifts
  2. the kill switch       re-read now, never cached
  3. the caps              session (and day): refused before anything is read
  4. staleness             each entity's CURRENT value must still be the
                           preview's ``before``, or someone edited it in between
  5. ledger intent         one row per item, BEFORE the request leaves the process
  6. the request
  7. ledger outcome        per item, judged by READ-BACK -- HTTP 200 does not
                           mean applied -- and only then the counters

This module deliberately has no ``from __future__ import annotations``: the
tool functions it generates carry annotations built at runtime, and string
annotations would have to be resolved against globals where they do not exist.
"""

import inspect
import secrets
import sqlite3
import typing
from decimal import Decimal
from typing import Annotated, Any, Callable

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from apple_ads_mcp import ledger
from apple_ads_mcp.changes.base import PREVIEW, ChangeKind, Outcome, Proposal
from apple_ads_mcp.client import WRITE_TIMEOUT, normalized_value
from apple_ads_mcp.guardrails import (
    Check,
    check_campaign_in_scope,
    check_count,
    check_projected_delta,
)
from apple_ads_mcp.previews import (
    ApplyResult,
    BulkApplyResult,
    BulkChangePreview,
    ChangePreview,
    any_failed,
    pct_change,
    render_checks,
)


class Engine:
    """Runs kinds against one :class:`~.context.ChangeContext`."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.kinds: dict[str, ChangeKind] = {}

    # -- registry ----------------------------------------------------------------

    def add(self, kind: ChangeKind) -> ChangeKind:
        if kind.name in self.kinds:
            raise ValueError(f"change kind {kind.name!r} registered twice")
        self.kinds[kind.name] = kind
        return kind

    def kind_for_entry(self, entry: dict[str, Any]) -> ChangeKind:
        """The kind that owns this ledger row, found by what the row changed.

        Keyed on (entity_type, field) rather than on the tool name, because a
        revert's own row carries tool="revert_change" -- and reverting a revert
        has to find the same kind the original did.
        """
        key = (entry.get("entity_type"), entry.get("field"))
        for kind in self.kinds.values():
            if key in kind.ledger_keys:
                return kind
        raise ToolError(f"no revert path for {entry.get('tool')!r} ({key[0]} {key[1]}).")

    # -- shared gates --------------------------------------------------------------

    def guard_writes(self) -> None:
        """The kill switch. Checked inside every apply, never cached.

        A FILE rather than an env var: a long-lived stdio server never sees a
        variable exported after it started, but `touch .audit/DISABLE_WRITES`
        from another terminal halts the next apply_* within one tool call.
        """
        reason = self.ctx.writes_disabled()
        if reason:
            raise ToolError(
                f"writes are disabled: {reason}. Remove .audit/DISABLE_WRITES to "
                f"re-enable them."
            )

    def refuse_if_over_caps(self, ctx, projected: Decimal, cost: int) -> None:
        """Session first (this process), then the day (every process).

        Both are enforced HERE, at apply time, not only in the preview: another
        process may use up the day between a preview and its approval.
        """
        reason = ctx.counters.would_exceed(projected, cost) or ctx.daily.would_exceed(
            projected, cost
        )
        if reason:
            raise ToolError(reason)

    @staticmethod
    def confirm_unchanged(current: str, recorded: str, what: str) -> None:
        """TOCTOU. Refuse when the entity moved between preview and approval."""
        if normalized_value(current) != normalized_value(recorded):
            raise ToolError(
                f"{what} is now {current!r}, but the preview recorded {recorded!r}. "
                f"Something changed it in between -- possibly a person in the Apple "
                f"Ads UI. Re-run the preview and look at the new numbers before "
                f"deciding again."
            )

    def preview_checks(self, ctx, kind: ChangeKind, proposal: Proposal) -> list[Check]:
        checks = list(proposal.checks)
        checks.append(
            check_projected_delta(
                ctx.limits, proposal.projected, "enable" if proposal.is_enable else "change"
            )
        )
        checks.append(ctx.daily.check(proposal.projected))
        currency = self.account_currency_check(ctx)
        if currency is not None:
            checks.append(currency)
        role = self.role_check()
        if role is not None:
            checks.append(role)
        return checks

    @staticmethod
    def role_check():
        """Block a preview when this credential's role cannot change campaigns.

        Apple would refuse the write with a 403 anyway; saying so in the
        preview spares a human approving something that cannot happen. Unknown
        roles never block.
        """
        from apple_ads_mcp.tools_lookup import account_roles, role_can_write

        roles = account_roles(cached=True)
        can_write = role_can_write(roles or [])
        if can_write is None:
            return None
        return Check(
            "role_can_write",
            can_write,
            f"this credential's role on the ad account: {', '.join(roles or [])}"
            + ("" if can_write else " -- read-only, so Apple would refuse the write"),
        )

    @staticmethod
    def account_currency_check(ctx):
        """The ACCOUNT's currency against allowed_currencies.

        Every limit here is a bare number -- "5.00" -- that only means something
        once you know 5.00 of what. Checked on the account record rather than at
        startup because a server must start without calling Apple; an unreadable
        account skips the line rather than blocking every preview.
        """
        from apple_ads_mcp import client
        from apple_ads_mcp.guardrails import check_currency

        currency = client.account_currency()
        if currency is None or not ctx.limits.allowed_currencies:
            return None
        check = check_currency(ctx.limits, currency)
        check.name = "account_currency"
        return check

    # -- preview -----------------------------------------------------------------

    def preview(self, kind: ChangeKind, args: dict[str, Any]):
        ctx = self.ctx.fork()
        proposal: Proposal = kind.propose(ctx, **args)
        checks = self.preview_checks(ctx, kind, proposal)
        if kind.bulk:
            checks.insert(0, check_count(ctx.limits, len(proposal.items)))
        blocked = any_failed(checks)
        tool = f"apply_{kind.name}"
        token = ctx.previews.mint(
            tool,
            proposal.items,
            proposal.projected,
            blocked,
            payload={"is_enable": proposal.is_enable},
        )
        warnings = list(proposal.warnings) + self.extra_warnings(ctx, kind, proposal)
        if kind.bulk:
            return BulkChangePreview(
                preview_token=token,
                tool=tool,
                entity_type=proposal.items[0]["entity_type"] if proposal.items else "",
                count=len(proposal.items),
                changes=proposal.children,
                total_projected_daily_spend_delta=str(proposal.projected),
                guardrail_checks=render_checks(checks),
                blocked=blocked,
                warnings=warnings,
                note=proposal.note,
            )
        campaign = proposal.campaign
        budget = getattr(campaign, "daily_budget", None) if campaign is not None else None
        from apple_ads_mcp.client import enum_value, format_money

        return ChangePreview(
            preview_token=token,
            tool=tool,
            entity_type=proposal.entity_type,
            entity_id=str(proposal.entity_id),
            entity_name=proposal.entity_name or "(unnamed)",
            path=proposal.path,
            field=proposal.field_name,
            before=proposal.before,
            after=proposal.after,
            pct_change=pct_change(proposal.before_num, proposal.after_num)
            if proposal.after_num is not None
            else None,
            campaign_id=str(campaign.id) if campaign is not None else None,
            campaign_name=getattr(campaign, "name", None),
            campaign_daily_budget=format_money(budget.value) if budget and budget.value else None,
            campaign_serving_status=enum_value(getattr(campaign, "display_status", None)),
            entity_serving_status=proposal.entity_serving,
            last_7d=proposal.metrics,
            projected_daily_spend_delta=str(proposal.projected),
            guardrail_checks=render_checks(checks),
            blocked=blocked,
            warnings=warnings,
            note=proposal.note,
        )

    def extra_warnings(self, ctx, kind: ChangeKind, proposal: Proposal) -> list[str]:
        """The session and daily caps, as warnings on the preview.

        Warnings rather than FAIL lines for the APPLY counts: they are enforced
        at apply time, where the refusal names the cap, while a blocked token
        could only say "failed a guardrail".
        """
        warnings = []
        if proposal.is_enable and proposal.projected > 0:
            line = ctx.daily.check(proposal.projected)
            warnings.append(
                f"re-enabling resumes spend up to the daily budget; this {line.detail}"
            )
        for reason in (
            ctx.counters.would_exceed(proposal.projected, kind.cost),
            ctx.daily.would_exceed(proposal.projected, kind.cost),
        ):
            if reason:
                warnings.append(f"the apply will be REFUSED: {reason}")
        return warnings

    # -- apply -------------------------------------------------------------------

    def apply(self, kind: ChangeKind, token: str):
        ctx = self.ctx.fork()
        tool = f"apply_{kind.name}"
        pending = ctx.previews.take(token, tool)
        self.guard_writes()
        self.refuse_if_over_caps(ctx, pending.projected_delta, kind.cost)
        items = pending.items

        # Re-read every entity before writing any of them: a partial TOCTOU
        # check would be worse than none, because it would look like a check.
        for item in items:
            current = kind.current(ctx, item)
            if current is None:
                continue
            self.confirm_unchanged(
                current,
                item["before"] if item["before"] is not None else "none",
                f"{item['entity_type'].replace('_', ' ')} {item['entity_id']} {item['field']}",
            )

        entry_ids, batch_id = self.write_intents(tool, items, kind.cost)
        outcomes = self.send(ctx, kind, items, entry_ids)
        landed_total = self.record_outcomes(items, entry_ids, outcomes)
        if any(o.landed for o in outcomes):
            ctx.counters.record(landed_total, kind.cost)
        return self.result(ctx, kind, tool, items, entry_ids, batch_id, outcomes, landed_total)

    def write_intents(
        self,
        tool: str,
        items: list[dict[str, Any]],
        cost: int,
        reverts: list[str] | None = None,
    ) -> tuple[list[str], str | None]:
        """One ledger row per item, BEFORE the request leaves the process.

        A batch's rows share a batch_id (in extra_json), so the daily cap counts
        the batch once and revert_change can take the batch id. Each row carries
        its OWN item's projected delta, so a partial batch records only what
        landed.
        """
        batch_id = secrets.token_hex(8) if len(items) > 1 else None
        entry_ids = []
        for index, item in enumerate(items):
            entry_id = ledger.new_entry_id()
            entry_ids.append(entry_id)
            extra: dict[str, Any] = {}
            if batch_id:
                extra["batch_id"] = batch_id
            if cost != 1:
                extra["cost"] = cost
            detail = {
                k: v
                for k, v in item.items()
                if k
                not in (
                    "entity_type", "entity_id", "entity_name", "path", "field",
                    "before", "after", "campaign_id", "projected",
                )
            }
            if detail:
                extra["detail"] = detail
            try:
                ledger.write_intent(
                    entry_id,
                    tool=tool,
                    entity_type=item["entity_type"],
                    entity_id=item["entity_id"],
                    entity_name=item.get("entity_name"),
                    path=item.get("path") or "-",
                    field=item["field"],
                    before=item.get("before"),
                    after=item["after"],
                    campaign_id=item.get("campaign_id"),
                    projected_daily_spend_delta=str(item.get("projected") or "0"),
                    reverts_entry_id=reverts[index] if reverts else None,
                    extra=extra or None,
                )
            except sqlite3.Error as exc:
                for earlier in entry_ids[:-1]:
                    try:
                        ledger.write_outcome(
                            earlier, outcome=ledger.REFUSED, detail="batch intent failed"
                        )
                    except sqlite3.Error:
                        pass
                raise ToolError(
                    f"LEDGER: could not record the intent for this change "
                    f"({type(exc).__name__}: {exc}), so NOTHING WAS SENT to Apple. "
                    f"A write that cannot be recorded is refused. Check logs/apple-ads.db."
                ) from exc
        return entry_ids, batch_id

    def send(self, ctx, kind: ChangeKind, items, entry_ids) -> list[Outcome]:
        """The request. A failure is recorded against every row, then re-raised."""
        method, kwargs = kind.request(ctx, items)
        try:
            response = ctx.call(method, _request_timeout=WRITE_TIMEOUT, **kwargs)
            return kind.observe(ctx, response, items)
        except ToolError as exc:
            for entry_id in entry_ids:
                ledger.write_outcome(entry_id, outcome=ledger.FAILED, detail=str(exc))
            raise

    @staticmethod
    def record_outcomes(items, entry_ids, outcomes: list[Outcome]) -> Decimal:
        try:
            return Engine._record_outcomes(items, entry_ids, outcomes)
        except sqlite3.Error as exc:
            landed = [i["entity_id"] for i, o in zip(items, outcomes) if o.landed]
            raise ToolError(
                f"LEDGER: the request WAS SENT and Apple answered (landed: "
                f"{landed or 'none'}), but recording the outcome failed "
                f"({type(exc).__name__}: {exc}). The ledger rows stay unfinished; "
                f"run reconcile_ledger and check the Apple Ads UI before retrying."
            ) from exc

    @staticmethod
    def _record_outcomes(items, entry_ids, outcomes: list[Outcome]) -> Decimal:
        landed_total = Decimal("0")
        for item, entry_id, outcome in zip(items, entry_ids, outcomes):
            if outcome.landed:
                landed_total += Decimal(str(item.get("projected") or "0"))
            ledger.write_outcome(
                entry_id,
                outcome=ledger.APPLIED if outcome.landed else ledger.FAILED,
                observed_after=outcome.observed,
                entity_id=outcome.created_id,
                detail=""
                if outcome.landed
                else (
                    outcome.error
                    or f"Apple answered 200 but echoed {outcome.observed!r}, not {item['after']!r}"
                ),
            )
        return landed_total

    def result(self, ctx, kind, tool, items, entry_ids, batch_id, outcomes, landed_total):
        session = {k: str(v) for k, v in ctx.counters.snapshot().items()}
        daily = self.daily_snapshot(ctx)
        if kind.bulk:
            lines, failures = [], []
            for item, outcome in zip(items, outcomes):
                label = f"{item['entity_id'] or ''} {item.get('entity_name')!r}".strip()
                if outcome.landed:
                    suffix = f" (created as {outcome.created_id})" if outcome.created_id else ""
                    lines.append(f"{label}: {item.get('before')} -> {outcome.observed}{suffix}")
                elif outcome.error:
                    failures.append(f"{label}: {outcome.error}")
                    lines.append(f"{label}: FAILED -- {outcome.error}")
                else:
                    failures.append(
                        f"{label}: Apple reported success but {item['field']} is still "
                        f"{outcome.observed!r}, not {item['after']!r}. Nothing changed -- "
                        f"usually an automated bid strategy, where Apple sets the bids and "
                        f"a written one is accepted and ignored."
                    )
                    lines.append(f"{label}: NOT APPLIED -- still {outcome.observed}")
            succeeded = sum(1 for o in outcomes if o.landed)
            failed = len(items) - succeeded
            undo_id = batch_id or entry_ids[0]
            return BulkApplyResult(
                applied=succeeded > 0,
                entry_id=undo_id,
                entry_ids=entry_ids,
                tool=tool,
                succeeded=succeeded,
                failed=failed,
                results=lines,
                failures=failures,
                projected_daily_spend_delta=str(landed_total),
                session=session,
                daily=daily,
                note=(
                    f"{succeeded} applied, {failed} failed, one ledger entry each. "
                    + (
                        "Some items did NOT apply -- read the failures list before "
                        "assuming the batch landed. "
                        if failed
                        else ""
                    )
                    + (
                        f"Undo every item that landed with revert_change(entry_id='{undo_id}'), "
                        "or one item by its own entry id."
                        if succeeded
                        else ""
                    )
                    + (f" {kind.success_note}" if kind.success_note and succeeded else "")
                ).strip(),
            )
        item, outcome, entry_id = items[0], outcomes[0], entry_ids[0]
        landed = outcome.landed
        return ApplyResult(
            applied=landed,
            entry_id=entry_id,
            tool=tool,
            entity_type=item["entity_type"],
            entity_id=str(outcome.created_id or item["entity_id"]),
            entity_name=item.get("entity_name"),
            path=item.get("path"),
            field=item["field"],
            before=item["before"] or ("none" if item["field"] != "status" else "unknown"),
            after=item["after"],
            observed_after=outcome.observed,
            projected_daily_spend_delta=str(item.get("projected") or "0"),
            session=session,
            daily=daily,
            failures=[]
            if landed
            else [
                outcome.error
                or f"Apple accepted the request but {item['field']} is still "
                f"{outcome.observed!r}, not {item['after']!r}. Nothing changed."
            ],
            note=(
                (kind.success_note or f"Undo with revert_change(entry_id='{entry_id}').")
                if landed
                else kind.failure_note
            ),
        )

    def daily_snapshot(self, ctx) -> dict[str, str]:
        return ctx.daily.snapshot()

    # -- revert ------------------------------------------------------------------

    def revert(self, entry_id: str) -> ApplyResult:
        ctx = self.ctx.fork()
        self.guard_writes()
        # A revert is a real write to the live account, so it answers to the
        # same caps. Checked BEFORE the lookup: a loop reverting every entry in
        # the ledger -- changes from earlier sessions this process never saw --
        # must stop even on entries that turn out not to exist.
        self.refuse_if_over_caps(ctx, Decimal("0"), 1)

        entry = ledger.find_entry(entry_id)
        if entry is None:
            batch = ledger.batch_entries(entry_id)
            if batch:
                return self.revert_batch(ctx, entry_id, batch)
            raise ToolError(f"no ledger entry {entry_id!r}. list_my_changes shows valid ids.")
        kind, item = self.plan_revert(ctx, entry)
        projected = self.revert_projection(ctx, entry, item, kind)
        self.refuse_if_over_caps(ctx, projected, 1)
        return self.send_revert(ctx, [entry], [(kind, item)], projected)

    def plan_revert(self, ctx, entry: dict[str, Any]) -> tuple[ChangeKind, dict[str, Any]]:
        entry_id = entry.get("entry_id")
        if entry.get("outcome") != ledger.APPLIED:
            raise ToolError(
                f"entry {entry_id} is recorded as {entry.get('outcome')!r}, not "
                f"'applied'. Only a change that definitely landed can be reverted; "
                f"for a 'partial' or an unfinished one, run reconcile_ledger and "
                f"look at what actually happened first."
            )
        if entry_id in ledger.reverted_entry_ids():
            raise ToolError(f"entry {entry_id} has already been reverted.")
        entity_id = str(entry.get("entity_id") or "")
        # Rows written before the one-row-per-entity ledger: a bulk change was a
        # single row with comma-joined ids, and a negative add carried "(new)".
        if entry.get("tool") == "apply_negative_keywords_add" and entity_id in ("(new)", ""):
            raise ToolError(
                "that entry created negative keywords before the ledger recorded "
                "their ids, so it cannot be traced to the rows it made. Pause them "
                "with preview_negative_keyword_pause / apply_negative_keyword_pause."
            )
        if "," in entity_id:
            raise ToolError(
                "reverting a bulk bid change is not supported in one call: some "
                "items may have failed. Read list_my_changes for the per-item "
                "results and revert the ones you want individually with "
                "preview_keyword_bid."
            )
        # SCOPE, again. A revert writes to the live account like any apply, and
        # allowed_campaign_ids may have been narrowed since the original change:
        # undoing it must not reach a campaign the server is no longer allowed
        # to touch.
        scope = check_campaign_in_scope(ctx.limits, entry.get("campaign_id"))
        if not scope.passed:
            raise ToolError(f"refusing to revert entry {entry_id}: {scope.detail}.")
        kind = self.kind_for_entry(entry)
        if kind.not_revertible:
            raise ToolError(kind.not_revertible)
        before = entry.get("before")
        if before in (None, "", "none"):
            raise ToolError(
                f"entry {entry_id} recorded no previous value, so there is nothing "
                f"to put back."
            )
        try:
            send_kind, item = kind.inverse(ctx, entry)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        current = send_kind.current(ctx, item)
        if current is not None:
            self.confirm_unchanged(
                current,
                str(item["before"]),
                f"{str(entry.get('entity_type')).replace('_', ' ')} {entity_id} {item['field']}",
            )
        return send_kind, item

    def revert_projection(self, ctx, entry, item, send_kind=None) -> Decimal:
        """What undoing ``entry`` puts back into daily spend. Never assumed 0.

        Reverting a pause or a budget cut RESTORES spend, so it is counted
        against the session and daily totals like an enable (and, like one, is
        exempt from the per-change limit). The kind that sends the inverse
        decides the bound: a status kind bounds an enable by the campaign's
        budget headroom, a budget kind by the cut it undoes.
        """
        if send_kind is None:
            send_kind = self.kind_for_entry(entry)
        try:
            projected, _is_enable = send_kind.revert_projection(ctx, entry, item)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return max(projected, Decimal("0"))

    def revert_batch(self, ctx, batch_id: str, batch: list[dict[str, Any]]) -> ApplyResult:
        """Undo every still-applied row of one batch, each with its own request.

        All rows are planned (and their staleness checked) before any is sent,
        so one stale item stops the whole batch rather than half of it.
        """
        reverted = ledger.reverted_entry_ids()
        live = [
            e for e in batch if e.get("outcome") == ledger.APPLIED and e["entry_id"] not in reverted
        ]
        if not live:
            raise ToolError(
                f"batch {batch_id} has nothing left to revert: every entry in it was "
                f"never applied or has already been reverted."
            )
        plans = [self.plan_revert(ctx, entry) for entry in live]
        projected = sum(
            (self.revert_projection(ctx, e, item, k) for e, (k, item) in zip(live, plans)),
            Decimal("0"),
        )
        self.refuse_if_over_caps(ctx, projected, 1)
        return self.send_revert(ctx, live, plans, projected)

    def send_revert(self, ctx, entries, plans, projected: Decimal) -> ApplyResult:
        for (_kind, item), entry in zip(plans, entries):
            item["projected"] = str(projected if len(entries) == 1 else item.get("projected", "0"))
        items = [item for _kind, item in plans]
        entry_ids, _batch = self.write_intents(
            "revert_change", items, 1, reverts=[str(e.get("entry_id")) for e in entries]
        )
        outcomes: list[Outcome] = []
        for (kind, item), entry_id in zip(plans, entry_ids):
            outcomes.extend(self.send(ctx, kind, [item], [entry_id]))
        landed_total = self.record_outcomes(items, entry_ids, outcomes)
        all_landed = all(o.landed for o in outcomes)
        if any(o.landed for o in outcomes):
            ctx.counters.record(projected if all_landed else landed_total, 1)
        first, outcome = entries[0], outcomes[0]
        item = items[0]
        originals = ", ".join(str(e.get("entry_id")) for e in entries)
        return ApplyResult(
            applied=all_landed,
            entry_id=entry_ids[0],
            tool="revert_change",
            entity_type=str(first.get("entity_type")),
            entity_id=str(first.get("entity_id")),
            entity_name=first.get("entity_name")
            if len(entries) == 1
            else f"{len(entries)} entries",
            path=str(first.get("path")),
            field=str(item["field"]),
            before=str(item["before"]),
            after=str(item["after"]),
            observed_after=outcome.observed,
            projected_daily_spend_delta=str(projected),
            session={k: str(v) for k, v in ctx.counters.snapshot().items()},
            daily=self.daily_snapshot(ctx),
            failures=[]
            if all_landed
            else [
                f"Apple accepted the revert but {it['entity_id']} is still "
                f"{o.observed!r}, not {it['after']!r}."
                + (f" {o.error}" if o.error else "")
                for it, o in zip(items, outcomes)
                if not o.landed
            ],
            note=(
                f"Reverted entry {originals}."
                if all_landed
                else (
                    f"NOT REVERTED. Apple returned success and left the value alone, "
                    f"so entry {originals} still stands. For a bid this "
                    f"usually means the ad group moved onto an automated bid strategy, "
                    f"where Apple sets the bids. Recorded as failed, so the original "
                    f"entry stays revertable once the cause is fixed."
                )
            ),
        )


# ---------------------------------------------------------------------------
# Publishing a kind as two MCP tools
# ---------------------------------------------------------------------------


def _preview_function(engine: Engine, kind: ChangeKind) -> Callable[..., Any]:
    """``preview_<name>``, with exactly ``kind.propose``'s parameters.

    The MCP SDK builds the tool's input schema from the function signature, so
    the generated function has to LOOK like the hand-written tool it replaces:
    same parameter names, same Annotated descriptions, same docstring.
    """
    method = kind.propose
    signature = inspect.signature(method)
    hints = typing.get_type_hints(method, include_extras=True)
    parameters = [p for p in signature.parameters.values() if p.name != "ctx"]
    returns = BulkChangePreview if kind.bulk else ChangePreview

    def preview(**kwargs: Any):
        return engine.preview(kind, kwargs)

    preview.__name__ = preview.__qualname__ = f"preview_{kind.name}"
    preview.__doc__ = inspect.getdoc(method)
    preview.__signature__ = signature.replace(
        parameters=[p.replace(annotation=hints.get(p.name, p.annotation)) for p in parameters],
        return_annotation=returns,
    )
    preview.__annotations__ = {
        **{p.name: hints[p.name] for p in parameters if p.name in hints},
        "return": returns,
    }
    return preview


def _apply_function(engine: Engine, kind: ChangeKind) -> Callable[..., Any]:
    """``apply_<name>(preview_token)`` -- and nothing else, ever.

    An apply that accepted the values directly could be called cold, with no
    preview shown first; test_tool_surface pins that every apply requires the
    token.
    """
    token_type = Annotated[
        str, Field(description=f"The token returned by preview_{kind.name}")
    ]
    returns = BulkApplyResult if kind.bulk else ApplyResult

    def apply(preview_token: str):
        return engine.apply(kind, preview_token)

    apply.__name__ = apply.__qualname__ = f"apply_{kind.name}"
    apply.__doc__ = (
        f"Write the change preview_{kind.name} described. Live account. Takes only "
        "its token; show the human the preview first -- their approval of this "
        "call is the authorization."
    )
    apply.__signature__ = inspect.Signature(
        [
            inspect.Parameter(
                "preview_token", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=token_type
            )
        ],
        return_annotation=returns,
    )
    apply.__annotations__ = {"preview_token": token_type, "return": returns}
    return apply


def register_pair(mcp, engine: Engine, kind: ChangeKind, *, apply_tool: Callable) -> None:
    """Register ``preview_<kind>`` and ``apply_<kind>`` for one kind.

    ``apply_tool`` is the read-only-aware decorator from tools_write: in
    read-only mode it registers nothing, so the apply is absent from tools/list
    rather than refused at call time. The kind is still added to the engine,
    so revert_change (also absent then) and the tests can reach it.
    """
    engine.add(kind)
    mcp.tool(title=kind.preview_title, annotations=PREVIEW)(_preview_function(engine, kind))
    apply_tool(title=kind.apply_title, annotations=kind.apply_annotations)(
        _apply_function(engine, kind)
    )
