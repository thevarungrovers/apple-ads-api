# Apple Ads API

OAuth (client-credentials) access to the Apple Ads account: campaign performance
pulled programmatically instead of exported by hand, plus an **MCP server** so an
AI agent can read the account and propose changes a human approves one at a time.

## One API

Everything talks to the **Apple Ads Platform API** at `https://api.ads.apple.com/v1`
through Apple's official `apple-ads-platform` client.

The older Campaign Management **v5** API (`api.searchads.apple.com/api/v5`)
sunsets on **2027-01-26**, and this repo used to carry a hand-rolled client for
it alongside the new one. That client was retired on 2026-10-09, while v5 was
still answering — deliberately, because **a port can only be verified while both
APIs still work**. Each script was diffed against its v5 output on the same
window before the old one was deleted:

- `fetch_campaign_report.py` — identical for every complete day. The only rows
  that differed were *today's*, by one impression, because the day was still
  accumulating; re-running showed it climbing 123 → 124 → 125 while spend, taps
  and installs held.
- `fetch_ad_structure.py` — same 1 campaign, 5 ad groups and 34 keyword ids. The
  32 field differences were all vocabulary, not data (below).

A port done after the sunset would have been one nobody could check.

### Two renames to know about

The Platform API uses different words for the same states, which matters if you
compare a new `ad_structure.json` against one saved before the migration:

| v5 | Platform API |
| --- | --- |
| keyword `status: ACTIVE` | `status: ENABLED` |
| `servingStatus: NOT_RUNNING` | `displayStatus: PAUSED` |
| `supplySources`, `countriesOrRegions` | `targeting.supplyPlacement`, `targeting.countryOrRegion` — singular, each `{include: [...]}` |
| `X-AP-Context: orgId=<id>` | `X-AP-Context: adAccountId=<id>;` — **different value**, trailing semicolon |

Nothing in this repo compares those strings, so the rename is cosmetic here. It
would not be in anything downstream that does.

## How the authentication works

Three separate steps, each with its own lifetime. Confusing them is the main
source of "it worked yesterday" problems.


| #   | What                                                                                 | Lifetime                   | Handled by                               |
| --- | ------------------------------------------------------------------------------------ | -------------------------- | ---------------------------------------- |
| 1   | **Client secret** — a self-signed ES256 JWT, produced locally from `private-key.pem` | **180 days** (Apple's cap) | `generate_client_secret.py`              |
| 2   | **Access token** — obtained by POSTing that secret to Apple                          | **3600 s**                 | the SDK's `TokenManager` (in-process, auto-refreshed) |
| 3   | **API call** — `Authorization: Bearer …` + `X-AP-Context: adAccountId=<id>;`          | per request                | `apple_ads_mcp/client.py` |


The context header is `adAccountId=<id>;` — with the trailing semicolon, and
with the **ad account id, not the org id**. A wrong format is a 401/403 on every
call that carries it, while `/acls` keeps working: that is the one endpoint
taking no context at all, which is why the health check calls it first.

Apple never sees the private key. It verifies our signature against the public
key registered in the Apple Ads UI.

The client secret is deliberately **not** stored on disk: it is cheap to
re-derive from `private-key.pem` plus the three values in `.env`, so
the SDK re-derives it in memory on every token fetch, so a long-running server
can never age out of its own secret. Nothing is cached on disk.

---



## Setup

Python **3.12+** (`apple-ads-platform`'s floor; the repo's `venv/` runs 3.13).
`cryptography` must be **>= 50** — an older pin will break the Platform API client.

```bash
cd apple-ads-api

# 1. Environment — either a dedicated one...
conda env create -f environment.yml
conda activate apple-ads

#    ...or install into the environment you already use:
#    python3 -m pip install -r requirements.txt

# 2. Credentials
cp .env.example .env
$EDITOR .env          # paste clientId, teamId, keyId, orgId

# 3. Verify the whole chain AND discover your adAccountId.
#    Rung 5 of 7 prints the APPLE_ADS_AD_ACCOUNT_ID line to paste into .env.
./venv/bin/python test_platform_connection.py
```

`conda list | grep -E 'pyjwt|cryptography|requests|dotenv'` confirms the four
packages are in the active environment. A missing `cryptography` is the usual
cause of `ModuleNotFoundError` — it means a different environment is active than
the one the packages went into.

`.env` holds:


| Variable                        | Required | Where it comes from                                                                       |
| ------------------------------- | -------- | ----------------------------------------------------------------------------------------- |
| `APPLE_ADS_CLIENT_ID`           | yes      | shown **once** when the API client is generated                                           |
| `APPLE_ADS_TEAM_ID`             | yes      | Apple Ads UI, API client list                                                             |
| `APPLE_ADS_KEY_ID`              | yes      | Apple Ads UI, API client list                                                             |
| `APPLE_ADS_ORG_ID`              | yes      | Apple Ads UI, Account Settings. Narrows the ACL candidates when `test_platform_connection.py` discovers your `adAccountId` — it cannot decide between them on its own |
| `APPLE_ADS_AD_ACCOUNT_ID`       | for the Platform API | **discovered, not looked up** — run `test_platform_connection.py`            |
| `APPLE_ADS_DEFAULT_CAMPAIGN_ID` | no       | default for `fetch_ad_structure.py --campaign`, so no real id has to be typed             |

`APPLE_ADS_AD_ACCOUNT_ID` is **not** the org id. Apple returns them as two
separate fields (`id` and `orgId`) on the same ACL record, and the Platform API
wants the former. Do not read it off the UI and do not guess it — step 5 of
`test_platform_connection.py` reads it back from `GET /acls` and prints the
line to paste into `.env`. Writing to the wrong account is not an error Apple
will catch for you.

Nothing in this repo defaults any of these. `load_config()` raises `ConfigError`
on a missing value, which is the correct failure: this repo is public, so a real
id has nowhere to live except `.env`, which is gitignored.


`clientId` and `teamId` are usually the **same** `SEARCHADS.<uuid>` string. That
makes a swap between them invisible on inspection — the JWT uses `sub` for the
clientId and `iss` for the teamId, and they must not be "tidied" together.

---



## Usage

```bash
# End-to-end health check, one rung at a time
./venv/bin/python test_platform_connection.py   # files -> key -> secret -> token -> adAccountId -> API

# The MCP server's own surface check — no network, no Apple
./venv/bin/python tests/test_tool_surface.py

# Daily campaign performance (spend, impressions, taps, installs, avg CPT)
./venv/bin/python fetch_campaign_report.py                     # last 7 days
./venv/bin/python fetch_campaign_report.py --days 30 --csv     # writes reports/*.csv
./venv/bin/python fetch_campaign_report.py --start 2026-08-01 --end 2026-08-28

# Resolve ad ids -> names (campaign / ad group / keyword tree)
./venv/bin/python fetch_ad_structure.py                        # table to stdout
./venv/bin/python fetch_ad_structure.py --campaign 1234567890 --save
./venv/bin/python fetch_ad_structure.py --save                 # defaults to $APPLE_ADS_DEFAULT_CAMPAIGN_ID

# Which user came from which ad group / keyword
./venv/bin/python resolve_attribution.py reports/attribution_rows.csv
./venv/bin/python resolve_attribution.py rows.csv --by-ad-group
./venv/bin/python resolve_attribution.py rows.csv --csv reports/out.csv

# Any endpoint, raw JSON (see break-glass below)
./venv/bin/python apple_ads_cli.py acls
./venv/bin/python apple_ads_cli.py campaigns/query -X POST -d '{}'

# How many days before the client secret expires
./venv/bin/python generate_client_secret.py
```

`--print` on `generate_client_secret.py` emits the raw JWT. It is off by default
so a credential does not land in terminal scrollback or shell history by
accident.

From Python, reuse the server's own client layer rather than writing a second one:

```python
from apple_ads_mcp.client import call, context_header, unwrap, query

campaigns = unwrap(
    call("campaigns_query_post", x_ap_context=context_header(), query_request=query()),
    "campaigns",
)
for campaign in campaigns:
    print(campaign.id, campaign.name, campaign.status)
```

---

## Driving this from an AI agent

`apple_ads_mcp/` is a stdio **MCP server** named `apple-ads`. It exposes 36 tools:
19 read-only, and 8 `preview_*`/`apply_*` pairs plus `revert_change`.

### The authorization model

There is no `--confirm-live` here, because an agent can type a flag as easily as
a human can. **The authorization gate is Claude Code's own per-tool-call
permission prompt** — the moment a human sees what is about to happen and says
yes.

That only works if the prompt is worth reading, which drives two choices:

**Separate `preview_*` and `apply_*` tools, never a `dry_run` parameter.**
Claude Code's permission rules key on the **tool name**. Separate names let you
permanently allowlist every `preview_*` and never allowlist a single `apply_*`.
With a `dry_run` flag, one "always allow" clicked during a harmless preview would
silently authorise every future real write — exactly the failure being guarded
against.

**Every `apply_*` demands a `preview_token`** minted by its matching preview. The
token is single-use, expires after 10 minutes, and is refused if the entity's
*current* value no longer matches the one the preview recorded. That last check is
a TOCTOU guard: between the preview and the approval, somebody in the Apple Ads UI
may have moved the same bid. It also guarantees a readable preview always sits
directly above the approval prompt — an `apply_*` can never be called cold.

A preview answers the four questions that make an approval a judgement rather
than a reflex:

| Field | Why it is there |
| --- | --- |
| `entity_name` | `'competitor brand term'`, not `keyword 1234567890` |
| `path` | `Campaign 'X' > AdGroup 'Y' > Keyword 'z'` |
| `entity_serving_status` | whether **this** entity can spend money today. Not the campaign's: a keyword under a paused ad group reads `AD_GROUP_ON_HOLD` while its campaign reads `RUNNING` |
| `projected_daily_spend_delta` | an upper bound on the extra daily spend |

Projections are labelled upper bounds and are for catching a 100× typo, not for
forecasting: a bid change assumes tap volume is unchanged, which is precisely what
it is meant to alter. A budget change is exact — it is the cap itself moving.

### What it can and cannot do

In scope: keyword bids (single and bulk), keyword / ad group / campaign
pause-enable, campaign daily budgets, adding negative keywords and pausing them.

**Out of scope, structurally.** There is no `request(method, path, body)`
passthrough, so an operation with no tool is *unreachable*, not merely
undocumented. `tests/test_tool_surface.py` pins the registered tool-name set to a
`frozenset` and asserts the forbidden API method names appear nowhere in
`tools_write.py`, so the surface cannot widen by accident:

- creating or deleting campaigns, ad groups, ads, creatives, assets
- **deleting** negative keywords — pausing achieves the same outcome reversibly,
  and deletion would be the only irreversible operation in scope
- `apply_daily_budget_recommendations` — one call that moves real money with no
  preview, no bound and no ledger line. The read-only `budget_recommendations`
  tool shows the advice; acting on it goes through the normal preview/apply path

Two tools are **deferred, not dropped**, because the API does not yet support
them cleanly: ad group default bid (`AdGroupUpdate` has no `defaultBid` field;
the bid appears to sit under `bidStrategy.bid`, unverified) and shared budgets
(`shared_budgets_id_put` takes no `x_ap_context`, so there is no way to say which
ad account an update belongs to).

`destructive_hint=True` is set on exactly two tools — the campaign pause and the
campaign budget. Pausing a keyword is reversible, and marking it destructive would
train you to click through the loud prompts, which is how loud prompts stop
working.

### A write can be in scope and still be a no-op

**Keyword bids only mean anything under a manual bid strategy.** If the ad group
is on `MAX_CONVERSIONS` or `MAX_ENGAGEMENTS`, Apple sets the bids, and a
keyword-bid `PUT` is accepted with **HTTP 200 and then discarded** — same status
code, same response shape, bid unchanged, nothing anywhere saying it was ignored.

`preview_keyword_bid` therefore reads the parent ad group's `bidStrategy` and
blocks on a `bid_is_settable` check before anyone is asked to approve a change
that cannot land:

```
FAIL bid_is_settable: ad group bid strategy is MAX_CONVERSIONS -- Apple sets the
     bids. A keyword-bid write is accepted with HTTP 200 and then IGNORED, so this
     would report success and change nothing. Change the ad group's bid strategy
     first, in the Apple Ads UI.
```

Switching an ad group to `MANUAL_CPT` is a campaign-strategy decision, not a
scripting one, so the server will not do it for you. Check with `list_ad_groups`
— the `default_bid` column comes from `bidStrategy.bid`.

This is also why *every* write verifies its read-back rather than trusting the
status code; see [The ledger](#the-ledger).

### Money

Always a **decimal string in major units**: `"1.20"` is one dollar twenty. Never
cents as an integer — `"120"` would be a hundredfold error, and the argument is
rendered verbatim in the prompt a human approves. A `max_bid` ceiling catches it
as a second line of defence.

### Registration

Add it to the global `mcpServers` block of `~/.claude.json`:

```json
"apple-ads": {
  "command": "/Users/you/dev/apple-ads-api/venv/bin/python",
  "args": ["/Users/you/dev/apple-ads-api/apple_ads_mcp/server.py"]
}
```

or equivalently:

```bash
claude mcp add --scope user apple-ads -- \
    /Users/you/dev/apple-ads-api/venv/bin/python \
    /Users/you/dev/apple-ads-api/apple_ads_mcp/server.py
```

`--scope` takes `local`, `user` or `project`. Use `user`. Avoid `project`: it
writes a committed `.mcp.json`, and this repo is public, so that would publish
absolute paths out of somebody's home directory for no benefit — everyone needs
their own venv and their own credentials regardless.

**Absolute interpreter, absolute script path** — never a bare `python3`, and not
`-m`: the launching process's PATH and cwd are not yours.

Credentials stay in `.env` (mode 600) and **not** in the registration's `env`
block. `~/.claude.json` is a config file that gets backed up, copied between
machines and pasted into issues; `.env` is already the one place credentials
live, and splitting them across two files means rotating them in two places.

Check it without restarting anything — this runs a real connection attempt:

```bash
claude mcp list | grep apple-ads
# apple-ads: /…/venv/bin/python /…/apple_ads_mcp/server.py - ✔ Connected
```

A server registered mid-session shows `Connected` here while its tools are still
absent from the session you are in. That is expected: the tool list is read at
client startup, so restart Claude Code and `/mcp` will show all 36.

> **The registration points at a path, not at a branch.** It is whatever is
> checked out at that path right now. Merging changes nothing, but checking out
> a branch from before `apple_ads_mcp/` existed makes the server disappear with
> no error that mentions git.

### Permissions: allowlist every `preview_*`, never an `apply_*`

This is the step that makes the preview/apply split pay off, and it is easy to
skip. Without it every read prompts too, and a human clicking through twenty
harmless prompts is being trained for the one that matters.

Rules key on the tool name, as `mcp__apple-ads__<tool>`, in the
`permissions.allow` array of `~/.claude/settings.json` (or a project
`.claude/settings.json`):

```json
{
  "permissions": {
    "allow": [
      "mcp__apple-ads__whoami",
      "mcp__apple-ads__list_campaigns",
      "mcp__apple-ads__get_campaign",
      "mcp__apple-ads__list_ad_groups",
      "mcp__apple-ads__get_ad_group",
      "mcp__apple-ads__list_keywords",
      "mcp__apple-ads__get_keyword",
      "mcp__apple-ads__list_negative_keywords",
      "mcp__apple-ads__list_shared_budgets",
      "mcp__apple-ads__campaign_report",
      "mcp__apple-ads__ad_group_report",
      "mcp__apple-ads__keyword_report",
      "mcp__apple-ads__search_term_report",
      "mcp__apple-ads__keyword_suggestions",
      "mcp__apple-ads__budget_recommendations",
      "mcp__apple-ads__list_recent_changes",
      "mcp__apple-ads__get_guardrails",
      "mcp__apple-ads__list_my_changes",
      "mcp__apple-ads__reconcile_ledger",

      "mcp__apple-ads__preview_keyword_bid",
      "mcp__apple-ads__preview_keyword_bids_bulk",
      "mcp__apple-ads__preview_keyword_status",
      "mcp__apple-ads__preview_ad_group_status",
      "mcp__apple-ads__preview_campaign_status",
      "mcp__apple-ads__preview_campaign_daily_budget",
      "mcp__apple-ads__preview_negative_keywords_add",
      "mcp__apple-ads__preview_negative_keyword_pause"
    ]
  }
}
```

Nine tool names are **deliberately absent** — the eight `apply_*` and
`revert_change`. Every one of them stops and asks, with its preview sitting
directly above the prompt.

Two things not to do:

- **Do not use a blanket `mcp__apple-ads` entry.** It approves the whole server,
  writes included, which is the opposite of the point.
- **Do not assume `mcp__apple-ads__preview_*` works.** Whether that wildcard is
  honoured depends on your Claude Code version, and the failure is silent — the
  prompt simply keeps appearing. The names are listed out above so there is
  nothing to guess. If you do try the wildcard, confirm a preview runs without
  prompting before relying on it.

---

### Reading data: Apple's filter rules are narrower than they look

The read tools hide this, but anyone extending them will hit it. Verified against
the live API, not the SDK's models:

| Call | What it actually accepts |
| --- | --- |
| `apps_campaign_reports` | **no filter at all** — even `campaignId` is rejected |
| `apps_keyword_reports`, `apps_ad_group_reports`, `apps_search_term_reports` | `campaignId` and **nothing else**; `keywordId` and `adGroupId` are rejected with `INVALID_FIELD_ATTRIBUTE` |
| any report filter | `EQUALS` with a scalar. `IN` is refused for API users |
| `apps_search_term_reports` | `GRAND_TOTAL` **or** granular rows, never both |
| `negative_keywords_query_post` | requires an `adGroupId` condition. A `campaignId` filter alone is a 400 |
| `query_audit_summary` | `eventTime` as a `BETWEEN` range **and** a required `entityType`, one type per call |

So the API call is scoped by campaign (or not at all) and every other narrowing
happens client-side, over the rows that come back. `keyword_report(ad_group_id=…)`
works; it just filters after fetching, and drops the grand total because Apple's
covers the whole campaign rather than the subset you asked for.

Two consequences worth knowing:

- **Campaign-level negative keywords are not listable.** They belong to no ad
  group, and the query endpoint demands one. `list_negative_keywords` sweeps a
  campaign's ad groups, so it finds everything attached to an ad group and
  nothing attached to the campaign itself. Check the Apple Ads UI if you need
  certainty.
- **Reports are parsed from raw JSON**, not through the SDK's response models.
  `apple-ads-platform` 1.109.0 generates `ReportingKeyword.status` with the enum
  `('ACTIVE','PAUSED','DELETED')` while the live API returns `ENABLED`, and the
  generated validator *raises* rather than falling back to its own
  `unknown_default_open_api` placeholder — so the whole keyword report fails to
  deserialize with the response sitting there intact. The tools reshape reports
  into their own row type anyway, so reading the JSON directly costs nothing and
  stops the server being hostage to one wrong enum in generated code.

---

## Guardrails and the ledger

### Bounds

`guardrails.toml` is **committed on purpose**. Every limit is also a code
constant in `apple_ads_mcp/guardrails.py`; the file only overrides them. The
point of committing it is that raising a limit becomes a visible diff with an
author and a date, rather than an env var someone exported once and nobody can
find. A misspelled key is an error at startup, not a silently ignored line.

The per-session counters matter more than the per-call ones. A `max_bid` ceiling
does nothing to stop a runaway loop making 400 individually-legal changes;
`max_applies_per_session` and `max_projected_session_delta` do. They live in
memory and reset only when the server restarts — a counter that survived a
restart would make an ordinary new session start out already half-spent.

`ALLOWED_CAMPAIGN_IDS` pins writes to a set of campaigns. Campaign ids are real
identifiers, so they go in `.env` as `APPLE_ADS_ALLOWED_CAMPAIGN_IDS`, not in the
committed TOML. Pin it to one pilot campaign for the first week, then widen.

`get_guardrails` is a read-only tool, so the agent can learn its own limits
*before* proposing something that will be refused.

### Kill switch

```bash
touch .audit/DISABLE_WRITES          # halts every apply_* within one tool call
echo "paused during the sale" > .audit/DISABLE_WRITES   # with a reason
rm .audit/DISABLE_WRITES             # re-enable
```

A **file**, not an environment variable, because a long-lived stdio server never
sees a variable exported after it started. The check runs inside every `apply_*`
and is never cached.

### The ledger

`.audit/changes.jsonl`, mode 600, `.audit/` gitignored because the entries carry
real entity ids. Append-only, under `flock`, so a concurrent `apple_ads_cli.py`
run cannot interleave.

**Two lines per write**, linked by `entry_id`: an `intent` written *before* the
API call leaves the process, and an `outcome` written after it returns. The pair
is the whole point. A single line written afterwards records only the writes that
came back, and the case that actually needs evidence is the one that did not — a
crash or a timeout, where the mutation may well have landed at Apple's end and
nothing local would ever say so. An `intent` with no `outcome` is the signal
"something may have changed; go and look".

**HTTP 200 does not mean applied.** Every write compares the value Apple echoed
back against the one that was asked for, and records `failed` when they differ.
This is not theoretical: a keyword-bid PUT against an ad group on an automated
bid strategy (`MAX_CONVERSIONS`) returns 200 with the bid unchanged and nothing
in the response saying it was ignored. Bulk writes additionally parse Apple's
per-item `success`/`error` against the `correlationId` set on each request item,
and a mixed result is recorded as `partial`, never as a bare success.

### Undo, and finding out what else changed

`revert_change(entry_id)` writes the old value back. It refuses unless the entry
is recorded as `applied`, refuses an entry already reverted, and re-reads the
entity first: if its current value is no longer what the entry said it was left
at, somebody else has moved it since and reverting would silently overwrite
*their* change. It appends a new entry rather than erasing the old one.

`reconcile_ledger(days)` compares the local ledger with Apple's own audit trail
and reports two classes:

- `local_without_apple` — a write we recorded that Apple has no record of
- **`apple_without_local`** — the valuable one: changes Apple recorded that this
  server did **not** make, which means something else moved the account. Apple
  labels them, so a person in the Apple Ads UI shows up as `userType: CUSTOMER`
  against the server's `CUSTOMER_API`.

The join is on entity id, which Apple exposes in each audit summary's `metas`.
It runs on demand rather than per write: Apple's change *detail* endpoint needs a
composite `EntityType.entityId.txnId` you cannot construct without querying the
summary first, so making it a per-write dependency would double the cost of every
write.

---

## Writing to the account from the CLI (break-glass)

The MCP server above is the normal path. `apple_ads_cli.py` is the other half of
that bargain: the server exposes a deliberately narrow surface, so when you need
something it refuses to expose — create a campaign, delete a negative keyword,
call an endpoint nobody has wrapped — you reach for this instead of widening the
agent's surface.

What makes that safe is **who is driving**. The MCP server is reachable by an
agent and has to assume the caller may be wrong, which is why it has preview
tokens, guardrails and a ledger. This is a shell command: it lives in a human's
scrollback, under a human's hands, and its guard is the one an agent could
trivially defeat — a flag.

That default matters more here than on most APIs: Apple runs **no sandbox** for
campaign management. There is no test org and no staging account. The only thing
these credentials can point at is the live advertising account, where campaigns
are serving and spending today.

Two flags gate a write, and they answer different questions:

| Flags | Means |
| ----- | ----- |
| *(neither)* | Dry run — print the exact request, send nothing |
| `--apply` | "I meant to write" |
| `--apply --confirm-live` | "I meant to write to a campaign that is spending money today" |

```bash
# Dry run: exactly what would be sent, no network call
./venv/bin/python apple_ads_cli.py campaigns -X POST -d @new-campaign.json

# Send it
./venv/bin/python apple_ads_cli.py campaigns -X POST -d @new-campaign.json --apply

# Pause a campaign that is currently serving — needs the second flag
./venv/bin/python apple_ads_cli.py campaigns/1234567890 -X PUT \
    -d '{"status":"PAUSED"}' --apply --confirm-live
```

Before applying a write whose path names an existing campaign, the CLI reads that
campaign back and refuses if its `displayStatus` is `RUNNING`, unless
`--confirm-live` is also present. `POST /campaigns` creates a new campaign and
matches no existing id, so it is not gated on that.

`POST` on its own does not mean "write". This API uses POST for every
query-by-selector and every report, and all of them end in `/query` —
`campaigns/query`, `reports/apps/campaigns/query`, `change-history/query`. So the
CLI classifies by path as well as by method:

```python
PUT / PATCH / DELETE   -> always a mutation
POST                   -> a mutation UNLESS the path ends in /query
GET                    -> never
```

That one rule covers all 80 resource paths in the SDK. (v5 needed two patterns to
say the same thing: a `reports/` prefix *or* a `/find` suffix.)

`/acls` and `/me` are the only endpoints that take **no** context header — they
answer "who is this credential and what can it reach", which is exactly the
question you ask when the context itself is what you doubt. Note the path is
`/acls`, not `/me/acls`.

The role on the credential has the last word: a write succeeds only if the ACL
role allows it. `./venv/bin/python apple_ads_cli.py acls` prints the role.

---

## Rotation and expiry



### Re-signing the client secret (routine — every 180 days)

Nothing to do in the Apple UI. The key pair and the three `.env` values are
unchanged; only the JWT's `iat`/`exp` move:

```bash
./venv/bin/python test_platform_connection.py
```

That re-signs the secret and fetches a new access token in one step. To check
how much life the current secret has:

```bash
python3 generate_client_secret.py     # prints "valid for N days"
```

Worth a calendar reminder ~2 weeks before the 180 days elapse.
`test_platform_connection.py` also warns when fewer than 14 days remain.

### Rotating the key pair (only if the key is compromised or the clientId is lost)

```bash
# 1. New pair, alongside the old one
openssl ecparam -genkey -name prime256v1 -noout -out private-key.new.pem
openssl ec -in private-key.new.pem -pubout -out public-key.new.pem
chmod 600 private-key.new.pem

# 2. Register it: ads.apple.com → Account Settings → API → Generate API Client
#    Paste public-key.new.pem. COPY THE clientId IMMEDIATELY — shown only once.

# 3. Swap the files and update .env with the new clientId / teamId / keyId
mv private-key.new.pem private-key.pem
mv public-key.new.pem  public-key.pem
./venv/bin/python test_platform_connection.py

# 4. Only once that passes: delete the OLD API client in the Apple Ads UI.
```

Verify a pair matches without ever printing the private key:

```bash
openssl ec -in private-key.pem -pubout 2>/dev/null | openssl dgst -sha256
openssl dgst -sha256 public-key.pem      # the two digests must be identical
```

---



## Troubleshooting


| Symptom                                               | Cause                                                                                                                                                                                               |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ModuleNotFoundError: No module named 'cryptography'` / `'mcp'` / `'apple_ads_platform'` | Wrong interpreter. Run everything as `./venv/bin/python …`, or `conda activate apple-ads`. If `cryptography` is older than 50, `pip install -r requirements.txt` to fix the floor.                |
| `invalid_client` from the token endpoint              | Secret past its 180 days (most likely); or `sub`/`iss` swapped; or the API client was revoked. `generate_client_secret.py` shows the days remaining.                                                |
| API section missing at ads.apple.com                  | The account needs the **Account Admin** or **API Account Manager** role on the org.                                                                                                                 |
| clientId lost                                         | Not recoverable — generate a new API client (full key rotation above).                                                                                                                              |
| 401 on an API call                                    | Handled automatically: the client force-refreshes the token and retries exactly once. A second 401 is a real credential problem.                                                                    |
| Works for `/acls`, fails for everything else          | Missing or wrong `X-AP-Context`. `/acls` and `/me` are the only endpoints that do not take it — which is why the health check calls `/acls` first, so a bad adAccountId cannot masquerade as a bad credential. |
| Empty report, no error                                | Apple only reports days with delivery. No spend in the window is a valid empty response, not a failure.                                                                                             |
| A metric column is blank for every row                | A wrong metric field name. Apple returns absent keys silently rather than erroring — see the reconciliation caveats below.                                                                              |

### MCP server

| Symptom                                                        | Cause                                                                                                                                                                                                 |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ConfigError: .env has no value for APPLE_ADS_AD_ACCOUNT_ID`    | Expected the first time. Run `test_platform_connection.py` — step 5 reads it back from Apple and prints the line to paste. Nothing guesses it.                                                        |
| Every tool that takes the context header returns 401/403, but `whoami` works | The `X-AP-Context` is wrong. `whoami` is the only call that sends none. Check the trailing semicolon: `adAccountId=<id>;`                                                                 |
| `apply_*` says `preview_token … is unknown or already used`     | Tokens are single-use and expire after 10 minutes. Re-run the preview. This is working as intended, not a bug.                                                                                        |
| `apply_*` says the value "is now X, but the preview recorded Y" | The TOCTOU guard. Something changed the entity between the preview and the approval — often a person in the Apple Ads UI. Re-run the preview and look at the new numbers before deciding again.       |
| `applied: false` with "Apple returned success and left the value alone" | A write Apple accepted and discarded. For a keyword bid this is almost always an automated bid strategy — see [A write can be in scope and still be a no-op](#a-write-can-be-in-scope-and-still-be-a-no-op). |
| A preview shows `projected_daily_spend_delta: 0.00` with a warning about "0 by default, NOT by measurement" | The 7-day report for that entity failed, so the projection has no data behind it. The warning carries Apple's reason. Treat the zero as unknown, not as "spends nothing". |
| `writes are disabled: …`                                        | The kill switch. `rm .audit/DISABLE_WRITES`.                                                                                                                                                         |
| `session limit reached: N applies already made`                 | The per-session counter. Check what those changes were with `list_my_changes`, then restart the server to reset it.                                                                                   |
| Server starts but `/mcp` lists no tools                         | Claude Code was not restarted after the registration was added to `~/.claude.json`. The absolute interpreter and script path are both required.                                                       |


---



## Files


| File                        | Purpose                                                                                                                                                                              |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `private-key.pem`           | EC P-256 signing key, mode 600. **Never commit, print or transmit.**                                                                                                                 |
| `public-key.pem`            | Registered with Apple. Safe to share.                                                                                                                                                |
| `.env`                      | Every credential **and every real id**. Gitignored — see the environment table above.                                                                                                |
| `.env.example`              | The committed template: key names and empty values, never a real id.                                                                                                                 |
| `environment.yml`           | Conda environment definition.                                                                                                                                                        |
| `requirements.txt`          | Pinned from `pip freeze`. `cryptography` must stay **>= 50**.                                                                                                                         |
| `guardrails.toml`           | **Committed.** Write bounds for the MCP server, so raising a limit is a visible diff.                                                                                                |
| `generate_client_secret.py` | Owns `.env` parsing for the whole repo, and prints the days left on the client secret.                                                                                              |
| `apple_ads_cli.py`          | Break-glass CLI — any endpoint, human-driven. Dry run by default; writes need `--apply` (and `--confirm-live` on a serving campaign).                                                 |
| `test_platform_connection.py` | 7-step end-to-end verification; step 5 discovers `APPLE_ADS_AD_ACCOUNT_ID`.                                                                                                        |
| `apple_ads_mcp/`            | The `apple-ads` MCP server. `config` / `client` / `guardrails` / `ledger` / `previews` / `tools_read` / `tools_write` / `server`.                                                     |
| `tests/test_tool_surface.py` | Pins the registered tool-name set, so the write surface cannot widen by accident.                                                                                                   |
| `fetch_campaign_report.py`  | Daily campaign performance → table and CSV.                                                                                                                                          |
| `fetch_ad_structure.py`     | Campaign → ad group → keyword tree, as a name lookup.                                                                                                                                |
| `resolve_attribution.py`    | Joins `attribution_apple_search_ads` rows to that tree, with a verification pass.                                                                                                    |
| `_bootstrap.py`             | Safety net: if the active interpreter is missing the dependencies, re-execs the script under a local `venv/` should one exist. The MCP server deliberately does **not** call it.     |
| `.audit/changes.jsonl`      | The MCP server's append-only write ledger, mode 600. Gitignored. `.audit/DISABLE_WRITES` is the kill switch.                                                                          |


`.gitignore` excludes `*.pem`, `.env*` (except `.env.example`), `venv/`,
`reports/` and `.audit/`. Before any commit, confirm with `git status`
that no key, `.env`, report or ledger is staged; `git check-ignore -v <path>`
shows which rule covers a given file.

**This repository is public.** No real identifier belongs in a tracked file — not
the org id, not the ad account id, not a campaign id, and not the company name.
None of them is a credential (all are inert without `private-key.pem`), but they
have no reason to be committed either. They live in `.env`, which is gitignored;
`.env.example` carries the key names with empty values. Nothing in the code
defaults any of them, so a missing value is a loud `ConfigError` rather than a
silent substitution.

---

