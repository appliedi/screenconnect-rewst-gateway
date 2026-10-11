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
| `NATIVE_DEFAULT_GROUP` | no | `All Machines` | Group used by native calls that need one but get none from the caller (search, get-by-id, details, custom-property writes) |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | no | – | Serve TLS directly |

If a backend's credentials are missing, that backend is disabled at startup. A warning is logged and `/health` reports it.

## Rewst setup

1. Create a custom integration pointed at `https://<gateway-host>` and import `openapi.json`. You can also fetch the spec from `GET /openapi.json`.
2. Authentication: API key, header `X-Api-Key`. Use the **read key** for org mapping and the **write key** only for workflows that change things.
3. For organization mapping, use `list_companies` (`GET /v1/companies`). Map using **`id`**, not `name`. `id` is the raw Custom Property 1 value: some companies have stray leading or trailing spaces, and `id` keeps them so lookups match exactly. `name` is trimmed for display.
4. To list a mapped company's machines, call `GET /v1/companies/{id}/sessions` with the `id` URL-encoded. A leading space becomes `%20`.

## API

All responses are JSON. Errors are always `{"message": "..."}`. Every response has `X-Gateway-Backend: restful|native` (or `none` when no upstream was involved, e.g. auth failures). Endpoints that support both backends accept an optional `?backend=restful|native` that forces one backend and disables fallback.

| Method & path | operationId | Backend(s) |
|---|---|---|
| `GET /health` (no auth) | health | probes both |
| `GET /v1/companies` | list_companies | both (cached `COMPANY_CACHE_SECONDS`) |
| `GET /v1/companies/{companyId}/sessions?sessionType=2` | list_company_sessions | both |
| `GET /v1/session-groups` | list_session_groups | native only |
| `GET /v1/session-groups/sessions?path=…&path=…&sessionType=&filter=&limit=` | list_group_sessions | native only |
| `GET /v1/sessions?filter=…` or `?name=…` | search_sessions | both |
| `GET /v1/sessions/{id}` | get_session | both |
| `GET /v1/sessions/{id}/details?group=` | get_session_details | both |
| `GET /v1/sessions/{id}/connection-logs?group=` | get_connection_logs | native only |
| `POST /v1/sessions` | create_session | restful only |
| `POST /v1/sessions/{id}/commands` | send_command | both |
| `POST /v1/sessions/{id}/messages` | send_message | restful only |
| `POST /v1/sessions/{id}/notes` | add_note | restful only |
| `POST /v1/sessions/{id}/toolbox` | run_toolbox_item | restful only |
| `PATCH /v1/sessions/{id}/name` | rename_session | restful only |
| `PATCH /v1/sessions/{id}/custom-properties` | update_custom_properties | both |
| `POST /v1/users` | create_user | native only |

Commands are asynchronous. `send_command` returns `202 {sessionId, queued: true}`. Poll `get_session_details` for an `eventType: 70` (ProcessedCommand) event whose `data` is the output; `44` is the RanCommand event.

### Backend selection

- `BACKEND_PREFERENCE` picks the first backend when both are supported. A disabled backend is skipped.
- **Reads:** a 5xx or timeout is retried once on the same backend. If it still fails, or the upstream rejects the credentials, the request moves to the other backend once. 4xx caused by input (bad filter, etc.) and 404 never fall back.
- **Writes are never auto-retried.** They fall back to the other backend only when the first one *definitely did not apply* the request: it is disabled, it couldn't be reached, it rejected the credentials, or the method doesn't exist (404/405). A write that times out or returns 5xx is **not** sent to the other backend, because it may already have run (for example, a command executed twice). This is a stricter reading of "try the other once on 5xx/timeout", chosen on purpose because of the "never auto-retry writes" rule.
- "Only" endpoints never fall back. They return 503 if their backend is disabled.

## Security

- `X-Api-Key` is compared in constant time, and both keys are checked on every request. The read key can only `GET`. The write key can do everything. `/health` and `/openapi.json` need no key (`ALLOWED_SOURCE_IPS` still applies to `/openapi.json`).
- Rate limits: 120 reads/min and 10 writes/min per key (sliding window, `429` with `Retry-After`).
- **Upstream allowlist:** the gateway can call only the 17 methods in `gateway/allowlist.py` (11 restful + 6 native), plus the native login sequence (`TryLogin`, `GET /`). Clients check the allowlist before any network I/O, and tests enforce it.
- **Audit log:** every non-GET request, including rejected ones, emits one JSON line on the `gateway.audit` logger with `time, endpoint, sessionId, keyId, backend, status, result`. Request bodies are never logged.
- **Redaction:** every log record from any logger is scrubbed of configured secrets and of the native session cookies and anti-forgery token (which are registered when obtained). Validation errors never echo submitted values, and the `create_user` password is a `SecretStr`.
- Upstream timeout is 30 s. The native login is single-flight: concurrent requests share one login, and on 401/403/redirect the client re-logs in once and retries.
- The container runs as a non-root user.

## Tests

```bash
uv run pytest      # 73 tests
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
