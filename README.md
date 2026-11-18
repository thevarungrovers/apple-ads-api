# Apple Ads API  [![M8ven Score](https://m8ven.ai/badge/mcp/thevarungrovers-apple-ads-api-1859fz?v=c6361b979cbba3ee16039ff9702045bb)](https://m8ven.ai/mcp/thevarungrovers-apple-ads-api-1859fz?s=readme)

Let an AI agent look at your Apple Ads account — and change it, but only after a
human says yes.

It is an **MCP server**. You add it to Claude Code, Claude Desktop, Cursor or
Codex, and then you can ask things like:

> "Which keywords spent the most last week and got nothing back?"
>
> "Show me what raising this bid to $2.50 would cost per day."

It can also change bids, budgets, and what is paused — but **never on its own**.
Every change is shown to you first, and you approve it.

---

## The big idea, in one picture

```
   YOU ask for something
        |
   1.  READ      the agent looks at the account          (safe, no changes)
        |
   2.  PREVIEW   the agent shows you exactly what would  (safe, no changes)
                 change, and what it would cost per day
        |
   3.  YOU SAY YES                                        <-- this is the gate
        |
   4.  APPLY     the change happens, and is written down
```

**Step 3 cannot be skipped or automated away.** The "preview" and the "apply" are
two different tools with two different names, so you can tell your agent *"always
allow previews, never allow applies"*. If it were one tool with a `safe=true`
switch, clicking "always allow" once on a harmless preview would hand over every
future real change.

---

## Why this is careful

**There is no practice mode.** Apple does not give you a test account for this.
The only account these credentials can reach is the real one, where ads are
running and money is being spent right now. A mistake costs money today.

So the server is built to assume the caller might be wrong.

---

## Install it

You need **Python 3.12 or newer**.

```bash
# 1. Get the code and its dependencies
git clone https://github.com/thevarungrovers/apple-ads-api.git
cd apple-ads-api
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 2. Fill in your Apple credentials (and, optionally, the two naming keys:
#    APPLE_ADS_OWNER_INITIALS / APPLE_ADS_AGENT_NAME -- see "Naming what the
#    agent creates" below)
cp .env.example .env
$EDITOR .env

# 3. Check it works, and find your ad account id
./venv/bin/python test_platform_connection.py

# 4. Add it to your AI agent
python3 install.py
```

Step 4 asks you two questions:

**Which agent?** Claude Code, Claude Desktop, Cursor, or Codex.

**Basic permissions only?** Say **Y** if you want this agent to look but not
touch. What that means depends on the agent:

| Agent | Saying Y does this |
|---|---|
| Claude Code | The 71 read and preview tools stop asking permission. The 32 that change things still ask, every time, and the agent may never run the break-glass CLI with `--apply` |
| Claude Desktop, Cursor, Codex | The 32 tools that change things are not even given to the agent. It cannot call them |

The difference is not a choice — Claude Code is the only one of the four that can
be told "allow this tool, ask about that one". For the others the limit has to
live in the server.

Then **restart your agent**. It reads the tool list once, at startup.

```bash
python3 install.py --dry-run                 # show what it would write, write nothing
python3 install.py --agent cursor --read-only --yes   # no questions
```

It backs up every file before changing it, keeps your other settings, and running
it twice does nothing the second time.

---

## DOs and DON'Ts

### Setting it up

- **DO** run `test_platform_connection.py` before anything else. It checks the
  whole chain and tells you your ad account id.
- **DO** keep `private-key.pem` and `.env` to yourself. They are already
  gitignored.
- **DO** use the full path to `venv/bin/python` in any config. `install.py` does
  this for you.
- **DON'T** put your credentials in your agent's config file. They live in `.env`,
  one place, so rotating them is one edit.
- **DON'T** guess your ad account id. It is **not** the same as your org id, and
  writing to the wrong account is not something Apple will stop.
- **DON'T** commit anything real — no account id, no campaign id, no company
  name. This repo is public.

### Letting an agent use it

- **DO** answer **Y** to "basic permissions only" the first time.
- **DO** read the preview before you approve. The numbers are there to be read.
- **DO** set `APPLE_ADS_ALLOWED_CAMPAIGN_IDS` in `.env` to pin changes to one
  campaign while you are getting comfortable.
- **DON'T** allow a tool whose name starts with `apply_`.
- **DON'T** use a blanket `mcp__apple-ads` permission rule. It approves
  everything, including the money.
- **DON'T** rely on `mcp__apple-ads__preview_*` as a wildcard. Whether it works
  depends on your version, and when it does not work it just quietly keeps
  asking. List the names instead — `install.py` writes them out.

### Making changes

- **DO** write money as a plain decimal: `"1.20"` is one dollar twenty.
- **DO** check the `applied` field after a change. It is the only thing that
  says it really happened.
- **DO** note the `entry_id`. It is how you undo.
- **DON'T** write money as cents. `"120"` instead of `"1.20"` is a hundred times
  too much, and it is the number you will be shown to approve.
- **DON'T** approve a preview with a `FAIL` line in it. The apply will refuse it
  anyway.
- **DON'T** trust "no error" as "it worked". See the next section.

---

## The one surprise worth knowing

**Apple can say "OK" and change nothing.**

If an ad group is set to let Apple pick the bids (`MAX_CONVERSIONS` or
`MAX_ENGAGEMENTS`), then a bid you write is accepted with a success response and
quietly thrown away. Nothing in the reply says so.

This repo handles it in two places:

1. The **preview** checks the bid strategy first and blocks:

   ```
   FAIL bid_is_settable: ad group bid strategy is MAX_CONVERSIONS -- Apple sets the
        bids. A write to the ad group default bid is accepted with HTTP 200 and then
        IGNORED, so this would report success and change nothing. Change the ad
        group's bid strategy first, in the Apple Ads UI.
   ```

2. Every change **reads the value back** and compares it to what was asked for.
   If they differ it is recorded as **failed**, not applied.

Bids only mean something under `MANUAL_CPT` or `MANUAL_CPM`. Changing that is a
decision a person makes in the Apple Ads website; this server will not do it.

---

## What the agent can do

**104 tools: 40 that read, 31 preview/apply pairs, undo, and settling a timed-out change.** Almost anything
you would do by hand in the Apple Ads website -- except delete things.

### Can read (safe)

The account (timezone, currency, your role), campaigns, ad groups (with their full
targeting), keywords, negative keywords, ads, creatives, custom product pages and
shared budgets. Performance by campaign, ad group, keyword, ad and search term,
split by country, device, age or gender and by day. Impression share and search
term popularity. Apple's keyword, phrase and category suggestions, and its budget
and CPA recommendations. The ids the writes need: locations, apps, app
eligibility, languages. Everyone's recent changes, field by field. Its own limits,
its own change history, and a reconciliation of the two.

Every list and report says how many rows exist and whether it showed them all.

`list_campaigns`, `list_ad_groups`, `list_ads` and `list_creatives` say who each
name says made it (`origin`, `owner`, `agent`, `base`, and `ledger_created` when
this checkout created it) and filter on it: `origin` (`any` / `agent` / `cli` /
`manual`), `agent_name`, `owner`, and `mine`. See
[Naming what the agent creates](#naming-what-the-agent-creates).

### Can change (needs your yes)

Every change is a `preview_<name>` / `apply_<name>` pair.

| What | `<name>` |
|---|---|
| A keyword's bid, one or many | `keyword_bid`, `keyword_bids_bulk` |
| Pause or enable a keyword, ad group, campaign or ad | `keyword_status`, `ad_group_status`, `campaign_status`, `ad_status` |
| An ad group's default bid | `ad_group_default_bid` |
| A campaign's daily budget | `campaign_daily_budget` |
| Add keywords (**created paused**) | `add_keywords` |
| Change a keyword's match type (adds a paused copy, pauses the old) | `keyword_match_type` |
| Add negative keywords; pause or enable them | `negative_keywords_add`, `negative_keyword_pause`, `negative_keyword_status` |
| Bid strategy and goal; CPA goal; campaign start and end | `ad_group_bid_strategy`, `campaign_bid_strategy`, `ad_group_cpa_goal`, `campaign_schedule` |
| Targeting: Search Match, age and gender, devices, hours, locations, customer type, countries | `ad_group_search_match`, `ad_group_demographics`, `ad_group_devices`, `ad_group_daypart`, `ad_group_locations`, `ad_group_app_downloaders`, `campaign_countries` |
| Create a campaign, ad group, creative or ad (**created paused**, **named with your prefix**) | `create_campaign`, `create_ad_group`, `create_creative`, `create_ad` |
| Act on Apple's budget or CPA recommendation (normal limits apply) | `budget_recommendation`, `target_cpa_recommendation` |
| Rename something the agent created (the prefix is kept), or dismiss a recommendation | `low_risk_change` |
| Undo one change, or a whole batch | `revert_change` |
| Settle a timed-out change: did it land? (reads Apple, writes only the ledger, undoes nothing) | `resolve_unknown_change` |

**Created things start paused.** A new campaign, ad group, keyword or ad spends
nothing until a separate, previewed enable -- and that enable is counted against
the day's spend limit at its full budget. Negative keywords are the exception:
they are created enabled, because a paused negative does nothing and a negative
can only reduce traffic.

### Cannot do at all

**Delete anything** -- campaigns, ad groups, ads, creatives, keywords, negative
keywords. Pausing is the undo. Also: changing shared budgets, billing or account
settings, location groups and Maps (brand) ads, uploading assets, and applying
Apple's recommendations in one blind call.

This is not a rule someone has to remember. **There is no general-purpose "call
any endpoint" tool**, so anything without its own tool simply cannot be reached.
A test pins the exact list of tools, and another the list of API methods that
must stay unreachable, so neither can grow by accident.

If you genuinely need one of those, there is a command-line escape hatch further
down -- for a human to run. The agent is told to hand you the command, and it
cannot run it itself.

---

## Naming what the agent creates

A month after the agent created a campaign, nobody can tell it from one made by
hand. So everything the agent creates is named after who made it:

```
<OWNER>-<agent>-<base name>
AB-max-spring_promo_week_12          <- placeholder values
```

Put two keys in this checkout's `.env`:

| Key | Format | Example |
|---|---|---|
| `APPLE_ADS_OWNER_INITIALS` | 2-4 uppercase letters | `AB` |
| `APPLE_ADS_AGENT_NAME` | lowercase, 2-24 of `a-z 0-9 _`, starting with a letter, no `-`; `cli` and `manual` are reserved | `max` |

Either can be left empty -- that part is simply skipped, never an error:

| `.env` has | The agent asks for `spring_promo`; Apple gets |
|---|---|
| both | `AB-max-spring_promo` |
| only the initials | `AB-spring_promo` |
| only the agent name | `max-spring_promo` |
| neither | `spring_promo`, exactly as given |

- **A value that is set but malformed** (initials `a1`, an agent name with a
  `-`) blocks every creation at the preview, and the message names the `.env`
  key to fix.
- **`.env` is the only source.** No tool takes an owner or agent argument, and
  the process environment (a client's `env` block) is ignored -- the server warns
  at startup if one disagrees with `.env`. A `config.env` beside it is read too;
  a non-empty `.env` value wins, and an empty one leaves `config.env`'s in place.
  **One identity per checkout**: a second agent needs its own checkout with its
  own `.env`.
- **The prefix goes first**, before any house naming style you use: the
  house-style name is the base. The agent passes the base; the prefix is added
  for it, never twice (`AB-max-AB-max-x` is refused, not kept).
- **A name that reads as someone else's prefix is always refused** -- another
  agent's (`CD-bot-...`), the escape hatch's (`AB-cli-...`), or one this
  checkout's initials-only prefix would turn into another agent's -- **even
  when no identity is configured**: taken as given, it would impersonate them.
- **Invisible characters are refused**: control characters (tab, newline,
  NUL), zero-width and bidi marks, the byte-order mark, and line or paragraph
  separators.
- **The preview shows the exact name** that will be sent (`entity_name`, `path`
  and a `naming` block with the requested name, the final one, the prefix and
  the length), and warns when it differs from what was asked.
- **Too long is refused, never cut short.** Apple has no validate-only mode, so
  a name it rejects would only be found out after you approved it. The cap is
  **200 characters for the whole name, prefix included**, for campaigns, ad
  groups, ads and creatives -- a conservative figure until Apple's current limit
  is confirmed.
- **A campaign name already used** in the account, or an ad group name already
  used in its campaign, fails the preview (`name_is_free`) -- for a creation and
  for a rename alike. So does a scope with more entities than the scan reads
  ("could not verify the name is free"). A duplicate ad or creative name is a
  warning.

**What gets a prefix:** campaigns, ad groups, ads and creatives. Keywords and
negative keywords have no name field (only their text), so nothing is added to
them.

**Renames:** the agent may rename only **its own** entities -- the name carries
this checkout's prefix, or the change log shows this server created it. A
hand-made entity, or another agent's, is refused, with no override.
"Carries this checkout's prefix" means the whole prefix: with only the initials
`AB` set, `AB-launch` is ours but `AB-bot-launch` (agent `bot`'s) and
`AB-cli-launch` (a person's, through the escape hatch) are not. The same rule
decides `mine=True` and the `reconcile_ledger` naming section. The prefix
is kept (the agent changes only the base), and an entity created here whose
prefix was removed in the website gets it back. Undoing a rename restores the
old name exactly.

**The escape hatch** names its creations and renames `<OWNER>-cli-<base>` when
the initials are set (`AB-cli-launch`), and leaves the name as given when they
are not: `cli` only separates a person from an agent under the same initials.
It refuses a create or update path carrying `?` or `#` (`campaigns?`), which
would otherwise slip past the stamp, and takes no abbreviated flags: `--appl`
is not `--apply`.

**Finding them:** the list tools filter by `origin` (`agent`, `cli` or `manual`,
read from the name), `agent_name`, `owner`, or `mine=True` (this checkout's
prefix). Each row also says `ledger_created` -- the name is a convention, the
change log is the proof. When a prefix is configured, `reconcile_ledger` adds a
`naming` section: `prefix_lost` (created here, the name no longer carries the
prefix -- renamed in the website), `prefix_without_ledger` (carries the prefix
but this change log has no record of creating it), and other prefixes counted.
Nothing there is changed automatically. Existing entities are left as they are.

---

## What a preview tells you

```json
{
  "entity_name": "competitor brand term",
  "path": "Campaign 'Search' > AdGroup 'Brand' > Keyword 'competitor brand term'",
  "before": "1.20 CAD",
  "after": "2.50 CAD",
  "pct_change": "+108.3%",
  "entity_serving_status": "RUNNING",
  "projected_daily_spend_delta": "18.57",
  "guardrail_checks": ["OK  max_bid: 2.50 CAD vs ceiling 5.00 CAD", "..."],
  "blocked": false,
  "warnings": ["This keyword IS SERVING TODAY -- the change takes effect on live traffic."]
}
```

Four fields do the real work:

| Field | Why it matters |
|---|---|
| `entity_name` | "competitor brand term" means something. `keyword 1234567890` does not |
| `path` | Where it sits, in words |
| `entity_serving_status` | Whether **this** thing can spend money today. A keyword inside a paused ad group cannot, even if the campaign looks fine |
| `projected_daily_spend_delta` | Roughly how much more per day |

**`projected_daily_spend_delta` is for catching a typo, not for forecasting.** It
assumes traffic stays the same, which is exactly what a bid change is meant to
alter. Budget changes are the exception — there the number is exact, because the
budget *is* the number.

The token the preview gives you:

- works **once**
- expires after **10 minutes**
- is refused if anybody changed the value in the meantime

That last one is not hypothetical. It has already caught a person editing a bid
in the Apple Ads website while an agent was mid-conversation about it.

---

## Safety net

Four layers, weakest first.

### 1. Limits per change

In `guardrails.toml`, which is **committed on purpose** — raising a limit should
show up as a change somebody can see, not an environment variable set once and
forgotten.

| Limit | Default |
|---|---|
| `max_bid` | 5.00 |
| `max_bid_increase_pct` | 50 |
| `max_daily_budget` | 150.00 |
| `max_budget_change_pct` | 25 |
| `max_entities_per_call` | 25 |
| `max_projected_delta_per_day` | 50.00 |
| `max_cpa_goal` | 20.00 |
| `max_cpa_goal_change_pct` | 50 |

**Enabling something is the exception.** Re-enabling a paused campaign can spend
its whole daily budget, which is over `max_projected_delta_per_day` on any real
campaign -- so an enable (or anything else that widens delivery: pausing a
negative keyword, switching Search Match on, loosening targeting, undoing a
pause) skips that one line and is counted in full against the daily total below.

### 2. Limits per conversation

| Limit | Default |
|---|---|
| `max_applies_per_session` | 20 |
| `max_projected_session_delta` | 150.00 |

**These are the ones that matter.** A per-change limit does nothing against a
loop making 400 perfectly legal changes. These do. They reset only when the
server restarts.

And per day, shared by every agent session and the CLI on this machine, counted
from the change log so a restart does not reset them. The day starts at your
local midnight.

| Limit | Default |
|---|---|
| `max_applies_per_day` | 40 |
| `max_projected_delta_per_day_total` | 200.00 |

A batch counts once; creating something counts three. Keep the daily total at
least as big as your largest campaign budget, or that campaign can never be
re-enabled from here (the server warns at startup).

### 3. Pin it to one campaign

```bash
# in .env
APPLE_ADS_ALLOWED_CAMPAIGN_IDS=1234567890
```

Anything outside the list is refused before you are ever asked about it. Empty
means every campaign is allowed.

### 4. The stop button

```bash
touch .audit/DISABLE_WRITES                            # stop all changes, now
echo "paused for the sale" > .audit/DISABLE_WRITES     # with a reason
rm .audit/DISABLE_WRITES                               # start again
```

A **file**, not a setting, because a server that is already running will never
notice a setting you change afterwards. It will notice the file, on the very next
change it is asked to make.

And separately, **read-only mode**: start the server with
`APPLE_ADS_READ_ONLY=1` and the change tools are not published at all. This is
what `install.py` uses for Claude Desktop, Cursor and Codex.

### Ask it about itself

```
get_guardrails
```

Returns the limits, how much of the conversation's budget is left, whether the
stop button is pressed, and whether this one is read-only. It is a read tool, so
an agent can check its own limits before suggesting something that would be
refused.

---

## Everything is written down

One file, `logs/apple-ads.db`, gitignored and readable only by you.

| Table | One row per |
|---|---|
| `tool_calls` | Thing the agent asked for |
| `api_calls` | Request sent to Apple, linked to the ask that caused it |
| `changes` | Change made to the account |

```sql
-- the last 20 things the agent did
SELECT t.ts, t.tool_name, t.ok, COUNT(a.id) AS api_calls
FROM tool_calls t LEFT JOIN api_calls a ON a.tool_call_id = t.id
GROUP BY t.id ORDER BY t.ts DESC LIMIT 20;

-- changes that started and never finished  <-- look at these
SELECT entry_id, ts, tool, entity_id FROM changes WHERE outcome IS NULL;
```

**Each change is written down twice**: once *before* the request is sent, and
once after the answer comes back. If the second one is missing, something may
have changed at Apple's end with nothing local to say so. That is the row worth
looking at, and it is the reason for writing the first one at all.

### Undoing something

```
revert_change(entry_id="...")
```

It refuses if:

- the change was not recorded as having worked
- it has already been undone
- somebody else has changed that thing since — undoing would wipe out **their**
  change

Undoing something that was **created** pauses it -- nothing is ever deleted. A
batch (bulk bids, added keywords) can be undone item by item, or all at once by
the batch id the apply returned. Undoing a pause or a budget cut puts spend back,
so it counts against the limits like any enable.

A change whose request timed out is recorded as `unknown`: it may have landed,
so it counts against the limits as if it had. `resolve_unknown_change(entry_id=
"...")` reads the thing back and records it as applied or failed, without
changing anything at Apple. Use it to find out; use `revert_change` only when
you want the change undone. "Applied" is recorded as soon as Apple shows the new
value. "Failed" stops it counting, so it needs more: the change must be at
least 5 minutes old (Apple's read can lag its write -- earlier, it says "too
early to tell" and when to try again), and no later change in the log may have
touched the same field -- a break-glass CLI write whose path or body names the
same id counts too (then the old value proves nothing, and it stays
`unknown`). Undoing a whole batch skips such an item -- it stays `unknown`,
the result's `skipped` says why -- and undoes the items that landed; revert the
skipped one later by its own entry id.

A targeting change whose own dimension landed while Apple's read-back shows
another dimension different (normalised or dropped) is recorded as `partial`:
it is live, it counts against the limits, and `revert_change` puts back only
the dimension that was asked for.

### Finding out what else changed

```
reconcile_ledger(days=7)
```

Compares this server's record with Apple's own. Two answers come back, and the
second is the interesting one:

- `local_without_apple` — we think we changed something Apple has no record of
- **`apple_without_local`** — Apple recorded a change *we did not make*. Usually
  a person in the Apple Ads website

---

## Teaching your agent to use it well

There is a skill file in this repo:

```
.claude/skills/apple-ads/SKILL.md
```

Claude Code picks it up automatically when you work in this folder. To have it
everywhere:

```bash
mkdir -p ~/.claude/skills/apple-ads
cp .claude/skills/apple-ads/SKILL.md ~/.claude/skills/apple-ads/
```

For Cursor or Codex, point them at that file or paste it into their rules.

---

## Running things yourself

```bash
# Does everything work? (7 checks, reads only)
./venv/bin/python test_platform_connection.py

# The whole test suite (no internet, no credentials used, nothing sent)
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/pytest
# ...or any one file as a plain script
./venv/bin/python tests/test_tool_surface.py

# Performance
./venv/bin/python fetch_campaign_report.py --days 30 --csv

# Campaign -> ad group -> keyword, as names
./venv/bin/python fetch_ad_structure.py --save

# Which user came from which keyword
./venv/bin/python resolve_attribution.py reports/rows.csv

# How long before the credential expires
./venv/bin/python generate_client_secret.py
```

From Python, reuse the server's own client instead of writing another one:

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

## The escape hatch

`apple_ads_cli.py` can call **any** Apple Ads endpoint. It exists so the agent's
tool list does not have to cover deletes, shared budgets and billing: when you
need one of those, you do it yourself.

**It is for a human to run, and only a human can.** `--apply` needs an
interactive terminal and the path typed back at a prompt -- an agent's shell has
neither -- and `install.py` adds a Claude Code rule that denies the agent the
command outright. The stop button stops it too, and every write it sends is
recorded in the change log, so it counts against the daily limits.

```bash
# Look at something
./venv/bin/python apple_ads_cli.py acls
./venv/bin/python apple_ads_cli.py campaigns/query -X POST -d '{}'

# Dry run: prints exactly what would be sent, sends nothing
./venv/bin/python apple_ads_cli.py campaigns/1234567890 -X PUT -d '{"status":"PAUSED"}'

# Actually send it (asks you to type the path back)
./venv/bin/python apple_ads_cli.py campaigns/1234567890 -X PUT \
    -d '{"status":"PAUSED"}' --apply --confirm-live
```

| Flags | Means |
|---|---|
| *(none)* | Print what would be sent. Send nothing |
| `--apply` | I meant to write (then type the path to confirm) |
| `--apply --confirm-live` | I meant to write to a campaign that is spending money today |

**Nothing is sent without `--apply`.** The dry run is the thing you read before
you mean it.

A create (`POST campaigns`, `.../adgroups`, `ads`, `creatives`) or an update by
id with a `"name"` in its body gets `<OWNER>-cli-<base>` when
`APPLE_ADS_OWNER_INITIALS` is set, and the dry run prints the final name.

---

## Credentials

Three steps, three lifetimes. Mixing them up is the usual cause of "it worked
yesterday".

| # | What | Lasts | Who handles it |
|---|---|---|---|
| 1 | **Client secret** — signed on your machine from `private-key.pem` | 180 days | `generate_client_secret.py` |
| 2 | **Access token** — Apple gives you one for that secret | 1 hour | The library, automatically |
| 3 | **The API call** itself | one request | `apple_ads_mcp/client.py` |

Apple never sees your private key. It checks your signature against the public
key you registered.

The client secret is never saved to disk. It is cheap to re-create, so it is
re-created in memory each time a token is needed — which means a server running
for months can never go stale.

`.env` holds:

| Variable | Needed | Where it comes from |
|---|---|---|
| `APPLE_ADS_CLIENT_ID` | yes | Shown **once**, when you create the API client |
| `APPLE_ADS_TEAM_ID` | yes | Apple Ads website, API client list |
| `APPLE_ADS_KEY_ID` | yes | Apple Ads website, API client list |
| `APPLE_ADS_ORG_ID` | yes | Apple Ads website, Account Settings |
| `APPLE_ADS_AD_ACCOUNT_ID` | yes | **Discovered**, by `test_platform_connection.py` |
| `APPLE_ADS_DEFAULT_CAMPAIGN_ID` | no | Saves typing a real id in commands |
| `APPLE_ADS_ALLOWED_CAMPAIGN_IDS` | no | Pins changes to these campaigns |
| `APPLE_ADS_OWNER_INITIALS` | no | Your initials, for naming what the agent creates (`AB`) |
| `APPLE_ADS_AGENT_NAME` | no | The agent's name, for the same (`max`) |

> `APPLE_ADS_AD_ACCOUNT_ID` is **not** your org id. Apple returns them as two
> different fields. Do not read it off the website and do not guess —
> `test_platform_connection.py` reads it back from Apple and prints the exact
> line to paste.

> `clientId` and `teamId` are usually the **same** text. Keep them in separate
> settings anyway: if they ever differ, swapping them is invisible to look at and
> only shows up as Apple rejecting you.

### Every 180 days

```bash
./venv/bin/python test_platform_connection.py   # re-signs and re-checks, in one go
./venv/bin/python generate_client_secret.py     # how many days are left
```

It warns you when fewer than 14 days remain. Worth a calendar reminder.

### If the key is lost or compromised

```bash
# 1. New pair, next to the old one
openssl ecparam -genkey -name prime256v1 -noout -out private-key.new.pem
openssl ec -in private-key.new.pem -pubout -out public-key.new.pem
chmod 600 private-key.new.pem

# 2. Register public-key.new.pem at ads.apple.com -> Account Settings -> API
#    COPY THE NEW clientId IMMEDIATELY. It is shown once.

# 3. Swap them in and update .env
mv private-key.new.pem private-key.pem
mv public-key.new.pem  public-key.pem
./venv/bin/python test_platform_connection.py

# 4. Only once that passes: delete the OLD API client at Apple.
```

Check a pair matches without ever printing the private key:

```bash
openssl ec -in private-key.pem -pubout 2>/dev/null | openssl dgst -sha256
openssl dgst -sha256 public-key.pem      # the two must be identical
```

---

## When something goes wrong

| It says | It means | Do this |
|---|---|---|
| `ModuleNotFoundError` | Wrong Python | Use `./venv/bin/python`, not `python3` |
| `ConfigError: no value for APPLE_ADS_AD_ACCOUNT_ID` | Normal, the first time | Run `test_platform_connection.py` and paste the line it prints |
| `invalid_client` | The 180 days ran out, most likely | `generate_client_secret.py` shows the days left |
| `/acls` works, everything else is 401/403 | Wrong account id | Check the trailing semicolon: `adAccountId=<id>;` |
| `preview_token ... unknown or already used` | Tokens work once, for 10 minutes | Run the preview again. Working as intended |
| `... is now X, but the preview recorded Y` | Somebody changed it in between | Run the preview again and look at the new numbers |
| `applied: false`, "left the value alone" | Apple accepted it and ignored it | See [The one surprise](#the-one-surprise-worth-knowing) |
| `writes are disabled` | The stop button | `rm .audit/DISABLE_WRITES` |
| `session limit reached` | 20 changes already this conversation | Check them with `list_my_changes`, then restart the server |
| Server connects but no tools appear | The agent was not restarted | Restart it. The tool list is read once |
| An `apply_*` tool is missing entirely | Read-only mode | Remove `APPLE_ADS_READ_ONLY` from that agent's config |
| Empty report, no error | No spend that day | Apple only reports days with delivery. Not a failure |

---

## For people extending it

Apple's filters are narrower than they look. These are verified against the live
API, not read off the documentation:

| Call | What it actually accepts |
|---|---|
| Campaign reports | **No filter at all**, not even a campaign id |
| Keyword / ad group / search term reports | A campaign id and **nothing else** |
| Any report filter | `EQUALS` with one value. `IN` is refused |
| Search term reports | A grand total **or** detailed rows, never both |
| Negative keyword lookup | Requires an ad group id. A campaign id alone is rejected |
| Change history | A date range **and** one entity type per call |

So the call is narrowed by campaign, or not at all, and everything else is
filtered afterwards in Python.

Two things that follow:

- **Campaign-level negative keywords cannot be listed.** They belong to no ad
  group, and the lookup demands one. Check the Apple Ads website if you need to
  be sure.
- **Not yet exercised against the live account** (built for parity, checked
  only against the SDK's models): impression share, search term popularity,
  phrase and category suggestions, target CPA reads, geo and app search,
  product pages, change details, the report `group_by` / `sort_by` /
  pagination arguments, and every new write. An empty targeting `include`
  list is written to mean "no restriction", which is unverified. Run their
  previews and reads once before relying on them.
- **Reports are read as raw JSON**, not through the library's models. The library
  expects a keyword status of `ACTIVE` while Apple returns `ENABLED`, and it
  raises rather than shrugging — so the whole report fails with the data sitting
  right there. The tools reshape reports anyway, so reading the JSON directly
  costs nothing.

---

## Files

| File | What it is |
|---|---|
| `install.py` | Adds the server to your agent. Standard library only |
| `apple_ads_mcp/` | The server itself |
| `.claude/skills/apple-ads/SKILL.md` | How an agent should use it |
| `guardrails.toml` | The limits. **Committed**, so a change is visible |
| `apple_ads_cli.py` | The escape hatch. For humans |
| `apple_ads_mcp/naming.py` | The `<OWNER>-<agent>-<base>` naming identity, its parser and its rules |
| `test_platform_connection.py` | 7-step health check |
| `apple_ads_mcp/changes/` | One class per kind of change, and the engine they all run through |
| `tests/test_tool_surface.py` | Pins the tool list, the permission hints and the unreachable API methods |
| `tests/kindkit.py` | A fake Apple account the change tests run against |
| `tests/test_*.py` | The rest of the suite: the engine, every change kind, the daily cap, errors, retries, reads, the CLI |
| `CLAUDE.md` | Guidance for an agent working on this repository |
| `generate_client_secret.py` | Signs the credential, and counts down its 180 days |
| `fetch_campaign_report.py` | Performance, as a table or CSV |
| `fetch_ad_structure.py` | Campaign → ad group → keyword, as names |
| `resolve_attribution.py` | Which user came from which keyword |
| `migrate_ledger.py` | One-time move of the old change log into the database |
| `private-key.pem` | Your signing key. **Never share it** |
| `.env` | Your credentials and real ids. Gitignored |
| `logs/apple-ads.db` | Everything that happened. Gitignored |
| `.audit/DISABLE_WRITES` | The stop button, when it exists |

**This repository is public.** No real id belongs in a committed file — not the
org id, not the account id, not a campaign id, not the company name. They all
live in `.env`, which is gitignored. `.env.example` has the names with the values
left empty.

Nothing in the code makes up a default for any of them. A missing value is a loud
error, never a quiet guess.

---

## Licence

MIT. See [LICENSE](LICENSE).
