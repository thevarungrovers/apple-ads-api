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

# 2. Fill in your Apple credentials
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
| Claude Code | The 28 safe tools stop asking permission. The 10 that change things still ask, every time |
| Claude Desktop, Cursor, Codex | The 10 tools that change things are not even given to the agent. It cannot call them |

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

**38 tools: 19 that read, 9 preview/apply pairs, and undo.**

### Can read (safe)

Accounts, campaigns, ad groups, keywords, negative keywords, shared budgets.
Performance reports by campaign, ad group, keyword and search term. Apple's
keyword and budget suggestions. Everyone's recent changes to the account. Its own
limits, its own change history, and a reconciliation of the two.

### Can change (needs your yes)

| What | Tools |
|---|---|
| A keyword's bid | `preview_keyword_bid` / `apply_keyword_bid` |
| Many keyword bids at once | `preview_keyword_bids_bulk` / `apply_keyword_bids_bulk` |
| Pause or enable a keyword | `preview_keyword_status` / `apply_keyword_status` |
| Pause or enable an ad group | `preview_ad_group_status` / `apply_ad_group_status` |
| An ad group's default bid | `preview_ad_group_default_bid` / `apply_ad_group_default_bid` |
| Pause or enable a campaign | `preview_campaign_status` / `apply_campaign_status` |
| A campaign's daily budget | `preview_campaign_daily_budget` / `apply_campaign_daily_budget` |
| Add negative keywords | `preview_negative_keywords_add` / `apply_negative_keywords_add` |
| Pause a negative keyword | `preview_negative_keyword_pause` / `apply_negative_keyword_pause` |
| Undo one change | `revert_change` |

### Cannot do at all

Creating or deleting campaigns, ad groups, ads or creatives. Deleting negative
keywords (pause them instead — it has the same effect and can be undone).
Changing shared budgets. Applying Apple's budget advice in one click.

This is not a rule someone has to remember. **There is no general-purpose "call
any endpoint" tool**, so anything without its own tool simply cannot be reached.
A test pins the exact list of tools, so it cannot grow by accident.

If you genuinely need one of those, there is a command-line escape hatch further
down — for a human to run, not an agent.

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

### 2. Limits per conversation

| Limit | Default |
|---|---|
| `max_applies_per_session` | 20 |
| `max_projected_session_delta` | 150.00 |

**These are the ones that matter.** A per-change limit does nothing against a
loop making 400 perfectly legal changes. These do. They reset only when the
server restarts.

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

It cannot undo "add negative keywords", because that created things and there is
no delete. Pause them instead.

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

# Are the tools still what they should be? (no internet needed)
./venv/bin/python tests/test_tool_surface.py
./venv/bin/python tests/test_logdb.py

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
tool list can stay small: when you need something the server will not do, you do
it yourself instead of widening what the agent can reach.

**It is for a human to run.** Its only protection is a flag, which an agent could
type as easily as you can.

```bash
# Look at something
./venv/bin/python apple_ads_cli.py acls
./venv/bin/python apple_ads_cli.py campaigns/query -X POST -d '{}'

# Dry run: prints exactly what would be sent, sends nothing
./venv/bin/python apple_ads_cli.py campaigns/1234567890 -X PUT -d '{"status":"PAUSED"}'

# Actually send it
./venv/bin/python apple_ads_cli.py campaigns/1234567890 -X PUT \
    -d '{"status":"PAUSED"}' --apply --confirm-live
```

| Flags | Means |
|---|---|
| *(none)* | Print what would be sent. Send nothing |
| `--apply` | I meant to write |
| `--apply --confirm-live` | I meant to write to a campaign that is spending money today |

**Nothing is sent without `--apply`.** The dry run is the thing you read before
you mean it.

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
| `test_platform_connection.py` | 7-step health check |
| `tests/test_tool_surface.py` | Pins the tool list and the permission hints |
| `tests/test_logdb.py` | 31 checks over the log database |
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
