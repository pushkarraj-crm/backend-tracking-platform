# Frontend API integration

The backend serves JSON and redirect endpoints. It does not build or serve a frontend.

## Registration options

Call `GET /auth/registration-options` to populate dropdowns. Do not hardcode UI labels as submitted values. Business types are:

| Value | Label |
| --- | --- |
| `brand_direct_advertiser` | Brand/Direct Advertiser |
| `agency` | Agency |
| `affiliate_ad_network` | Affiliate/Ad Network |
| `mobile_apps` | Mobile Apps |
| `media_buyer` | Media Buyer |
| `social_media_advertising` | Social Media Advertising |

The response also contains usage types, ISO country choices, preferred languages, referral sources, `registration_enabled`, `trial_days`, policy URLs, and `legal_version`. Do not submit the “Select Business Type” placeholder as a value.

## Create an account

`POST /auth/register`, JSON:

```json
{
  "name": "Jane Doe",
  "email": "jane@example.com",
  "password": "a-long-unique-password",
  "company": "Example Agency",
  "business_type": "agency",
  "usage_types": ["lead_generation", "e_commerce"],
  "country": "IN",
  "mobile": "+919876543210",
  "teams_enabled": true,
  "teams_email": "jane.teams@example.com",
  "preferred_language": "en",
  "referral_source": "search_engine",
  "terms_accepted": true,
  "legal_version": "1"
}
```

The form needs password creation in addition to the requested profile fields. `teams_email` is required only when `teams_enabled=true`; omit it otherwise. Usage types support multiple unique selections. Country uses ISO alpha-2 codes. Valid mobile numbers are saved as E.164; a leading `00` international prefix is accepted. Submit the `legal_version` returned by signup options; stale versions are rejected so the user can review updated policies. Agreement acceptance records the server's verified legal version and timestamp, not a client-provided timestamp.

The signup creates a workspace and its admin with exactly 15 days of trial access. No payment details or setup fee are requested. In production, the response is `201 {"verification_required":true,"email":"...","trial_days":15}`; the user must follow the email verification flow before login. Local development can return a login session directly. Registration is `403` when closed and `422` for invalid fields.

## Authentication and browser cookies

Login requires only email and password. Emails are globally unique; the tenant is resolved internally. Workspace IDs and MFA codes are not login inputs. Keep access tokens in memory and send `Authorization: Bearer <access_token>` for business requests. Never treat a client-selected role or tenant as authorization.

Use `credentials: "include"` on browser login, refresh, logout, and CSRF requests. The response sets a HttpOnly refresh cookie and returns `csrf_token` in JSON. The separate frontend cannot read the API host's cookies through `document.cookie`; retain the returned CSRF value in memory. After a page reload, fetch `GET /auth/csrf` with credentials to obtain it, then `POST /auth/refresh` with `X-CSRF-Token` and credentials. Refresh returns a new access token and CSRF token. Serialize refresh across tabs, for example with `navigator.locks`, because reuse revokes the session.

Set `CORS_ORIGINS` to the exact frontend origin, such as `["https://app.example.com"]`. For the default Strict cookies, host API and frontend on the same registrable domain with HTTPS. If they are truly cross-site, configure `COOKIE_SAMESITE=none` in production; browser third-party-cookie restrictions can still prevent sessions, so same-site hosting is preferred. Never use wildcard CORS with credentials.

Verification/reset emails link to `${FRONTEND_BASE_URL}/account?action=verify|reset#token=...`. The frontend submits the fragment token to `/auth/verify` or `/auth/reset-password`, then removes it from browser history. Request new emails with `/auth/request-verification` or `/auth/forgot-password`.


## Affiliates and publishers

An affiliate creates a publisher with `POST /users` using `role="publisher"`; the server sets the parent to the authenticated affiliate. Network admins may specify `parent_user_id` for a publisher. Publishers cannot create other users. Affiliates can create publisher links only for offers already assigned to that affiliate. `/users`, `/links`, `/offers`, `/conversions`, reports, and payout visibility enforce the hierarchy in the backend.

## Conversions and billing

Advertiser sends `POST /postbacks/{offer_id}` with `X-Postback-Key` and `{"click_id":"...","transaction_id":"..."}`. Revenue and payout come from the offer. The current attribution model supports one conversion per click. Identical retries return `200` and the original ID; new events return `201`; conflicting duplicates return `409`.

Workspace admins call `/billing/subscribe` with a configured plan. Redirect the user to its HTTPS `checkout_url`. Subscription access changes only through verified provider reconciliation, not a browser “payment succeeded” message. When signup's trial is still active, the first cycle is scheduled for the end of that trial. `/workspace` remains accessible after expiry so the frontend can show a renewal screen; business endpoints return `402`.

## Response conventions

List APIs accept `offset` and `limit` (maximum 100). Money is returned as decimal strings with currency. Dates are timezone-aware. Report `start`/`end` values must include a timezone and form a half-open interval. `400/422` indicate bad input, `401` invalid sessions, `403` denied access, `404` inaccessible/missing objects, `409` conflicts, `429` throttling, and `503` dependency failures. `X-Request-ID` correlates redacted logs. Validation responses omit submitted passwords and other raw input values.

## Current self-service campaigns

Use `/campaigns`, `/campaign-applications`, `/landing-pages`, `/publisher-invoices` and `/publisher-postback` as described in [the integration guide](campaigns-invoices-postbacks.md). Admin-created publishers can have no parent affiliate. Automated fund transfers and manual payout recording are retired. Advertisers pay publishers externally and publishers confirm receipt through invoices.
