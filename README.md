# ScreenConnect Gateway

One HTTPS service that fronts both ConnectWise ScreenConnect APIs and exposes a single REST API to Rewst:

| Backend | Upstream | Auth |
|---|---|---|
| `restful` | ConnectWise Labs **RESTful API Manager** extension (`/App_Extensions/2d558935-…/Service.ashx/<Method>`) | `CTRLAuthHeader` secret |
| `native` | Built-in web API (`/Services/*.ashx`) | Cookie login + anti-forgery token (+ optional TOTP) |

Every read is a `GET`. This matters for **`GET /v1/companies`**, which Rewst uses for organization mapping. "Company" means session Custom Property 1.

## Quick start

```bash
cp .env.example .env    # fill in values
docker build -t sc-gateway .
docker run -d --name sc-gateway --env-file .env -p 8080:8080 sc-gateway
curl localhost:8080/health
curl -H "X-Api-Key: $GATEWAY_READ_KEY" localhost:8080/v1/companies
```

Local without Docker (Python 3.12 + [uv](https://docs.astral.sh/uv/)):

```bash
uv sync
set -a; source .env; set +a
uv run sc-gateway            # serves on $PORT (default 8080)
uv run pytest                # unit tests (upstreams mocked)
```

### HTTPS

Run the gateway behind a TLS-terminating proxy, such as Caddy, nginx, Azure App Service, or Cloudflare Tunnel. Alternatively, set `TLS_CERT_FILE` and `TLS_KEY_FILE` so uvicorn serves TLS directly.

Behind a proxy, `ALLOWED_SOURCE_IPS` should see the real client IP. Set uvicorn's `FORWARDED_ALLOW_IPS` to the proxy's address so it trusts `X-Forwarded-For` from that proxy only.

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SC_BASE_URL` | yes | – | e.g. `https://yourcompany.screenconnect.com` (must be https) |
| `SC_RESTFUL_SECRET` | for restful | – | RESTful API Manager `RESTfulAuthenticationSecret` |
| `SC_USERNAME`, `SC_PASSWORD` | for native | – | Dedicated ScreenConnect service account |
| `SC_TOTP_SECRET` | no | – | Base32 TOTP seed for that account |
| `GATEWAY_READ_KEY` | one of the two | – | `X-Api-Key` allowed for GET only |
| `GATEWAY_WRITE_KEY` | one of the two | – | `X-Api-Key` allowed for everything. Must differ from the read key |
| `BACKEND_PREFERENCE` | no | `restful-first` | or `native-first` |
| `COMPANY_GROUP_PATH` | no | `All Machines by Company` | Session group whose subgroups are companies |
| `COMPANY_CACHE_SECONDS` | no | `300` | `0` disables the cache |
| `ALLOWED_SOURCE_IPS` | no | – | Comma-separated CIDRs; others get 403 (`/health` exempt) |
| `PORT` | no | `8080` | |
| `NATIVE_DEFAULT_GROUP` | no | `All Machines` | Preferred unfiltered Access group for native name searches. Other types use their unfiltered group (`All Sessions`, `All Meetings`) from `GetSessionGroups`. Lookups use the group ScreenConnect reports for the session |
| `ALLOWED_USER_ROLES` | for user management | – (disabled) | Comma-separated roles `create_user`/`save_user` may assign; others get 403. **Unset = user management disabled.** `*` allows any role. The gateway's own service account (`SC_USERNAME`) can never be created, changed or renamed |
| `ALLOWED_EVENT_TYPES` | no | safe set | `SessionEventType` names/numbers `add_session_events` may queue; others get 403. Default: `AcknowledgedEvent, AddedNote, QueuedWake, QueuedCommand, QueuedMessage, QueuedGuestInfoUpdate, QueuedTool` (nothing beyond the other write endpoints). `all` enables every defined type, including uninstall/delete/elevation |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | no | – | Serve TLS directly |

If a backend's credentials are missing, that backend is disabled at startup. A warning is logged and `/health` reports it as `enabled: false`.

## Rewst setup

1. Create a custom integration pointed at `https://<gateway-host>` and import `openapi.json`. You can also fetch the spec from `GET /openapi.json`.
2. Authentication: API key, header `X-Api-Key`. Use the **read key** for org mapping and the **write key** only for workflows that change things.
3. For organization mapping, use `list_companies` (`GET /v1/companies`). Map using **`id`**, not `name`. `id` is the raw Custom Property 1 value: some companies have stray leading or trailing spaces, and `id` keeps them so lookups match exactly. `name` is trimmed for display.
4. To list a mapped company's machines, call `GET /v1/companies/{id}/sessions` with the `id` URL-encoded. A leading space becomes `%20`.
5. Rewst stores mapping ids **lowercased and trimmed**. That's fine: company ids are resolved in two steps, so `{{ ORG.MAPPINGS["…"] }}` can be passed straight in.
   - An exact match wins.
   - Otherwise a unique case- and whitespace-insensitive match is used. The response then carries `X-Gateway-Resolved-Company: <url-encoded raw id>`.
   - If several stored spellings match (e.g. `Acme` and `ACME `), the response is `409 {"message": "ambiguous company id", "candidates": [...]}`.
   - The same resolution applies to the company segment of `/v1/session-groups/sessions?path=<COMPANY_GROUP_PATH>&path=<company>`.
   - Genuinely different spellings (e.g. `D and B` vs `D&B`) are not merged. Fix those in Custom Property 1 or map each one.

## API

All responses are JSON. Errors are always `{"message": "..."}`. Every response has `X-Gateway-Backend: restful|native` (or `none` when no upstream was involved, e.g. auth failures). Endpoints that support both backends accept an optional `?backend=restful|native` that forces one backend and disables fallback.

| Method & path | operationId | Backend(s) |
|---|---|---|
| `GET /health` (no auth) | health | probes both |
| `GET /v1/companies` | list_companies | both (cached `COMPANY_CACHE_SECONDS`) |
| `GET /v1/companies/{companyId}/sessions?sessionType=2` | list_company_sessions | both |
| `GET /v1/session-groups` | list_session_groups | native only |
| `GET /v1/session-groups/sessions?path=…&path=…&sessionType=&filter=&limit=&findSessionId=&complete=` | list_group_sessions | native only |
| `GET /v1/sessions?filter=…` or `?name=…` (`&sessionType=`) | search_sessions | `name`: both; `filter`: restful only |
| `GET /v1/sessions/{id}?sessionType=&groupPath=` | get_session | both |
| `GET /v1/sessions/{id}/details?groupPath=&sessionType=` (`group=` still accepted) | get_session_details | both |
| `GET /v1/sessions/{id}/connection-logs?groupPath=&sessionType=` | get_connection_logs | both |
| `GET /v1/raw/restful/sessions?filter=…` or `?name=…` | raw_search_sessions | restful (verbatim) |
| `GET /v1/raw/restful/sessions/{id}` | raw_get_session | restful (verbatim) |
| `GET /v1/raw/restful/sessions/{id}/details` | raw_get_session_details | restful (verbatim) |
| `GET /v1/raw/native/live-data?sessionType=&path=&filter=&findSessionId=&limit=` | raw_get_live_data | native (verbatim) |
| `GET /v1/raw/native/sessions/{id}/details?groupPath=&sessionType=&items=` | raw_get_native_session_details | native (verbatim) |
| `POST /v1/sessions` | create_session | restful only |
| `POST /v1/sessions/{id}/commands` | send_command | both |
| `POST /v1/session-events` | add_session_events | native only |
| `POST /v1/sessions/{id}/messages` | send_message | restful only |
| `POST /v1/sessions/{id}/notes` | add_note | restful only |
| `POST /v1/sessions/{id}/toolbox` | run_toolbox_item | restful only |
| `PATCH /v1/sessions/{id}/name` | rename_session | restful only |
| `PATCH /v1/sessions/{id}/custom-properties` | update_custom_properties | both |
| `POST /v1/users` | create_user | native only |
| `POST /v1/users/save` | save_user | native only |

### Session fields

Every session response (`search_sessions`, `list_company_sessions`, `list_group_sessions`, `get_session`, and the `session` inside `get_session_details`) has the same shape:

- **Identity:** `sessionId, name, sessionType`
- **Guest details:** `guestMachineName, guestLoggedOnUserName, loggedOnUsers, operatingSystem`
- **Custom properties:** `company, site, department, deviceType, customProperties`
- **Status:** `isOnline`
- **Device evidence:** the eight fields in the table below

Values are `null` when the upstream gives no evidence; `false` is never turned into `null` or the other way round. `isOnline` is true only for an active guest (ProcessType 2) connection, so a technician connection alone doesn't count.

| Field | RESTful source | Native source |
|---|---|---|
| `guestMachineSerialNumber` | `GuestInfo.MachineSerialNumber` | `GetSessionDetails` only, so get-by-id/details; **null in native lists** |
| `guestHardwareNetworkAddress` (MAC) | `GuestInfo.HardwareNetworkAddress` | same as serial |
| `guestClientVersion` | `GuestClientVersion` | `GuestClientVersion` (identical) |
| `guestInfoUpdateTime` | `GuestInfoUpdateTime` | details: `BaseTime` − seconds; **null in native lists** |
| `lastGuestConnectedEventTime` | `LastGuestConnectedEventTime` | **always null** |
| `lastGuestDisconnectedEventTime` | `LastGuestDisconnectedEventTime` | live data: response time − seconds (±2 s) |
| `isDeleted` / `isEnded` | `IsDeleted` / `IsEnded` | **always null** |

**Native lastGuestConnectedEventTime is always null:** the native `LastConnectedEventTime` also counts technician connections, so it isn't a guest-only equivalent.

All times are ISO-8601 UTC (`...Z`). ScreenConnect's "never" values (`0001-01-01` on RESTful, `-1.797e308` seconds on native) and timestamps without a timezone become `null`, never the current time. Every native mapping above was checked against the RESTful values for the same machines on a live instance.

**Fleet inventory:** `GET /v1/sessions?filter=SessionType = 'Access'` returns every Access session, including those with a blank company (RESTful backend). List responses carry `X-Gateway-Truncated`: `false`/`true` when the upstream reports it (native), `unknown` when it gives no signal (RESTful). Don't treat a list as the complete fleet unless the header is `false`.

Commands are asynchronous. `send_command` returns `202 {sessionId, queued: true}`. Poll `get_session_details` for an `eventType: 70` (`RanCommand`) event whose `data` is the output; `44` is `QueuedCommand`. Event type numbers follow ScreenConnect's `SC.types.SessionEventType` (see `gateway/sc_types.py`).

### Backend selection

- `BACKEND_PREFERENCE` picks the first backend when both are supported. A disabled backend is skipped.
- **Reads:** a 5xx or timeout is retried once on the same backend. If it still fails, or the upstream rejects the credentials, the request moves to the other backend once. 4xx caused by input (bad filter, etc.) and 404 never fall back.
- **Writes are never auto-retried.** They fall back to the other backend only when the first one *definitely did not apply* the request: it is disabled, it couldn't be reached, it rejected the credentials, or the method doesn't exist (404/405). A write that times out or returns 5xx is **not** sent to the other backend, because it may already have run (for example, a command executed twice). This is a stricter reading of "try the other once on 5xx/timeout", chosen on purpose because of the "never auto-retry writes" rule.
- "Only" endpoints never fall back. They return 503 if their backend is disabled.

## Parity with the original Rewst ScreenConnect integrations

The gateway covers all 16 operations of Rewst's two original ScreenConnect integrations.

**Where the argument shapes come from:**
- **Native methods:** ScreenConnect's own web client, i.e. the `SC.service` definitions and the admin/host UI call sites.
- **RESTful methods:** the extension's client definitions where they exist (`CreateSession` and `UpdateSessionName` aren't in the web client and come from the original connector's contract).
- **Responses:** the shapes were checked against a live instance.

**Separate columns, because they prove different things:**
- **Implemented:** the gateway supports it.
- **Automated tests:** it's tested through HTTP (request arguments and response serialization), mostly in `tests/test_parity.py`.
- **Live (read-only):** it was checked against production. Write operations were deliberately **not** executed live (no commands, users or sessions were created); their argument order is verified against ScreenConnect's client code and covered by tests.

| # | Original operation | Gateway operations | Inputs / targeting | Output | Implemented | Automated tests | Live (read-only) |
|---|---|---|---|---|---|---|---|
| 1 | AddNoteToSession | add_note | sessionID, noteBody | `{sessionId, ok}` | ✅ | ✅ | not executed (write) |
| 2 | CreateSession | create_session | sessionType 0/1/2, name, isPublic, code, all 8 custom properties (named or indexed; conflicts → 400) | normalized + `raw` upstream response | ✅ | ✅ all 3 types, 8 properties, conflicts | not executed (write) |
| 3 | GetSessionBySessionID | get_session, raw_get_session | sessionID | normalized session / verbatim | ✅ | ✅ incl. unknown-field preservation | ✅ |
| 4 | GetSessionDetailsBySessionID | get_session_details, get_connection_logs, raw_get_session_details | sessionID | session, events (host, connectionId, correlationEventId), connections / verbatim (incl. screenshot) | ✅ | ✅ | ✅ Access + Support |
| 5 | GetSessionsByFilter | search_sessions (`filter`), raw_search_sessions | sessionFilter expression | normalized list (all types unless `sessionType`) / verbatim | ✅ | ✅ | ✅ |
| 6 | GetSessionsByName | search_sessions (`name`), raw_search_sessions | sessionName | normalized list / verbatim | ✅ | ✅ | ✅ |
| 7 | SendCommandToSession | send_command | sessionID, command (+ `#!ps`, `#timeout=` helpers) | `{sessionId, queued}` | ✅ | ✅ | not executed (write) |
| 8 | SendMessageToSession | send_message | sessionID, byHost (`from`), message | `{sessionId, queued}` | ✅ | ✅ | not executed (write) |
| 9 | SendToolboxItemToSession | run_toolbox_item | sessionID, toolboxItemName | `{sessionId, queued}` | ✅ | ✅ | not executed (write) |
| 10 | UpdateSessionCustomProperties | update_custom_properties | sessionID, all 8 values (merged with current) | resulting 8 values | ✅ | ✅ | not executed (write) |
| 11 | UpdateSessionName | rename_session | sessionID, name | `{sessionId, name}` | ✅ | ✅ | not executed (write) |
| 12 | AddSessionEvents | add_session_events (batch ≤100), send_command native fallback | sessionGroupPathParts, entries `{SessionID, EventType, Data, ConnectionID, CorrelationEventID}`; event types validated against `SessionEventType`; safe default set, all types via `ALLOWED_EVENT_TYPES=all` | `{queued, sessionIds}` | ✅ | ✅ batch, names/numbers, unknown types, default-deny, opt-in, read key | validation only (no events queued) |
| 13 | GetLiveData | list_group_sessions, list_company_sessions, search_sessions (`name`), list_companies, raw_get_live_data | sessionType 0/1/2, nested sessionGroupPathParts, filter (free text), findSessionID, sessionLimit | normalized / verbatim (Version, ProductVersion, full ResponseInfoMap) + truncation headers | ✅ | ✅ all types, nested paths, find, limit, `complete` | ✅ all 3 types, 1,715/1,715 complete |
| 14 | GetSessionDetails | get_session / get_session_details / get_connection_logs (native), raw_get_native_session_details | sessionGroupPathPartsOrName (default: the session's reported group), sessionID, items flags | normalized / verbatim | ✅ | ✅ Support, Meeting, items, paths | ✅ Access + Support |
| 15 | UpdateSessionCustomPropertyValue | update_custom_properties (native) | caller `groupPath` passed through unchanged (default: the session's reported group), sessionID, index 0-7, value | resulting 8 values | ✅ | ✅ caller path, Support session | not executed (write) |
| 16 | SaveUser | create_user, save_user | userSourceName, originalUserName (`oldUserName`), newUserName, password (null = keep), verifyPassword, passwordQuestion (`otpTokenString`, the MFA setting), displayName, comment, email, roleNames, forcePasswordChange | `{userName, oldUserName, created}` | ✅ create, update, rename (opt-in via `ALLOWED_USER_ROLES`) | ✅ incl. required-fields guard, secrecy, role allowlist, disabled-by-default, service-account guard | validation only (no user saved) |

### Raw compatibility endpoints

`/v1/raw/...` returns ScreenConnect's payload **verbatim** in `response`, alongside `backend`, `upstreamMethod` and `receivedAt`. Nothing is removed, including fields the gateway doesn't know about.

- **Read-only:** they're GET only and use the read key.
- **Allowlisted:** they're limited to the allowlisted methods, with typed parameters, so they aren't a general proxy.
- **Native times are relative:** `GetLiveData` times are seconds before `receivedAt`, and `GetSessionDetails` times are relative to its `BaseTime` (epoch ms; event times in ms, guest-info time in seconds).
- **Use these when you need every field.** The normalized endpoints stay compact.
- **Pass a `path` to `raw_get_live_data`.** Without one, ScreenConnect picks its own default view for the session type.
- **RESTful details include `GuestScreenshotContent`,** an image of the user's screen. Treat the read key accordingly.

### Native targeting

- **Get-by-ID, details, connection logs and native writes** locate the session the way ScreenConnect's UI does: `findSessionID` with the session's type.
- **Without a `sessionType` hint,** the gateway tries Access, Support and Meeting (at most three reads). It then uses the group path ScreenConnect reports for the session.
- **`groupPath` (repeatable) and `sessionType` override this.** For custom-property writes, a caller's `groupPath` is passed to `UpdateSessionCustomPropertyValue` unchanged.
- **Native name searches without `sessionType`** search the unfiltered group of every type, matching RESTful `GetSessionsByName`, which covers all types.

### Completeness

- **`GetLiveData` has no paging,** only `sessionLimit`.
  - List responses carry `X-Gateway-Truncated` (`true`/`false` from ScreenConnect's `UntruncatedSessionCount`, `unknown` when there's no signal) and, for group listings, `X-Gateway-Total-Count`.
  - `list_group_sessions` defaults to `limit=1000`. Pass `complete=true` to re-request with the untruncated count (up to 100,000) and get `X-Gateway-Truncated: false`.
  - Company sessions and native name searches do this automatically.
- **The RESTful API has no limit or paging parameters and no truncation signal,** so its lists report `unknown`.
  - In live testing it returned the full fleet (1,715 Access sessions, equal to native's untruncated count).
  - For a verified-complete Access inventory, use `GET /v1/session-groups/sessions?path=All Machines&complete=true`.

### Backend differences (by design)

- **`filter`:** on RESTful it's an expression language (`GetSessionsByFilter`). On native it's free-text search, as in the web UI search box. Expressions are therefore restful-only: `?backend=native` returns 400 and they never silently fall back.
- **Omitted `sessionType`:** both backends cover all session types.
- **Native lists:** they don't include serial, MAC or guest-info update time (those need a per-session details read). Native never provides `lastGuestConnectedEventTime`, `isDeleted` or `isEnded` (see Session fields).
- **Native lookups:** they cost one to three extra reads (session location) per single-session call. Lists never make per-session calls.

### Remaining limitations

- Write operations are verified by contract and tests, not by live execution.
- `CreateSession` and `UpdateSessionName` RESTful argument shapes come from the original connector; they aren't in ScreenConnect's web client.
- The RESTful backend can't prove completeness (no truncation signal).
- `save_user` can't read a user's current values (no read method is in the original integrations), so updates must restate every field.

## Security

### Read this before deploying

- **The write key is the keys to the kingdom.** It can run arbitrary commands on every machine the service account can reach, and `POST /v1/users` can create ScreenConnect users. Store it like a domain-admin credential, give it only to the Rewst workflows that need it, and rotate it if it leaks.
- **Give the ScreenConnect service account the least privilege that works.** The gateway can only do what that account is allowed to do. If you don't need `create_user`, don't give the account user-management rights. User management (`create_user`, `save_user`) is **off until you set `ALLOWED_USER_ROLES`**. List only the roles Rewst should assign, so a leaked write key can't mint administrators or take over admin accounts (`save_user` can reset any listed user's password). The gateway refuses to modify its own service account. `add_session_events` only queues a safe default set of event types. Destructive types (uninstall, delete, license, elevation, logon) need an explicit `ALLOWED_EVENT_TYPES`; use `all` only if you really need them.
- **Always serve over HTTPS.** API keys travel in a header.
- **Set `ALLOWED_SOURCE_IPS`** to Rewst's egress ranges if you can.
- **Behind a reverse proxy**, set `FORWARDED_ALLOW_IPS` to the proxy's address only. Never combine `FORWARDED_ALLOW_IPS=*` with `ALLOWED_SOURCE_IPS` when the gateway is reachable other than through the proxy: clients could spoof `X-Forwarded-For` and walk past the allowlist. On platforms whose proxy IPs aren't fixed (e.g. Azure App Service), use the platform's own access restrictions instead.
- **Never commit `.env`.** It is in `.gitignore` and `.dockerignore`. Pass secrets as environment variables or through your platform's secret store.

### What the gateway enforces

- `X-Api-Key` is compared in constant time, and both keys are checked on every request. The read key can only `GET`; the write key can do everything.
  - The key is checked **before routing**, so unauthenticated requests never have their body parsed. Unknown paths return 401, not 404.
  - Only `/health` and `/openapi.json` need no key. `ALLOWED_SOURCE_IPS` still applies to `/openapi.json`.
- **Rate limits:** 120 reads/min and 10 writes/min per key (sliding window, `429` with `Retry-After`).
- **Request bodies** are capped at 1 MB (`413`). A chunked body without `Content-Length` is rejected (`411`).
- **Upstream allowlist:** the gateway can call only the 17 methods in `gateway/allowlist.py` (11 restful + 6 native), plus the native login sequence (`TryLogin`, `GET /`). Clients check the allowlist before any network I/O, and tests enforce it.
- **Injection:** company ids and names are single-quote-escaped (`'` → `''`) before they go into ScreenConnect filter expressions. Session ids must be GUIDs.
  - The `filter` parameter on `GET /v1/sessions` is a ScreenConnect filter expression, passed through by design and supported only by the restful backend; anyone with the read key can query any session.
  - On `/v1/session-groups/sessions`, `filter` is the native API's free-text search, like the web UI search box. That API doesn't evaluate expressions.
- **Audit log:** every non-GET request, including rejected ones, emits one JSON line on the `gateway.audit` logger with `time, endpoint, sessionId, clientIp, keyId, backend, status, result`. Request bodies are never logged. Lines for requests without a valid key are capped at 60/min so they can't flood the log; the overflow is counted in `suppressedKeyless` on the next line written.
- **Redaction:** every log record from any logger, *including tracebacks*, is scrubbed of the configured secrets and of the native session cookies and anti-forgery token (registered when obtained).
  - Validation errors never echo submitted values, and the `create_user` password is a `SecretStr`.
- **`/health` is unauthenticated, so it is kept cheap and quiet.**
  - It reports only `ok`/`enabled` per backend; failure details go to the log.
  - Probes are single-flight and cached for 15 s, so a burst of requests causes at most one probe per backend.
- **Service-account lockout protection:** after a failed native login, the gateway waits 60 s before trying again. Requests made in the meantime fail fast, or fall back to restful.
  - The login itself is single-flight: concurrent requests share one login. On 401/403/redirect the client re-logs in once and retries, but not within 30 s of a successful login (a 403 then means permission denied, and re-logging in would reuse the TOTP code).
- **Responses** carry `Cache-Control: no-store`, so a CDN or proxy won't cache company data. Shared caches don't treat `X-Api-Key` like `Authorization`.
- **Upstream calls:** 30 s timeout per call and a 90 s overall deadline per request (retries, re-login and fallback included); redirects are never followed and are treated as failures, and the upstream host is fixed by `SC_BASE_URL`, so callers can't redirect requests elsewhere (no SSRF).
- **Container:** runs as a non-root user (uid 10001). Dependencies are installed from `requirements.lock` with `--require-hashes`, which a test keeps identical to `uv.lock`. The base image is the `python:3.12-slim` tag rather than a digest, so rebuilds pick up OS security patches.

### Reporting a vulnerability

Please open a GitHub security advisory on this repository rather than a public issue.

## Tests

```bash
uv run pytest
```

Upstreams are mocked with `httpx.MockTransport` (a fake ScreenConnect that asserts every call is allowlisted). Coverage:

- auth/keys
- backend selection and fallback, including the write-safety rules
- exact company matching, including leading spaces and `/`
- quote escaping
- custom-property merge on restful (replace all 8) and native (one call per changed index)
- the allowlist
- error mapping to `{message}`
- log redaction and audit
- TOTP login, anti-forgery extraction, re-login, and serialized logins
- drift check between the committed `openapi.json` and the generated spec

Regenerate the spec after API changes: `uv run python scripts/export_openapi.py`.

After changing dependencies, refresh the Docker lockfile: `uv export --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.lock`.

## Live-instance verification (must-test)

The native web API isn't publicly documented; its contract comes from Rewst's existing connector. Re-run the checks after ScreenConnect upgrades:

```bash
GATEWAY_URL=https://<gateway-host> GATEWAY_READ_KEY=... uv run python scripts/live_check.py
```

Results against a production ScreenConnect cloud instance (2026-10-11, 100+ companies):

| Item | How it was checked | Result |
|---|---|---|
| Filter `CustomProperty1 <> ''` (restful companies) | companies via restful: identical ids and per-company counts to native | ✅ pass |
| Quote escaping (`''`), e.g. `O'Brien's Bakery` | company sessions for a real name containing `'`, both backends: exact match | ✅ pass |
| Native login with TOTP | `/health` → `native.ok` with `SC_TOTP_SECRET` set | ✅ pass |
| Native login without TOTP | not tested (service account has MFA) | ⏭ n/a |
| Anti-forgery token extraction | native login + session-group and live-data calls succeeded | ✅ pass |
| Session-expiry re-login | Manual: sign the service account out in ScreenConnect, then call a native endpoint (e.g. `/v1/session-groups`); expect 200 and a "re-authenticating" log line | ⏳ pending |
| `GetLiveData` nested path `[COMPANY_GROUP_PATH, "<company>"]` | company sessions via native match the group summary counts | ✅ pass |
| Company ids with leading/trailing spaces round-trip | every such company on the instance (4) via both backends: exact match, counts equal group summaries | ✅ pass |
| Gateway keys | no/wrong key → 401, read key GET → 200, read key POST → 403 | ✅ pass |
| Log redaction | none of the 5 configured secrets appear in the gateway log | ✅ pass |

Still unverified against the live instance (writes were not exercised):

- connection fields in `GetSessionDetails` (`ConnectionID`, `ProcessType`, `ParticipantName`, `ClientType`, `NetworkAddress`)
- whether a failed `TryLogin` is reported in its response body (the gateway currently detects failure by a missing anti-forgery token or a later 401/403)

## Layout

```
gateway/
  config.py          env → Settings
  allowlist.py       the 17 permitted upstream methods
  errors.py          error types (status, fallback_ok, write_safe)
  upstream/          restful + native clients, shared retry/error policy
  normalize.py       pure shaping functions (sessions, companies, commands, merge)
  service.py         per-endpoint implementations + backend dispatch/fallback
  security.py        key identification, IP allowlist, rate limiter
  logs.py            redaction + audit log
  main.py            FastAPI app, routes, OpenAPI customization
scripts/export_openapi.py, scripts/live_check.py
tests/
openapi.json
```

## License

MIT. See [LICENSE](LICENSE).

See [CHANGELOG.md](CHANGELOG.md) for release history.
