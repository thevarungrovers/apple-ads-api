---
name: apple-ads
description: Use the `apple-ads` MCP server to read an Apple Ads (Apple Search Ads) account and change it safely. Covers the preview/apply approval loop, keyword and ad group bids, pausing and enabling, daily budgets, negative keywords, reports, guardrails, the change ledger and undo. Use whenever the task involves Apple Ads, Apple Search Ads, App Store ads, keyword bids, ad group default bids, campaign budgets, negative keywords, search terms, or the apply_*/preview_* tools on that server.
---

# Apple Ads

The `apple-ads` MCP server talks to **one real, live advertising account**.

There is no sandbox. Apple does not run one for campaign management. Every write
lands on campaigns that are serving and spending today, so a mistake costs money
now, not at the next deploy.

## The one rule

**Read → preview → show the human → apply.** Never skip a step.

```
1. READ     get_guardrails, then list_/get_/..._report
2. PREVIEW  preview_<thing>  ->  returns a preview_token
3. SHOW     put the preview in front of the human, in your own words
4. APPLY    apply_<thing>(preview_token="pv_...")
```

Step 3 is the authorization. The human approving the `apply_*` permission prompt
**is** the sign-off — so never call an `apply_*` without having just shown them
what it does.

## DO

- **DO call `get_guardrails` first**, before proposing any change. It tells you
  the bounds and how much of this session's change budget is left, so you propose
  something that can be approved instead of something that will be refused.
- **DO read the current value before you change it.** `get_keyword`,
  `get_ad_group`, `get_campaign`, and the matching `*_report` for what it has
  actually been doing.
- **DO re-read anything older than a few minutes.** `list_recent_changes` shows
  every change on the account including ones a person made in the Apple Ads UI.
  Numbers on screen go stale.
- **DO write money as a decimal string in major units.** `"1.20"` is one dollar
  twenty.
- **DO read the whole preview before you summarise it**, especially
  `entity_serving_status`, `projected_daily_spend_delta`, `warnings` and
  `guardrail_checks`.
- **DO check the `applied` field on the result.** It is the only thing that says
  the change actually happened.
- **DO tell the human the `entry_id`.** It is what `revert_change` takes.
- **DO use `apply_keyword_bids_bulk` for many keywords at once** rather than a
  loop of single applies — one approval, one ledger entry, one session slot.

## DON'T

- **DON'T call an `apply_*` without showing the preview first.** The approval
  prompt is meaningless if nobody saw what it approves.
- **DON'T pass money as cents.** `"120"` where `"1.20"` was meant is a
  hundredfold error, and the number is rendered verbatim in the prompt a human
  approves.
- **DON'T treat `applied: true` as the default.** A write can return success and
  change nothing — see *Silent no-ops* below.
- **DON'T re-use a `preview_token`.** It is single use, expires in 10 minutes,
  and belongs to exactly one `apply_*` tool.
- **DON'T retry an `apply_*` that failed by calling it again with the same
  token.** Run the preview again; the account may have moved.
- **DON'T guess ids.** Look them up with `list_campaigns`, `list_ad_groups`,
  `list_keywords`.
- **DON'T try to delete anything.** No delete tool exists. Pause instead.
- **DON'T run the break-glass CLI yourself.** Hand the command to the human.

## The tools

19 read, 9 `preview_*`/`apply_*` pairs, plus `revert_change`. 38 in total.

### Read — safe, call freely

| Tool | Gives you |
|---|---|
| `whoami` | Which account this server writes to. Run it first when anything returns 401/403 |
| `get_guardrails` | The bounds, and this session's remaining budget |
| `list_campaigns` / `get_campaign` | Campaigns, status, daily budget |
| `list_ad_groups` / `get_ad_group` | Ad groups, status, default bid, bid strategy |
| `list_keywords` / `get_keyword` | Keywords, bids, match types |
| `list_negative_keywords` | Negatives on an ad group, or across a campaign's ad groups |
| `list_shared_budgets` | Shared budgets (read only — they cannot be changed here) |
| `campaign_report` / `ad_group_report` / `keyword_report` / `search_term_report` | Spend, impressions, taps, installs, CPT, CPI |
| `keyword_suggestions` | Apple's keyword ideas |
| `budget_recommendations` | Apple's budget advice (acting on it goes through the normal preview/apply path) |
| `list_recent_changes` | Apple's own audit trail — **everyone's** changes, not just yours |
| `list_my_changes` | What this server has done (the local ledger) |
| `reconcile_ledger` | Writes that silently failed, and changes somebody else made |

### Change — preview first, always

| Pair | Changes |
|---|---|
| `preview_keyword_bid` / `apply_keyword_bid` | One keyword's bid |
| `preview_keyword_bids_bulk` / `apply_keyword_bids_bulk` | Several keyword bids at once |
| `preview_keyword_status` / `apply_keyword_status` | Pause or enable a keyword |
| `preview_ad_group_status` / `apply_ad_group_status` | Pause or enable an ad group (**every keyword in it**) |
| `preview_ad_group_default_bid` / `apply_ad_group_default_bid` | The ad group's default bid |
| `preview_campaign_status` / `apply_campaign_status` | Pause or enable a **whole campaign** |
| `preview_campaign_daily_budget` / `apply_campaign_daily_budget` | A campaign's daily budget |
| `preview_negative_keywords_add` / `apply_negative_keywords_add` | Add negative keywords |
| `preview_negative_keyword_pause` / `apply_negative_keyword_pause` | Pause or enable a negative keyword |
| `revert_change` | Undo one ledger entry by its `entry_id` |

### Not here, on purpose

Creating or deleting campaigns, ad groups, ads, creatives and assets. Deleting
negative keywords. Changing shared budgets. Applying Apple's budget advice in one
call. There is **no generic request/passthrough tool**, so if an operation is not
in the tables above it cannot be done from here.

If the human needs one of those, tell them so and hand them the break-glass CLI
line to run themselves:

```
./venv/bin/python apple_ads_cli.py <path> -X PUT -d '{...}' --apply --confirm-live
```

## Silent no-ops — the trap to know

**HTTP 200 does not mean the change happened.** If an ad group is on an automated
bid strategy (`MAX_CONVERSIONS` or `MAX_ENGAGEMENTS`), Apple sets the bids itself.
A bid you write is accepted with a success response and then thrown away, with
nothing in the reply saying so.

- The preview catches this up front with a `FAIL bid_is_settable` check, and the
  apply is then refused.
- Every apply also compares the value Apple echoed back against the one asked
  for, and reports `applied: false` when they differ.
- **So read `applied`, not the absence of an error.**

Only `MANUAL_CPT` and `MANUAL_CPM` make a bid mean anything. Check with
`get_ad_group`. Switching the strategy is a campaign decision a person makes in
the Apple Ads UI — this server will not do it.

## Reading the preview

Four fields turn an approval into a judgement. Put them in your summary.

| Field | Why it matters |
|---|---|
| `entity_name` | `'competitor brand term'` reads differently from `keyword 1234567890` |
| `path` | `Campaign 'X' > AdGroup 'Y' > Keyword 'z'` |
| `entity_serving_status` | `RUNNING` means **this** entity can spend today. A keyword under a paused ad group reads `AD_GROUP_ON_HOLD` even when the campaign reads `RUNNING` |
| `projected_daily_spend_delta` | Upper bound on extra spend per day |

`projected_daily_spend_delta` is for catching a 100× typo, **not** a forecast. It
assumes tap volume does not change, which is precisely what a bid change is meant
to do. A budget change is the exception: that number is exact, because the cap
itself is what moved.

If `blocked: true`, a guardrail failed. The `FAIL` line in `guardrail_checks`
says which. The apply will refuse the token, so fix the proposal rather than
trying the apply.

## Undo

```
revert_change(entry_id="...")
```

It refuses unless the entry is recorded as `applied`, refuses one already
reverted, and re-reads the entity first — if the current value is not what the
entry left behind, somebody else moved it and reverting would overwrite **their**
change.

It **cannot** undo `apply_negative_keywords_add`, because that created rows and
deletion is not exposed. Pause them instead.

## When something goes wrong

| Message | What it means | What to do |
|---|---|---|
| `preview_token ... is unknown or already used` | Single use, 10-minute life | Re-run the preview |
| `... is now X, but the preview recorded Y` | Somebody changed it in between | Re-run the preview, look at the new numbers, decide again |
| `that preview was blocked by a guardrail` | A bound was breached | Read `guardrail_checks`, propose something inside the bounds |
| `writes are disabled: ...` | The kill switch file exists | A human removes `.audit/DISABLE_WRITES`. Do not work around it |
| `session limit reached: N applies already made` | Per-session cap | Show `list_my_changes`, let the human decide; the server must restart to reset |
| `applied: false` + "returned success and left the value alone" | Silent no-op | Almost always an automated bid strategy. Nothing changed |
| `projected_daily_spend_delta: 0` + a warning about "0 by default, NOT by measurement" | The 7-day report failed | Treat the zero as unknown, not as "spends nothing" |
| 401/403 on everything but `whoami` | Wrong account context | Run `whoami` and tell the human what it says |

## A worked example

> "Raise the bid on 'organic grocery delivery' to $2.50."

```
1. get_guardrails()                      -> max_bid 5.00, 3 of 20 applies used
2. list_keywords(ad_group_id=...)        -> find the id, read the current bid
3. get_ad_group(ad_group_id=...)         -> bidStrategy is MANUAL_CPT, so a bid will stick
4. keyword_report(...)                   -> what it has been doing
5. preview_keyword_bid(keyword_id=..., new_bid="2.50")
6. Summarise for the human: current bid, new bid, the path, whether it is serving
   today, the projected daily delta, and any warnings.
7. apply_keyword_bid(preview_token="pv_...")
8. Report `applied`, `observed_after`, and the `entry_id` for undo.
```

If step 3 had shown `MAX_CONVERSIONS`, stop at step 5: the preview will block,
and the honest answer is "Apple sets the bids on this ad group, so this change
cannot take effect — the bid strategy has to change first, in the Apple Ads UI."
