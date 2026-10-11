# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.3.0] - 2026-10-11

### Added
- Device identity and activity evidence on every session response
  (`search_sessions`, `list_company_sessions`, `list_group_sessions`, `get_session`,
  and the session inside `get_session_details`):
  - `guestMachineSerialNumber`, `guestHardwareNetworkAddress`: strings
  - `guestClientVersion`: string
  - `guestInfoUpdateTime`, `lastGuestConnectedEventTime`, `lastGuestDisconnectedEventTime`: ISO-8601 UTC
  - `isDeleted`, `isEnded`: booleans

  All of them are `null` when there is no evidence. ScreenConnect "never" sentinels,
  invalid timestamps and timezone-less timestamps become `null`, and `false` is preserved.
- `X-Gateway-Truncated` header on list responses: `true`/`false` when the upstream
  reports truncation (native), `unknown` when it gives no signal (restful).
- The full Access fleet, including sessions with a blank company, can be listed with
  `GET /v1/sessions?filter=SessionType = 'Access'`.

### Fixed
- The native `/details` fallback returned a session with a blank id, name and
  company, because the native details payload omits them.
- Native event times (milliseconds before `BaseTime`) failed response
  validation with HTTP 500. They are now converted to ISO-8601 UTC.

### Notes
- Native equivalents were verified against restful values on a live instance.
- Native lists return `null` for serial, MAC and guest-info update time; those values are only
  available from native get-by-id and details, and lists make no per-session requests.
- Native `lastGuestConnectedEventTime`, `isDeleted` and `isEnded` are always `null`
  (no guest-only equivalent exists in that API).

## [1.2.0] - 2026-10-11

### Added
- `loggedOnUsers` on sessions, listing all signed-in users (e.g. on terminal servers).

### Fixed
- `guestLoggedOnUserName` was null on nearly every machine. Current agents report users in
  `LogonSessions` rather than the legacy fields. Interactive sessions with a signed-in
  user are now used, skipping `[Backstage]` and the `Console` login screen.
- Native session search returned nothing: the native `filter` is free-text
  search, not an expression language. Name search now uses text search plus an
  exact-name match, and filter expressions are restful-only.

## [1.1.0] - 2026-10-11

### Added
- Rewst-style company ids: Rewst stores org-mapping ids lowercased and trimmed, and these now
  resolve to the stored company. An exact match wins; otherwise a unique case- and
  whitespace-insensitive match is used and reported in `X-Gateway-Resolved-Company`.
  Ambiguous ids return `409` with `candidates`.

### Fixed
- `guestMachineName` and `operatingSystem` were always null from the restful backend,
  which nests them under `GuestInfo`.
- `isOnline` is now computed from `ActiveConnections` on both backends.

## [1.0.0] - 2026-10-11

### Added
- Initial release: one REST API over ScreenConnect's built-in web API and the
  RESTful API Manager extension, with GET-only reads (`GET /v1/companies` for Rewst
  organization mapping), backend preference and fallback, read/write API keys,
  rate limits, audit logging, secret redaction, an OpenAPI 3.1 spec, Dockerfile,
  and tests.

### Security
- Health probes are single-flight, so unauthenticated bursts can't fan out to ScreenConnect.
- The API key is checked before request bodies are parsed, and bodies are capped at 1 MB.
- A 60 s cooldown after a failed native login protects the service account from lockout.
- Upstream redirects are treated as failures. Requests have a 90 s overall deadline.
- Secrets are redacted in tracebacks, and responses are marked `Cache-Control: no-store`.
- The audit log records client IP and counts suppressed keyless entries.
- Optional `ALLOWED_USER_ROLES` limits the roles `create_user` can assign.
- Docker installs hash-pinned dependencies.
