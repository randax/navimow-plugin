# Navimow OpenAPI token acquisition and refresh

Resolves wayfinder ticket #18 and feeds the deployment decision in #12 ("the
collector refreshes its own token; flow under research"). Static reading of
source code, public issue trackers and vendor pages only; no Navimow endpoint
was called. Tags as in `sdk-data-audit.md`:

- **[code]** verified in source code of a project that runs against the live API.
- **[obs]** a field observation written down by a maintainer or user (log
  excerpt, dated comment, issue report).
- **[inferred]** our own reading of the above.
- **UNVERIFIED** nobody we found has checked it.

## Verdict

**Refresh-token flow. The collector can refresh unattended for as long as the
refresh token stays valid, but the first token needs one interactive browser
login; there is no password or client-credentials grant on the public OpenAPI.**

- The OpenAPI issues an OAuth2 authorization code from a vendor-hosted login
  page, exchanges it at `POST /openapi/oauth/getAccessToken`, and returns
  `access_token` + `refresh_token` + `expires_in: 3600`. Every working
  integration refreshes with `grant_type=refresh_token` at the same endpoint,
  about every 55 minutes, indefinitely [code], [obs].
- All of them use one shared, hard-coded client (`client_id=homeassistant`,
  a fixed `client_secret`) that Segway built for its official Home Assistant
  integration. No third party has obtained its own client; Segway has not
  answered a public request for one [obs].
- The refresh token's own lifetime is UNVERIFIED. Long-running adapters
  (ioBroker) treat re-login as rare; some HA users report the grant dying after
  1 h or after 1-2 days. Plan for re-login as an operator action, not a fault.

For the collector this means: config takes either a one-shot authorization
code (or the full redirect URL) *or* a pre-existing token JSON; the collector
persists the rotating token pair to its state file and refreshes on a timer
plus on 401; when refresh is rejected deterministically it surfaces a
"re-login needed" health state and keeps the MQTT stream on the last access
token until it dies. Reading an externally maintained token file is not
needed as a primary mode, but stays trivial to support (same JSON shape as
ioBroker's `auth.token` state).

## Sources

| Source | Commit / date | Used for |
|---|---|---|
| `segwaynavimow/NavimowHA` (H, official HA integration by Segway) | `2331841`, 2026-04-10, v1.1.0 | `custom_components/navimow/const.py`, `auth.py`, `config_flow.py`, `coordinator.py`, `__init__.py`; issues #5, #10, #33, #49, #82, #86, #92 |
| `segwaynavimow/navimow-sdk` (U, official SDK) | `6596aa0`, 2026-04-10 | `README.md`, `mower_sdk/api.py`, `client.py`, `mqtt.py`, `errors.py` |
| `randax/navimow-sdk` (S) | `e9c4744`, 2026-08-30 | `docs/getting-started.md`, `docs/rest.md`, `mower_sdk/api.py` |
| `TA2k/ioBroker.navimow` (I) | `537721b`, 2026-09-14, v1.1.2 | `main.js` lines 13-17, 42-50, 454-550, 555-660, 2086-2256, 2507-2520; `admin/jsonConfig.json` line 19; `README.md` lines 40-49, 276; `main.test.js` 699-770; issues #13, #29 |
| `vahesoo/NaviMower` (N) | `9236970`, 2026-09-15 | `custom_components/navimower/const.py` 76-82, `oauth.py`, `mqtt.py` 801-870, `config_flow_base.py`, `api/passport.py`, `api/client.py` (private app cloud, for contrast) |
| `MadMorpheus/naviwatch` (W) | `66fac70`, 2026-09-10 | `custom_components/navimow_custom/auth.py`, `coordinator.py` 177-310, 547-551, `mqtt_client.py` 270-283, `CHANGELOG.md`, `README.md` 111 |
| `jrackerby/navimow` (J) | `64912f6`, 2026-09-15 | `auth.py` 1-8, `const.py` 23-29, `README.md` 227-228 (dissenting lifetime claim) |
| `niddu85/home-assistant-navimow` (D) | `78f8656`, 2026-04-08 | `const.py` 6-9, `api.py` 71-85, `config_flow.py` 97-123 (own-HTTP redirect) |
| Home Assistant core `helpers/config_entry_oauth2_flow.py` (HA) | `dev` branch, fetched 2026-09-15 | what H/N/J/W actually send: `redirect_uri`, `_token_request`, `async_ensure_token_valid` |
| navimow.com "Navimow X3 API" page and Zendesk "Open API Documentation for Expansion Bay" | fetched 2026-09-15 (Zendesk returned 403) | whether a developer programme exists |

## 1. Endpoints and grant type

- **[code]** Authorization page: `https://navimow-h5-fra.willand.com/smartHome/login?channel=homeassistant`
  (H `const.py` `OAUTH2_AUTHORIZE`; identical in N, J, W, D, I). Standard
  `response_type=code`, `client_id`, `redirect_uri`, `state` are appended by
  the OAuth client; the vendor page additionally keys on `channel=homeassistant`
  (H `auth.py` `async_generate_authorize_url` comment: "Carry
  channel=homeassistant, which the vendor login page keys on"; I's admin link
  spells it out in full:
  `smartHome/login?channel=homeassistant&client_id=homeassistant&response_type=code&redirect_uri=http%3A%2F%2Flocalhost%3A1%2Fcallback`).
- **[code]** Token endpoint: `POST https://navimow-fra.ninebot.com/openapi/oauth/getAccessToken`,
  body `application/x-www-form-urlencoded`. Exchange:
  `grant_type=authorization_code, code, client_id, client_secret, redirect_uri`
  (I `exchangeCodeForToken`; HA `_token_request`). Refresh:
  `grant_type=refresh_token, refresh_token, client_id, client_secret`
  (I `refreshToken`; D `async_refresh_token`; HA `_async_refresh_token` +
  `_token_request`). H sets `OAUTH2_REFRESH = None`, i.e. refresh goes to the
  same URL.
- **[obs]** Response shape, from a real refresh in an ioBroker debug log
  (I issue #13, 2026-04-06):
  `{"access_token":"e9f0…","refresh_token":"1bc4…","token_type":"Bearer","expires_in":3600}`.
  The refresh response carries a `refresh_token`; whether it is a *new* value
  (rotation) or the old one echoed is UNVERIFIED. D stores
  `token_response.get("refresh_token", refresh_token)` and I stores the whole
  response, so both are safe either way; do the same.
- **[code]** Error shape is not OAuth-standard: the vendor returns prose, so
  H, J and W classify a refresh failure by substring
  (`401`, `403`, `invalid`, `expired`, `unauthorized`, `forbidden` =>
  re-login; anything else => transient, keep the cached access token)
  (H `auth.py` `_async_refresh_token`, J `_DETERMINISTIC`, W `auth.py`).
  Expired access tokens surface as HTTP 401 on REST calls (I `pollDevices`,
  `sendCommand`: `error.response.status === 401` -> `handleTokenRefresh`) and
  as `CODE_OAUTH_INFO_ILLEGAL` on the MQTT side (H `__init__.py` 218-219,
  D `coordinator.py` 131, W `coordinator.py` 282).
- **[code]** The redirect URI is not pinned to one value: I uses
  `http://localhost:1/callback` (the browser shows "page cannot be reached"
  and the user copies `?code=` out of the address bar, I `README.md` 42-47),
  H/N/J/W go through HA's default `https://my.home-assistant.io/redirect/oauth`
  or `<ha_host>/auth/external/callback` (HA `MY_AUTH_CALLBACK_PATH`,
  `AUTH_CALLBACK_PATH`), D uses `<ha_url>/api/navimow/callback`. Whether the
  server validates `redirect_uri` at all is UNVERIFIED; at least these
  patterns are accepted. No PKCE parameters appear anywhere.
- **[code]** There is **no password grant and no client-credentials grant**
  in any of the eight projects. The only credential-based login in the
  ecosystem is N's `api/passport.py` (`POST /user/user/login` etc. against the
  Segway *passport* servers with the mobile app's `clientId=mowerbot_app_prod`
  and a signed request); that is the private app cloud, which the map puts out
  of scope, and its tokens are not the OpenAPI bearer token (N `README.md` 5:
  two separate connections; N `config_flow_base.py`: private login *then*
  "official Smart Home OAuth").

## 2. Client id / secret and third-party access

- **[code]** `CLIENT_ID = "homeassistant"`, `CLIENT_SECRET = "57056e15-722e-42be-bbaa-b0cbfb208a52"`
  are hard-coded in the official H `const.py` (with the comment "OAuth2
  Client 配置") and copied verbatim by I, N, J, W and D. H's README asks only
  for "your Navimow account [that] can sign in to the official app" (line 50);
  J `README.md` 208 spells out the consequence: "the client is registered
  automatically; you are not asked for one". The secret is therefore public;
  it functions as a channel tag rather than a secret.
- **[obs]** Nobody has a client of their own. H issue #82 (2026-07-12) asks
  Segway for a separate OAuth client for an IP-Symcon integration, PKCE
  support, redirect URI rules, refresh semantics, rate limits; Segway's only
  reply: "The integration with this platform requires evaluation, and we will
  get back to you". Still open as of 2026-09-15. W `README.md` 111 names
  "Segway locks down the shared OAuth client" as its top project risk.
- **[obs]** The "Navimow X3 API / Open API for Expansion Bay" programme on
  navimow.com (developer credentials requested inside the app, terms to sign)
  is about the X3's physical expansion port, not the smart-home cloud API; the
  detailed Zendesk article is not publicly fetchable (HTTP 403). No public
  developer portal for the `/openapi/smarthome` cloud API exists. UNVERIFIED
  whether the expansion-bay programme could also issue cloud OAuth clients.
- **[inferred]** The collector must either reuse the `homeassistant` client
  like every other integration, or accept a token minted by something that
  does. Reuse is the de-facto norm (five independent projects); the risk is
  Segway rotating the secret, which would break all of them at once, and the
  collector must make the client id/secret configurable so a new value can be
  dropped in without a release.

## 3. Lifetimes

| Item | Value | Evidence |
|---|---|---|
| Access token | **3600 s** | **[obs]** `expires_in: 3600` in every logged exchange/refresh (I #13 2026-04-06, I #29 2026-07-06, twice); I's log line "Token refresh scheduled in 55 min" = `expires_in - 300`. **[code]** U `mqtt.py` 548/566 comments "OAuth token 每小时轮换" (rotates hourly); W `coordinator.py` 547 "Access-Token rotiert stuendlich"; H issue #86 comment (MadMorpheus): "the access token really does expire after exactly 1h (not 1-2 days as some comments/docs suggest)". |
| Refresh token | **issued; lifetime UNVERIFIED** | **[obs]** present in exchange and refresh responses (I #13). **[obs]** I's README: "The token is refreshed automatically. A re-login is only needed if the refresh token expires"; I's retry ladder (`TOKEN_REFRESH_RETRY_MS` 1/5/15/60 min, commit `a282395` 2026-08-11) exists because "a refresh window missed over a short network outage is the common case", i.e. the refresh token is expected to still work well after the access token has died. **[obs]** Contrary reports: H #86 (2026-08-03, i108, v1.1.0) grant dead after exactly 1 h with "Refresh token is invalid or server rejected the request"; H #33 and #10 "losing authentication every 1-2 days" (i105e, 2026-04), which Segway attributed to "the integration's session handling" and patched in v1.1.0 (`26c8ed5`, `cb2bd56`). |
| "1-2 days, no refresh token" | **dissenting, likely stale** | **[code]** J `auth.py` 3-4 and H `auth.py` docstring ("OAuth token 有效期约为 1-2 天 … 初始 token 不含 refresh_token") say the initial grant carries no refresh token and lasts 1-2 days. Both still *handle* a refresh token when present. Every captured response does contain one and says 3600 s, so we treat J/H's text as an outdated observation from an earlier server behaviour or another account type. Confirm during the live capture (#6) by dumping the exchange response. |
| MQTT credentials (`userName`/`pwdInfo` from `GET /openapi/mqtt/userInfo/get/v2`) | bound to the access token | **[code]** H `__init__.py` 218-219: "userName/pwdInfo 与 OAuth token 绑定, token 刷新或过期后凭据会失效" (bound to the OAuth token; invalid after refresh/expiry, reconnecting with old ones gives `CODE_OAUTH_INFO_ILLEGAL`); I `refreshMqttCredentials` comment "MQTT credentials are bound to it"; W `coordinator.py` 280-283. The wss upgrade also carries `Authorization: Bearer <access_token>` (I `connectMqtt`, S `docs/rest.md` 115-119). **[obs]** the broker drops idle connections after ~1 h / on token rotation (W `mqtt_client.py` 280; H #5 "hourly MQTT disconnect/reconnect issue"). |

**[inferred]** Refresh cadence for the collector: refresh at `expires_in - 300 s`
(what I does and what HA's `async_ensure_token_valid` effectively does with
`CLOCK_OUT_OF_SYNC_MAX_SEC = 20`), then re-fetch `mqtt/userInfo/get/v2` and
hand the new bearer + userName/pwdInfo to the SDK via
`update_mqtt_credentials(...)`, which applies them on the next reconnect
without forcing one (U/S `mqtt.py` `update_credentials`). Do **not** disconnect
MQTT on every refresh; U's comment says that was tried and caused hourly
"device unavailable" blips. I does disconnect/reconnect on refresh
(`handleTokenRefresh`); either works, U's is quieter.

## 4. Relationship to `authList`

- **[code]** The token is an *account* token. `GET /openapi/smarthome/authList`
  returns `data.payload.devices[]` for every mower the logged-in account can
  see (I `getDeviceList`; U `api.py` 105; N `_async_validate_oauth_mower`
  matches the mower by serial inside that list and accepts a single-device list
  as a match). One token covers all devices, which matches the #12 decision of
  one collector process per account.
- **[inferred]** The "smartHome" login page and `channel=homeassistant` mark
  the account as authorized for the smart-home OpenAPI; there is no separate
  per-device consent step visible in any flow, and no revocation endpoint is
  called by anyone. Whether re-logging in invalidates earlier tokens for the
  same account (single-session semantics) is UNVERIFIED; N's `README.md` 60
  notes the OAuth account "may be different from the account used for the
  other Navimower connection", so a dedicated account for the collector is a
  clean way to avoid interfering with the phone app.

## 5. Rate limits and server-side protection

- **[obs]** No rate limit has been reported on `oauth/getAccessToken` itself.
  I throttles token refreshes only to avoid its own loops
  (`MQTT_CREDENTIAL_REFRESH_MIN_MS` 10 min for connects that never came up,
  commit `1b6ad51` "stop refreshing the OAuth token once per failed MQTT
  connect attempt").
- **[obs]** `GET /openapi/mqtt/userInfo/get/v2` **is** protected:
  - "Request too frequent. Please retry after 1 minute." — W `CHANGELOG.md`
    (US account, 2026-08-17 18:17; 36 retries at ~1.6 s intervals kept the
    limit tripped; resolved in 6 s once retries stopped, 2026-08-18).
  - "url Circuit Breaker" in the response body (`code != 1`) — H issue #49
    (many users, EU and US, 2026); a commenter identifies it as Alibaba
    Sentinel on the API gateway; I `connectMqtt` comment "Server may
    temporarily block the request (e.g. 'url Circuit Breaker')". The remedy
    that worked (H PR #50 description, I `scheduleMqttRetry`): cache the last
    good `mqttHost`/`mqttUrl`/`userName`/`pwdInfo`, back off, and never retry
    setup in a tight loop.
- **[obs]** `getVehicleStatus` is served from a cache 1-2 min behind
  (`sdk-data-audit.md` section 1); H polls it at most hourly
  (`HTTP_FALLBACK_MIN_INTERVAL = 3600`), I every 5 min by default.
- **[inferred]** Collector policy: one token refresh per hour, one
  `mqtt/userInfo` fetch per (re)connect with a >= 60 s floor between attempts
  and exponential back-off, cached MQTT credentials as fallback, and a REST
  poll floor of a few minutes. That is comfortably inside everything observed.

## 6. How each project bootstraps (for the collector's UX)

| Project | First token | Storage | Refresh | Re-login trigger |
|---|---|---|---|---|
| H (official) | HA OAuth flow via `my.home-assistant.io` redirect | HA config entry | HA `async_ensure_token_valid` before every poll and on MQTT disconnect | `ConfigEntryAuthFailed` -> HA repair card |
| I (ioBroker) | user pastes `?code=` URL from a dead `localhost:1` redirect into settings; adapter exchanges it once and clears the field | `auth.token` state (JSON, encrypted) | timer at `expires_in-300`, plus on 401, plus before MQTT credential refresh; retry ladder 1/5/15/60 min | log "Please re-login via settings", `info.connection=false` |
| N, J, W | as H (own OAuth implementation, same client) | HA config entry | as H | as H |
| D | own HTTP callback inside HA | config entry `access_token`/`refresh_token` | timestamp check with 10 s buffer, on `TOKEN_EXPIRED` | "Session expired. Please remove and re-add the integration." |

**[inferred]** The ioBroker model is the right one for a headless collector:
print (or serve) the authorize URL with `redirect_uri=http://localhost:1/callback`
(or a configurable loopback the operator's browser can reach), accept either
the pasted code or the full redirect URL, exchange once, persist
`{access_token, refresh_token, expires_in, obtained_at}` to the state file, and
never write the code back to config.

## 7. Open questions for the live capture (#6) and the decision ticket (#19)

1. Dump one `authorization_code` exchange response and one `refresh_token`
   response (redacted): confirm `expires_in`, whether the refresh token
   rotates, and whether the exchange really carries a refresh token for this
   account (J/H text says no, all logs say yes).
2. Let the collector prototype run >= 3 days with hourly refresh only; note
   the first deterministic refresh rejection, if any, to bound the refresh
   token's lifetime.
3. Log in from the phone app while the collector holds a token: does the
   collector's refresh token survive (single-session or multi-session)?
4. Record the exact error body of a rejected refresh so the collector can
   match on it instead of the substring heuristic H/J/W use.
5. Check whether `redirect_uri` is validated (try an arbitrary loopback URL
   in the authorize link) to decide the default the collector prints.
