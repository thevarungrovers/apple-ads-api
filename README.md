# Apple Search Ads API

OAuth (client-credentials) access to the Apple Ads **Campaign Management API v5**,
so campaign performance can be pulled programmatically instead of exported by hand.

> **The client secret expires 180 days after it is generated.** That is Apple's
> hard maximum and there is no renewal, no warning, and no grace period. When it
> lapses, the token endpoint returns a bare `invalid_client` that looks exactly
> like a wrong-credentials bug. See [Rotation and expiry](#rotation-and-expiry).

---

## How the authentication works

Three separate steps, each with its own lifetime. Confusing them is the main
source of "it worked yesterday" problems.


| #   | What                                                                                 | Lifetime                   | Handled by                               |
| --- | ------------------------------------------------------------------------------------ | -------------------------- | ---------------------------------------- |
| 1   | **Client secret** — a self-signed ES256 JWT, produced locally from `private-key.pem` | **180 days** (Apple's cap) | `generate_client_secret.py`              |
| 2   | **Access token** — obtained by POSTing that secret to Apple                          | **3600 s**                 | `get_token.py` (cached + auto-refreshed) |
| 3   | **API call** — `Authorization: Bearer …` + `X-AP-Context: orgId=22648740`            | per request                | `apple_ads_client.py`                    |


Apple never sees the private key. It verifies our signature against the public
key registered in the Apple Ads UI.

The client secret is deliberately **not** stored on disk: it is cheap to
re-derive from `private-key.pem` plus the three values in `.env`, so
`get_token.py` signs one in memory whenever it needs it. Only the short-lived
access token is cached (`.token_cache.json`, mode 600).

---



## Setup

Requires a conda environment with four packages: `pyjwt`, `cryptography`,
`requests`, `python-dotenv`.

```bash
cd apple-ads-api

# 1. Environment — either a dedicated one...
conda env create -f environment.yml
conda activate apple-ads

#    ...or install into the environment you already use:
#    python3 -m pip install -r requirements.txt

# 2. Credentials
cp .env.example .env
$EDITOR .env          # paste clientId, teamId, keyId

# 3. Verify the whole chain
python3 test_connection.py
```

`conda list | grep -E 'pyjwt|cryptography|requests|dotenv'` confirms the four
packages are in the active environment. A missing `cryptography` is the usual
cause of `ModuleNotFoundError` — it means a different environment is active than
the one the packages went into.

`.env` holds:


| Variable              | Where it comes from                             |
| --------------------- | ----------------------------------------------- |
| `APPLE_ADS_CLIENT_ID` | shown **once** when the API client is generated |
| `APPLE_ADS_TEAM_ID`   | Apple Ads UI, API client list                   |
| `APPLE_ADS_KEY_ID`    | Apple Ads UI, API client list                   |
| `APPLE_ADS_ORG_ID`    | `22648740`                                      |


`clientId` and `teamId` are usually the **same** `SEARCHADS.<uuid>` string. That
makes a swap between them invisible on inspection — the JWT uses `sub` for the
clientId and `iss` for the teamId, and they must not be "tidied" together.

---



## Usage

```bash
# End-to-end health check: files → key → secret → token → ACL → campaigns → report
python3 test_connection.py

# Daily campaign performance (spend, impressions, taps, installs, avg CPT)
python3 fetch_campaign_report.py                     # last 7 days
python3 fetch_campaign_report.py --days 30 --csv     # writes reports/*.csv
python3 fetch_campaign_report.py --start 2026-08-01 --end 2026-08-28

# Resolve ad ids -> names (campaign / ad group / keyword tree)
python3 fetch_ad_structure.py                        # table to stdout
python3 fetch_ad_structure.py --campaign 2144331926 --save

# Which user came from which ad group / keyword
python3 resolve_attribution.py reports/attribution_rows.csv
python3 resolve_attribution.py rows.csv --by-ad-group
python3 resolve_attribution.py rows.csv --csv reports/out.csv

# Any GET endpoint, raw JSON
python3 apple_ads_client.py acls
python3 apple_ads_client.py campaigns

# Credential/token inspection (neither prints a secret unless asked)
python3 generate_client_secret.py          # shows exp + days remaining
python3 get_token.py --status              # cache state, no network call
python3 get_token.py --force               # force a refresh
python3 get_token.py --clear               # drop the cached token
```

From Python:

```python
from apple_ads_client import AppleAdsClient

client = AppleAdsClient()                 # orgId comes from .env
print(client.acls())
print(client.campaigns())
print(client.campaign_report("2026-08-01", "2026-08-28"))
```

`--print` on `generate_client_secret.py` / `get_token.py` emits the raw
credential. It is off by default so secrets do not land in terminal scrollback
or shell history by accident.

---



## Rotation and expiry



### Re-signing the client secret (routine — every 180 days)

Nothing to do in the Apple UI. The key pair and the three `.env` values are
unchanged; only the JWT's `iat`/`exp` move:

```bash
python3 get_token.py --force
```

That re-signs the secret and fetches a new access token in one step. To check
how much life the current secret has:

```bash
python3 generate_client_secret.py     # prints "valid for N days"
```

Worth a calendar reminder ~2 weeks before the 180 days elapse.
`test_connection.py` also warns when fewer than 14 days remain.

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
python3 get_token.py --clear
python3 test_connection.py

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
| `ModuleNotFoundError: No module named 'cryptography'` | The active conda environment is not the one the packages were installed into. `conda activate apple-ads`, or `python3 -m pip install -r requirements.txt` into the current one.                     |
| `invalid_client` from the token endpoint              | Secret past its 180 days (most likely); or `sub`/`iss` swapped; or the API client was revoked. `get_token.py` prints this checklist on failure.                                                     |
| API section missing at ads.apple.com                  | The account needs the **Account Admin** or **API Account Manager** role on the org.                                                                                                                 |
| clientId lost                                         | Not recoverable — generate a new API client (full key rotation above).                                                                                                                              |
| 401 on an API call                                    | Handled automatically: the client force-refreshes the token and retries exactly once. A second 401 is a real credential problem.                                                                    |
| Works for `/acls`, fails for everything else          | Missing or wrong `X-AP-Context`. `/acls` is the only endpoint that does not take it — which is why `test_connection.py` calls it without one, so a bad orgId cannot masquerade as a bad credential. |
| Empty report, no error                                | Apple only reports days with delivery. No spend in the window is a valid empty response, not a failure.                                                                                             |
| A metric column is blank for every row                | A wrong v5 field name. Apple returns absent keys silently rather than erroring — see the reconciliation caveats below.                                                                              |


---



## Files


| File                        | Purpose                                                                                                                                                                              |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `private-key.pem`           | EC P-256 signing key, mode 600. **Never commit, print or transmit.**                                                                                                                 |
| `public-key.pem`            | Registered with Apple. Safe to share.                                                                                                                                                |
| `.env`                      | clientId / teamId / keyId / orgId. Gitignored.                                                                                                                                       |
| `environment.yml`           | Conda environment definition.                                                                                                                                                        |
| `requirements.txt`          | Same four packages, for `pip install -r` into an existing environment.                                                                                                               |
| `generate_client_secret.py` | Signs the 180-day ES256 client secret.                                                                                                                                               |
| `get_token.py`              | Trades it for an access token; caches and auto-refreshes.                                                                                                                            |
| `apple_ads_client.py`       | API wrapper — auth headers, `X-AP-Context`, 401 retry.                                                                                                                               |
| `test_connection.py`        | 7-step end-to-end verification.                                                                                                                                                      |
| `fetch_campaign_report.py`  | Daily campaign performance → table and CSV.                                                                                                                                          |
| `fetch_ad_structure.py`     | Campaign → ad group → keyword tree, as a name lookup.                                                                                                                                |
| `resolve_attribution.py`    | Joins `attribution_apple_search_ads` rows to that tree, with a verification pass.                                                                                                    |
| `_bootstrap.py`             | Safety net: if the active interpreter is missing the four packages, re-execs the script under a local `venv/` should one exist. A no-op in a correctly configured conda environment. |
| `.token_cache.json`         | Cached access token, mode 600. Gitignored, safe to delete.                                                                                                                           |


`.gitignore` excludes `*.pem`, `.env*` (except `.env.example`), the token cache,
`venv/` and `reports/`. Before any commit, confirm with `git status` that no key,
`.env` or report is staged; `git check-ignore -v <path>` shows which rule covers
a given file.

---

