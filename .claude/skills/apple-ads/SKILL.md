---
name: apple-ads
description: Use the `apple-ads` MCP server to read an Apple Ads (Apple Search Ads) account and change it safely. Covers the preview/apply approval loop, keyword and ad group bids, pausing and enabling, daily budgets, keywords and negative keywords, bid strategies and CPA goals, targeting (Search Match, age, gender, devices, daypart, locations, countries), creating campaigns, ad groups, creatives (custom product pages) and ads, Apple's recommendations, renames, reports, guardrails and the daily cap, the change ledger and undo. Use whenever the task involves Apple Ads, Apple Search Ads, App Store ads, keyword bids, campaign budgets, negative keywords, search terms, custom product pages, or the apply_*/preview_* tools on that server.
---

# Apple Ads

The `apple-ads` MCP server talks to **one real, live advertising account**.

There is no sandbox. Apple does not run one for campaign management, and there is
no validate-only mode either: a preview checks the server's own bounds, not
whether Apple will accept the change. Every write lands on campaigns that are
serving and spending today, so a mistake costs money now.

## The one rule

**Read → preview → show the human → apply.** Never skip a step.

```
1. READ     get_account, get_guardrails, then list_/get_/..._report
2. PREVIEW  preview_<thing>  ->  returns a preview_token
3. SHOW     put the preview in front of the human, in your own words
4. APPLY    apply_<thing>(preview_token="pv_...")
```

Step 3 is the authorization. The human approving the `apply_*` permission prompt
**is** the sign-off — so never call an `apply_*` without having just shown them
what it does.

## DO

- **DO call `get_account` once per session**: the timezone decides what "today"
  means in every report, the currency is what every amount is in, and a
  read-only role blocks every preview.
- **DO call `get_guardrails` before proposing a change.** It shows the bounds,
  this session's budget, and **today's** budget, which is shared with every other
  session and survives restarts.
- **DO read the current value before you change it** (`get_*`, `list_*`, the
  matching `*_report`).
- **DO write money as a decimal string in major units.** `"1.20"` is one dollar
  twenty.
- **DO read the whole preview**, especially `entity_serving_status`,
  `projected_daily_spend_delta`, `warnings` and `guardrail_checks`.
- **DO check `applied` on the result.** It is the only thing that says the
  change happened.
- **DO tell the human the `entry_id`** (or the batch id). It is what
  `revert_change` takes.
- **DO page.** Every list and report returns `truncated` and `next_offset`; when
  `truncated` is true you have not seen everything.

## DON'T

- **DON'T call an `apply_*` without showing the preview first.**
- **DON'T pass money as cents.** `"120"` where `"1.20"` was meant is a
  hundredfold error, rendered verbatim in the prompt a human approves.
- **DON'T treat "no error" as success.** See *Silent no-ops*.
- **DON'T re-use a `preview_token`.** Single use, 10 minutes, one tool.
- **DON'T try to delete anything.** No delete tool exists. Pausing is the undo.
- **DON'T run the break-glass CLI.** `apple_ads_cli.py ... --apply` needs an
  interactive terminal and a typed confirmation, and Claude Code denies it to
  you. If something is not in the tables below, tell the human and hand them the
  command to run themselves.

## The tools

40 read, 31 `preview_*`/`apply_*` pairs, plus `revert_change` and
`resolve_unknown_change`. 104 in total.

### Read — safe, call freely

| Tool | Gives you |
|---|---|
| `whoami`, `get_account` | The credential, the ad account, its timezone, currency and your role |
| `get_guardrails` | Bounds, and what is left this session and today |
| `list_campaigns` / `get_campaign` | Campaigns; the detail adds strategy, schedule, placements, limiting reasons, 7d/30d |
| `list_ad_groups` / `get_ad_group` | Ad groups; the detail adds bid strategy, CPA goal, Search Match, full targeting, counts |
| `list_keywords` / `get_keyword`, `list_negative_keywords` | Keywords, bids, match types; negatives |
| `list_ads` / `get_ad`, `list_creatives`, `creative_rejection_reasons` | Ads and creatives |
| `list_product_pages` / `get_product_page` | Custom product pages (made in App Store Connect) |
| `campaign_report` / `ad_group_report` / `keyword_report` / `ad_report` / `search_term_report` | Spend, taps, installs. `group_by` countryOrRegion/deviceClass/ageRange/gender, `segment_by_date`, `sort_by`, paging |
| `impression_share_report`, `search_term_popularity` | Share of voice; demand for a term |
| `keyword_suggestions`, `phrase_suggestions`, `category_suggestions` | Apple's ideas, by app, campaign, country |
| `budget_recommendations`, `target_cpa_recommendations`, `target_cpa_suggestion` | Apple's advice |
| `search_geos` / `get_geos`, `list_location_groups` | Location ids for targeting |
| `search_apps`, `app_eligibility`, `supported_languages` | What a new campaign can promote |
| `list_shared_budgets` | Shared budgets and the campaigns they feed (read only) |
| `list_recent_changes`, `get_change_details` | Apple's audit trail — **everyone's** changes, field by field |
| `list_my_changes`, `reconcile_ledger` | This server's ledger, and what does not match Apple's |

### Change — preview first, always

Each is `preview_<name>` / `apply_<name>`.

| `<name>` | Changes |
|---|---|
| `keyword_bid`, `keyword_bids_bulk` | Keyword bids |
| `keyword_status`, `ad_group_status`, `campaign_status`, `ad_status` | Pause or enable |
| `ad_group_default_bid`, `campaign_daily_budget` | Default bid; daily budget |
| `add_keywords` | New keywords, **created PAUSED** |
| `keyword_match_type` | Adds a PAUSED copy with the new match type and pauses the old |
| `negative_keywords_add`, `negative_keyword_pause`, `negative_keyword_status` | Negatives (created ENABLED — a paused negative does nothing) |
| `ad_group_bid_strategy`, `campaign_bid_strategy`, `ad_group_cpa_goal`, `campaign_schedule` | Bidding model, CPA goal, start/end |
| `ad_group_search_match`, `ad_group_demographics`, `ad_group_devices`, `ad_group_daypart`, `ad_group_locations`, `ad_group_app_downloaders`, `campaign_countries` | Targeting |
| `create_campaign`, `create_ad_group`, `create_creative`, `create_ad` | Creation, **always PAUSED**; 3 applies each |
| `budget_recommendation`, `target_cpa_recommendation` | Apple's advice, through the normal budget / CPA bounds |
| `low_risk_change` | Rename a campaign, ad group, ad or creative; dismiss a recommendation |
| `revert_change` | Undo an entry, or a whole batch by its id |
| `resolve_unknown_change` | Settle an `unknown` (timed-out) entry by read-back: records applied or failed, writes only the ledger, undoes nothing |

### Not here, on purpose

Deleting anything. Shared budget writes, billing and account settings, location
group writes and Maps (brand) ads, asset uploads, one-call
`apply_*_recommendations`. There is **no generic passthrough tool**.

## The caps

- **Per change:** `max_bid`, `max_bid_increase_pct`, `max_daily_budget`,
  `max_budget_change_pct`, `max_cpa_goal`, `max_cpa_goal_change_pct`,
  `max_entities_per_call`, `max_projected_delta_per_day`.
- **Per session:** `max_applies_per_session`, `max_projected_session_delta`.
  Reset by a restart.
- **Per day:** `max_applies_per_day`, `max_projected_delta_per_day_total`. Read
  from the ledger, shared by every session and the CLI, reset at **local**
  midnight. A batch counts once; a creation counts 3.
- **Enables are special.** Anything that resumes or widens delivery — enabling a
  campaign, ad group, keyword or ad, pausing a negative, switching Search Match
  on, loosening targeting, handing bids to Apple, undoing a pause — projects the
  most it could add (a campaign's whole budget, or its unused budget), shows
  `SKIP projected_daily_delta`, and is counted in full against the session and
  daily totals. A FAIL `projected_daily_total` line names what it would use and
  what is left today.

## Errors

Every failure starts with a stable prefix: `AUTH`, `FORBIDDEN_ROLE`,
`NOT_FOUND`, `RATE_LIMITED`, `INVALID`, `TRANSIENT` (Apple), `CONFIG`, `LEDGER`,
`INVALID_REQUEST`, `INTERNAL` (local). Reads are retried on `RATE_LIMITED` and
`TRANSIENT`; writes never are. A `LEDGER` error before a write means nothing was
sent; after one, run `reconcile_ledger`.

## Silent no-ops — the trap to know

**HTTP 200 does not mean the change happened.** Under an automated bid strategy
(`MAX_CONVERSIONS` / `MAX_ENGAGEMENTS`) Apple accepts a written bid and throws it
away. The preview FAILs `bid_is_settable`; every apply also compares Apple's echo
with what was asked and reports `applied: false` when they differ. Read `applied`.

## Recipes

**A daily check.** `get_account` → `get_guardrails` → `campaign_report(days=1)`
and `campaign_report(days=7)` → `list_recent_changes(days=1)` →
`reconcile_ledger(days=1)`. Report spend against budget, anything not serving
(`get_campaign` limiting reasons), and any change somebody else made.

**A search-term harvest.** `search_term_report(campaign_id=..., days=14,
min_impressions=...)` → for converting terms, `preview_add_keywords` (they arrive
PAUSED) → for wasted spend, `preview_negative_keywords_add` → show both → apply
→ later, enable the new keywords with `preview_keyword_status`.

**A custom product page test.** `list_product_pages(adam_id)` →
`preview_create_creative(name="<base>", product_page_id=...)` →
`preview_create_ad(ad_group_id, creative_id, name="<base>")` (PAUSED; both names
get your prefix) → when ready, `preview_ad_status(status="ENABLED")` →
compare with `ad_report(ad_group_id=...)`.

**A new campaign.** `search_apps` → `app_eligibility` → `preview_create_campaign`
with the base name only (state the `regulation_responses` explicitly; the human
is answering for them; read the final name off the `naming` block) →
`preview_create_ad_group` (base name again) → `preview_add_keywords` → negatives → review it all →
`preview_campaign_status(status="ENABLED")`, which projects the whole budget.

**Acting on a recommendation.** `budget_recommendations` or
`target_cpa_recommendations` → `preview_budget_recommendation(campaign_id)` or
`preview_target_cpa_recommendation(ad_group_id)` → the normal bounds decide →
apply. To set one aside, `preview_low_risk_change(entity_type=
"daily_budget_recommendation", entity_id=..., field="dismiss")` (no undo).

## Naming

Pass the BASE name to `create_campaign`, `create_ad_group`, `create_creative`
and `create_ad`. The server adds `<OWNER>-<agent>-` from this checkout's `.env`
(`AB-max-spring_promo`; one part if only one is set; nothing if neither is) and
the preview's `naming` block shows the exact final name -- show that to the
human. Never type a prefix yourself: ours twice is refused, and a name that
reads as anyone else's prefix (`CD-bot-...`, `AB-cli-...`) is ALWAYS refused --
even when no identity is configured -- because it would impersonate them. Over
200 characters (prefix included) is refused, not truncated, and so is any
invisible character (control, zero-width, bidi mark, line separator).
A house-style name, if the team has one, is the base: the prefix goes first.
A duplicate campaign name, or ad group name in its campaign, fails
`name_is_free` -- on a create or a rename -- and so does a scope too big to
scan in full ("could not verify the name is free").

Renames (`low_risk_change`, field `name`) reach only YOUR entities -- the name
carries your WHOLE prefix or the ledger shows this server created it under your
identity. A name that reads as another full identity (`CD-bot-x`) is refused
whatever the ledger shows. With only
initials `AB` set, `AB-bot-x` (agent `bot`'s) and `AB-cli-x` (a person's) are
not yours, and neither counts for `mine=True`. Hand-made and other agents'
entities are refused; tell the human to rename those in the Apple Ads UI. The
prefix is kept: pass the new base.

Find your work with `list_campaigns` / `list_ad_groups` / `list_ads` /
`list_creatives` and `mine=True`, or filter by `origin` (`agent`, `cli`,
`manual`), `agent_name`, `owner`. `get_guardrails` and `whoami` show the
identity; `reconcile_ledger` lists `prefix_lost` / `prefix_without_ledger`.

## Undo

`revert_change(entry_id=...)` refuses unless the entry is `applied`, refuses one
already reverted, re-checks the campaign scope, and re-reads the entity — if it
is no longer what the entry left, somebody else moved it. A creation is undone by
**pausing** it; one already paused has nothing to undo. A batch id undoes every
item of the batch that landed. Undoing a pause or a cut restores spend and counts
against the caps.

An `unknown` entry (the request timed out) may have landed and counts as if it
had. Settle it with `resolve_unknown_change(entry_id=...)` -- a batch id settles
every unknown item -- rather than calling `revert_change` to find out.
`applied` is recorded at once. `failed` needs the entry to be 5 minutes old
(Apple's read can lag; earlier you get "too early to tell; try again after
<time>" -- wait and call it again), and is never given when a later ledger row
touched the same field, or a CLI row named the same id (it stays `unknown`; tell the human to check the Apple
Ads UI). Reverting a batch id skips an `unknown` item Apple still shows at its
old value (listed in `skipped`, left `unknown`) and undoes the items that
landed; revert the skipped one later by its own entry id. A `partial` targeting entry is live; `revert_change` restores only the
dimension it asked for.

## When something goes wrong

| Message | What to do |
|---|---|
| `preview_token ... is unknown or already used` | Re-run the preview |
| `... is now X, but the preview recorded Y` | Somebody changed it; re-run the preview and decide again |
| `that preview was blocked by a guardrail` | Read the FAIL line; propose something inside the bounds |
| `writes are disabled: ...` | A human removes `.audit/DISABLE_WRITES`. Do not work around it |
| `session limit reached` / `daily ... limit reached` | Stop. Show `list_my_changes`; the human decides |
| `applied: false` | Nothing changed — usually an automated bid strategy |
| `0 by default, NOT by measurement` | The 7-day report failed; treat the zero as unknown |
| `FORBIDDEN_ROLE` / 401/403 everywhere | Run `whoami` and `get_account`; tell the human |

## A worked example

> "Raise the bid on 'example term' to $2.50."

```
1. get_guardrails()                      -> max_bid 5.00, applies left today
2. list_keywords(ad_group_id=...)        -> the id and current bid
3. get_ad_group(ad_group_id=...)         -> MANUAL_CPT, so a bid will stick
4. keyword_report(campaign_id=..., keyword_id=...)
5. preview_keyword_bid(keyword_id=..., new_bid="2.50")
6. Summarise: current and new bid, the path, serving today?, projected delta, warnings.
7. apply_keyword_bid(preview_token="pv_...")
8. Report `applied`, `observed_after` and the `entry_id`.
```
