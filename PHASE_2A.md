# Phase 2A browser identity

## Installation and configuration

`smackend init my-project --auth` installs the optional dependency extra in the generated manifest and sets `SMACKEND_AUTH_ENABLED=true`. The base scaffold remains runnable without this extra. Run ordinary Django migrations, then visit `/account/`. Allauth owns the browser protocol under `/identity/browser/v1/` and supplies its OpenAPI document at `/identity/openapi.json`. Ninja documents Smackend's account capabilities and profile, username and deactivation routes under `/api/docs`.

Process variables override the private `.env`. Registration entries with `!` are required. The fields and the sign-in methods are independent. Password and code sign-in may use different identifiers. A self-service registration profile must enroll at least one required credential the user can actually authenticate with.

| Variable | Default and meaning |
|---|---|
| `SMACKEND_AUTH_SIGNUP_FIELDS` | `username!,password!`. The supported fields are username, email, phone and password. |
| `SMACKEND_AUTH_PASSWORD_LOGIN` | `username`. Comma-separated login identifiers. Leave empty for code-only signup. |
| `SMACKEND_AUTH_OTP` | Empty. Set to `email`, `phone` or `email,phone` for code login. |
| `SMACKEND_AUTH_EMAIL_VERIFICATION` | `none`. Use `optional` or `mandatory` whenever email is collected. Allauth requires mandatory verification to use signup verification codes. Optional verification uses a link whose token is in the browser URL fragment. |
| `SMACKEND_AUTH_REQUIRE_PHONE_VERIFICATION` | `false`. Set true with `phone!` to require proof during signup. Optional phone numbers stay unusable as login identifiers until verified in the account workflow. |
| `SMACKEND_AUTH_ALLOW_SIGNUP` | `true`. Set false for operator-managed enrollment. |
| `SMACKEND_AUTH_PASSWORD_RESET` | `false`. Set true with email collection and password login to send recovery codes to verified email addresses only. |
| `SMACKEND_AUTH_SESSION_SECONDS` | `28800`. Absolute browser session lifetime from successful login, between 300 and 86400 seconds. |
| `SMACKEND_AUTH_EMAIL_BACKEND` | Django local-memory mail in development. Configure a production backend for email profiles. For Django SMTP, set `SMACKEND_AUTH_EMAIL_HOST`, optionally `SMACKEND_AUTH_EMAIL_PORT` (587), `SMACKEND_AUTH_EMAIL_USER`, `SMACKEND_AUTH_EMAIL_PASSWORD`, and `SMACKEND_AUTH_EMAIL_FROM`. The bundled SMTP configuration uses STARTTLS and a five-second timeout. |
| `SMACKEND_AUTH_SMS_BACKEND` | A dotted path to a callable taking `(destination, text)`. Use a bounded provider timeout. `smackend.identity.delivery.memory_sms` is available for in-process tests. |
| `SMACKEND_AUTH_GOOGLE` | `false`. When true, set `SMACKEND_AUTH_GOOGLE_CLIENT_ID` and `SMACKEND_AUTH_GOOGLE_CLIENT_SECRET`. Register the deployment origin plus `/identity/google/login/callback/` with Google. |

Example profiles include username plus password, email plus password with mandatory email proof, phone plus password with required SMS verification, email-only or phone-only code sign-in, and combined signup with individually selected sign-in identifiers. Add optional contact fields to a username/password profile to allow later verified sign-in after enrollment.

The PostgreSQL integration matrix covers each of the three password identifiers, email-only and phone-only code signup and login, all three password identifiers in one mandatory email and phone verification profile, and a username/password profile with optional contact collection and later verification. It also checks ambiguous credential input, invalid configuration, wrong codes, limited resends, expired and concurrent proofs, and the disabled module. The matrix covers these supported profiles. App-specific policy combinations can add their own tests.

The upstream OpenAPI schema describes the browser protocol. Use the project's `/api/account/capabilities` endpoint as the source of enabled registration fields and sign-in methods.

## Lifecycle and authorization

Use the allauth browser session and CSRF token with each unsafe request. Refresh the token from the cookie after login. Public registration, login, code and reset APIs use the same protections as authenticated mutations. Google OAuth uses upstream state and PKCE. A user must sign into a local account, reauthenticate, and explicitly connect a Google subject before Google sign-in works. A matching provider email cannot claim or automatically link a local account. Native Google token-login routes and social signup are closed by this profile.

The account routes support profile access and allowlisted name updates. Username changes, deactivation and identifier enrollment require recent proof. Allauth verifies email and phone changes. Old verified email aliases remain valid sign-in identifiers until explicitly removed. Password changes invalidate sessions by rotating Django's password-derived session hash. Allauth lists and revokes browser sessions by user. The toolkit revokes other tracked sessions when a verified phone or email identity changes and all tracked sessions on deactivation. `is_active` is checked on subsequent authentication.

Anonymous code and recovery requests use generic responses. Pending contact claims do not grant authentication or reserve a verified identifier. The database enforces one verified owner of a canonical email or phone number and case-insensitive uniqueness for scaffold usernames. Username signup has a public availability outcome. Applications that treat usernames as private identifiers should use verified-contact signup without a public username field. Review deactivated identity retention and phone-number recycling as product policies.

Use `session_auth` on private Ninja routers and Django groups and permissions for roles. `require_permission`, `require_staff` and `require_admin` express distinct checks. `owner_scope` and `authorized_scope` return ordinary querysets. Use the same policy with `authorized_object` for detail and mutations. Scope before paging, counting or exporting, derive owner IDs from the authenticated actor, and recheck permitted relationships in a transaction for concurrent state changes. Django's default backend supplies model permissions. Object-specific permissions can come from an application policy or an application-supplied Django authorization backend.

## Quotas, delivery and operations

The browser guard uses short PostgreSQL upserts with the server's clock. Counters have keyed digests, and aggregate admission is checked before creating per-address or per-identifier keys. The defaults per 60-second window are 600 identity operations globally, 60 per direct peer, 10 password attempts per identifier, 5 delivery requests per identifier, and 20 challenge submissions per browser session. A separate delivery budget caps outgoing messages at 120 globally and 6 per recipient. Override individual budgets in ordinary Django settings via `SMACKEND_AUTH_LIMITS`. Limits apply across workers sharing the PostgreSQL database and Django secret. Fixed windows permit bursts across the boundary. Gateways should provide their own transport-level overload protection.

Rate-limit errors return 429 with `Retry-After`. Database admission failure returns sanitized 503 and blocks credential processing. Browser account writes are serialized by session around Django's session load and save. Upstream bounds verification attempts, code expiry and resends. Concurrent writes to one browser session may return 409. Delivery runs after the transaction and releases its database checkout before the provider call. Delivery failures are logged without addresses, codes or provider responses. The request response remains generic. Ask the user to retry or resend after recovery. The baseline has no durable outbound queue or delivery guarantee.

Schedule `python manage.py clearauthlimits` for bounded cleanup of expired rate rows and Django's `clearsessions` for expired sessions. Use the actual peer address or let a trusted, explicitly configured ingress supply it through the ASGI server. The toolkit ignores arbitrary client-supplied forwarding headers. Use Secure cookies in production and suppress raw access URLs with Uvicorn's `--no-access-log` when running Smackend diagnostics.

## Extending and upgrading

The generated `accounts` app owns its user model and migrations. The installed `smackend.identity` app owns the verified phone and rate-limit tables. Allauth owns email, social and session tracking tables. Extend using `ACCOUNT_SIGNUP_FORM_CLASS`, subclasses of the configured adapters, app-owned models, and normal Ninja and Django routes. Keep identifier and privilege fields out of generic profile updates. The browser guard protects the documented endpoint prefixes. New custom authentication routes or an independently mounted admin login require corresponding CSRF, session and rate-limit controls.

The pinned allauth version supplies the native browser flow. Smackend uses its documented adapters and several version-bound headless hooks to enforce verified recipients and to coordinate session flow state. Re-run the full integration matrix before upgrading allauth. The installed migration adds a reversible case-insensitive index for verified emails to the upstream address table. Audit legacy verified contact collisions before applying it.

The account integration uses synchronous database auth and delivery adapters. Account-enabled ASGI deployments still need the Phase 0 handoff, connection contention and workload qualification in the roadmap.

For an existing v0.1.0a4 project, add the auth extra and merge the opt-in settings and URLs. Preserve `.env`, user data and migration history. Audit case-folded username collisions before applying the new project-owned uniqueness constraint. The migration never resolves or deletes conflicting records. For projects still using Django's built-in user model, plan a separate schema/data migration or equivalent username constraint before opting in. Existing sessions using a backend removed by the upgrade require sign-in again. Review existing unverified contacts before making them eligible for recovery or login.

## Verification

Set `SMACKEND_AUTH_SMOKE_PYTHON` to an interpreter containing the extra, and supply a local disposable `SMACKEND_SMOKE_DSN` with database creation rights. Run `python -m pytest tests/test_identity_config.py tests/test_identity_integration.py`. Each integration scenario creates a uniquely named PostgreSQL database and removes it afterward. The Google callback simulation patches the outbound token and user-info adapters while exercising the real application state, callback and linking paths. The mail and SMS backends capture synthetic messages. Live provider credentials and deployment qualification remain outside these tests.

## Google first-time registration evaluation

A future opt-in can admit a new local account after allauth has validated the Google callback, state, PKCE and issuer/audience assertions. Use Google's stable `sub` with its provider identity as the unique key and create the account and provider link atomically. A matching canonical local email should send the person through ordinary local sign-in and explicit reauthentication before linking. Apply the local profile's required fields and verification rules before opening a full session. Require local email proof where Google is not authoritative for the mailbox. [Google token verification guidance](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token)

The first-time registration option is deferred. Before enabling it, verify duplicate subjects under concurrency, verified and pending email collisions, provider-supplied email changes, signup-disabled profiles, absent required local fields, and recovery for accounts without passwords. Live Google callback and token validation, sender authentication and actual delivery for SMTP, and SMS delivery, cost and abuse limits remain unqualified. Existing Google sign-in keeps the explicitly linked local-account policy.

## Current qualification

See the [a8 qualification report](PHASE_4.md#live-integrations) for the status of live identity checks and direct Google registration.
