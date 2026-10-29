# Offer18 function-by-function review (29 Oct 2026)

Source: the full public [Offer18 knowledge base](https://knowledgebase.offer18.com/)
(162 pages, read from its `llms-full.txt` export on 2026-10-29). Each Offer18 function
is listed with what it does, Relay's status today, and its priority for Relay's
target market: **fintech CPL, D2C/ecommerce CPS and influencer marketing in India**.
This supersedes the scope notes in [offer18-feature-review.md](offer18-feature-review.md)
where they conflict (that file excluded ecommerce plugins and OTP; the product
direction has since moved to D2C and influencers).

Status: ✅ Relay has it · 🟡 partly · ❌ missing.
Priority: **P1** needed for the target market · **P2** valuable · **P3** low / other niches.

## 1. Offers (campaigns)

| Offer18 function | What it does | Relay | Priority |
|---|---|---|---|
| Create offer / General | Name, advertiser, category, app ID, external offer ID, preview URL, offer URL, revenue and payout model + price, currency, visibility, dates | 🟡 Has CPL/CPS, categories, preview URL, visibility, schedule. Missing: CPC/CPM/CPI models, app ID, external offer ID. Guided page is project 4 | P1 |
| Advertiser URL Builder | Detects the advertiser's platform (18 presets) and suggests an offer URL with the right click-ID parameter | 🟡 Platform templates build the *postback* side; no offer-URL suggestion | P2 |
| Tracking URL macros | `{tid}`, `{aff_id}`, `{sub_aff_id}`, `{ip}`, `{offerid}`, `{offername}`, `{useragent}`, `aff_sub1–10`, device IDs, `t_*` pass-through | 🟡 All except `{ip}` and `{useragent}` | P2 |
| Landing pages V2 | Several landing pages per offer, weights, per-page targeting, affiliate allow/block, show/hide to affiliates, fallback sub-landings, CSV import | 🟡 Weighted destinations by country and publisher. Missing: pre-landing type, per-page fallback, visibility, CSV import | P2 |
| Targeting | Allow/block country, state, city, browser, device, OS, OS version, device brand, ISP, language | 🟡 Country, device, OS, language, schedule, hours. Missing: state, city, browser, OS version, brand, ISP | P2 (state/city matter for Indian lenders) |
| Targeting rules v2 | Rules on clicks, conversions and impressions; 9 condition types; 30+ fields; actions include fallback offer/URL | 🟡 Click and conversion rules with conditions and approve/reject/pending. Missing: impression phase, fallback action | P2 |
| Creatives | Upload images/logos; HTML creatives with `{tracking_link}`, landing-specific and impression tokens | 🟡 Creative URLs and per-publisher HTML snippets. No file upload | P2 |
| Assign affiliates | Assign one or all affiliates, optional email alert | ✅ | — |
| Capping rules | Caps with over-capping action: pending / accept / reject / stop offer / fallback / fallback + approve | 🟡 Period caps (per publisher/event, timezone) that reject. No choice of action | **P1** (fintech lenders cap daily leads) |
| Events | Multi-conversion by click or by IP, auto/manual approval, conversion waiting time, postback IP whitelist, default event shown to affiliates, event-mismatch action | 🟡 Repeatable events, auto-approve, attribution window, IP allowlist. Missing: multi-conversion by IP, mismatch action (Relay rejects) | P2 |
| Payout rules | Up to 24 data points, 9 conditions, priority, affiliate visibility, CSV import | ✅ Commission rules with conditions and priority. Missing: visibility flag, CSV import | P3 |
| Anti-fraud tab | Click-spam defender (3 modes), click block filters, conversion validation filters, IPv4 ranges, unique-click limit, min/max CTIT, proxy block, blank-referrer block, referrer hiding (302/200 modes) | 🟡 Bot block, IP velocity, network blocks, min conversion time, rules on sub-IDs/CTIT. Missing: unique-click limit, proxy detection, referrer capture and blank-referrer block, referrer-hiding modes | **P1** (project 5) |
| Fallback / JS integration | Fallback URL or offer, advertiser JS tag, impression redirect, force affiliate landing | 🟡 Fallback URL and impressions. Missing: fallback *offer*, force landing | P2 |
| CPS offer | Revenue and payout as % of `sale` | ✅ | — |
| Revenue share offer | Advertiser sends the commission as `payout`; affiliate gets a % of it | ❌ | **P1** (common in Indian fintech/D2C programmes) |
| Smart offer | One URL rotating 2–50 offers; sort by random, ID, payout, revenue, profit, CR, weight | 🟡 Smart links exist; fewer sort strategies | P2 |
| Deep link | Affiliate supplies a destination URL on allowed hosts | ✅ | — |
| Pre-landing page | Transit page passing parameters to the final landing | ❌ | P2 |
| Offer categories | Category labels | ✅ | — |
| Coupons | Coupon codes per offer and affiliate with dates, quota, status, reports, CSV import; attribution via `&coupon=` | ❌ Relay stores a coupon value but has no coupon management or coupon-only attribution | **P1** (influencers, D2C) |
| Offer affiliate links (auto-assign) | Rules that auto-assign public offers to affiliates by payout, country, model | ❌ | P3 |
| Suppression list | Email suppression lists shared with affiliates | ❌ | P3 |
| Retargeting tag | Per-affiliate retargeting scripts | ❌ | P3 |

## 2. Affiliates (partners)

| Offer18 function | What it does | Relay | Priority |
|---|---|---|---|
| Create affiliate / profile | Name, company, address, mobile, social, account manager, postback delay, timezone, API access, self-edit, advertiser allow/block, fallback URL, invoice terms, tax details | 🟡 Users with role, parent, API access, permissions. Missing: manager, profile fields, tax, advertiser allow/block | P2 |
| Affiliate employees | Sub-logins under an affiliate with full or read-only permissions | ❌ (affiliates have sub-*publishers*, not team logins) | P2 (agencies need team logins) |
| Approve / reject affiliates | Pending self-signups with email notice | ❌ Partners are created by the admin | P2 |
| Manage offers per affiliate | Approve/reject offer requests | ✅ Applications | — |
| Generate tracking URL | Per affiliate per offer | ✅ Now in Offer18 format `/c?o=&a=` | — |
| Affiliate global postback | S2S, image pixel, iframe pixel; advanced headers/POST body; global, offer or event level | 🟡 Signed S2S callbacks with GET/POST templates and campaign overrides. Missing: pixel/iframe types, per-event postbacks | P2 |
| Postback test / deep test | Fires a test conversion and shows the partner's HTTP response | 🟡 Callback test events | P3 |
| API access | Enable API per affiliate | ✅ | — |
| Login as affiliate | Admin opens the affiliate's dashboard | ❌ | P2 (support) |
| Admin password reset | Admin sends a new password | ❌ Self-service reset only | P3 |
| Affiliate referrals | Referral commission (flat/% for N days) | ❌ | P3 |

## 3. Advertisers

| Offer18 function | What it does | Relay | Priority |
|---|---|---|---|
| Create / profile | Company, manager, timezone, postback IP whitelist, affiliate allow/block, private notes, tax | 🟡 Users with advertiser role; IP allowlist is per campaign | P2 |
| Advertiser's offers | List per advertiser | ✅ | — |
| Advertiser test link | Test conversion flow without affecting affiliates | 🟡 URL preview and link health | P2 |
| Login as advertiser | Admin impersonation | ❌ | P2 |

## 4. Postback integration (advertiser → platform)

| Offer18 function | Relay | Priority |
|---|---|---|
| Auto-generated global S2S postback (GET and POST) | ✅ Built in project 1 | — |
| Offer / advertiser-specific postbacks | ✅ Per-campaign postbacks kept | — |
| Image pixel and iframe pixel (cookie-based) | 🟡 Relay has a consent-gated capture endpoint and `relay.js`, not a drop-in conversion pixel | **P1** (D2C sites without developers) |
| Google Tag Manager template | ❌ | P2 |
| Auth token on postbacks | ✅ Secret in URL / header | — |

## 5. Ecommerce integrations

| Offer18 function | Relay | Priority |
|---|---|---|
| Shopify (custom pixel on `checkout_completed`, sale, currency, order ID, discount codes) | ❌ | **P1** |
| Indian Shopify checkouts: Shiprocket, GoKwik, Razorpay Magic Checkout, Flexype | ❌ | **P1** (most Indian D2C brands use one of these) |
| WooCommerce (plugin or `functions.php`) | ❌ | P1 |
| BigCommerce | ❌ | P3 |

## 6. Mobile attribution (MMPs)

| Offer18 function | Relay | Priority |
|---|---|---|
| AppsFlyer global attribution URL, normal and privacy postbacks | ❌ Requires Relay to register as an AppsFlyer integrated partner | P1 for fintech apps (business step first) |
| AppsFlyer click signing | ❌ | P2 |
| Adjust, Branch, Singular, Kochava | ❌ Each needs partner onboarding | P2 |
| Probabilistic attribution (iOS) | ❌ | P3 |

## 7. Reports and logs

| Offer18 function | Relay | Priority |
|---|---|---|
| Reports with templates, timezone, advanced filters, export | ✅ Grouped reports, saved templates, CSV export | — |
| KPI reports | ❌ | P2 |
| Conversion logs with bulk approve/reject | ✅ | — |
| Affiliate postback logs | ✅ Callback deliveries with retry | — |
| Advertiser postback logs with status reasons | ✅ Postback receipts | — |
| Capping logs with reset | 🟡 Cap usage visible; no log or reset | P2 |
| Click logs | ✅ | — |

## 8. Account configuration

| Offer18 function | Relay | Priority |
|---|---|---|
| Network name, timezone, currency, default managers | 🟡 No default managers | P3 |
| Custom login domain and custom tracking domain (CNAME + SSL) | ✅ Company hostnames and verified tracking domains | — |
| Terms and privacy for signups | ✅ | — |
| Global fallback URL | ❌ | P2 |
| Postback error alerts | ❌ | P2 |
| Default tracking-link tokens | ✅ Link shown with parameters | — |
| Test affiliate / test IP bypass | ❌ | P2 |
| GDPR data masking | 🟡 IPs hashed; `t_*` never stored | — |
| Custom appearance: favicon, logo, login background, terminology, font, invoice signature, chat widget | 🟡 Branding (logo, accent). Missing: terminology, signature, chat | P2 (terminology is close to our account-type wording) |
| Per-account SMTP (incl. Gmail OAuth) | ❌ Platform SMTP only | P2 |
| Affiliate settings: signup approval, payment methods, referral, referrer handling | 🟡 | P2 |
| Advertiser settings: payment methods, invoice CC, signup and report fields | ❌ | P3 |
| Notifications: admin/affiliate/advertiser, email + in-app; Telegram and Slack | 🟡 In-app notifications; Telegram/Slack are draft configs only | **P1** (Telegram is standard for Indian networks) |

## 9. Employees

| Offer18 function | Relay | Priority |
|---|---|---|
| Employees with module permissions and write access | ✅ Staff permissions | — |
| Permission templates (affiliate / advertiser / admin / billing manager) | ❌ | P2 |
| Clone permissions | ❌ | P3 |
| Assign affiliates/advertisers to managers; additional include/exclude access | ❌ | P2 |
| Report access by status | ❌ | P3 |

## 10. Integrations

| Offer18 function | Relay | Priority |
|---|---|---|
| Offer sync from advertiser platforms' APIs (schedule, payout %, filters, field rules, logs) | ❌ | P2 (networks onboard many offers this way) |
| OTP integration (mVAS send/verify OTP via advertiser API) | ❌ | P3 (fintech uses its own KYC) |

## 11. Tools

| Offer18 function | Relay | Priority |
|---|---|---|
| Mailer to advertisers/affiliates with templates and logs | ❌ | P2 |
| Import affiliates / advertisers | ❌ | P2 |
| Import offers | ✅ | — |
| Import conversions (with or without click ID, CSV or manual) | ✅ MIS upload (by click ID or reference) | — |
| Import affiliate postbacks | ❌ | P3 |
| Product feed (CSV/API import, affiliate download) | ❌ | P2 (D2C catalogues) |
| Automation (block offer/affiliate/sub-source on CR, CTR, EPC thresholds; logs) | ✅ Automation rules and logs | — |
| Link tester (instant + scheduled rules, health, stop offer) | 🟡 Instant link health; no scheduled rules | P2 |
| Fraud Fender (traffic breakdown by IP, proxy, ISP, OS…) | 🟡 Fraud events and reports, no dedicated view | P2 |
| Conversion risk monitor (click-to-conversion speed) | ❌ | P2 |
| 24Metrics / FraudScore | ❌ | P3 |

## 12. APIs and SDKs

| Offer18 function | Relay | Priority |
|---|---|---|
| Network API: offers list/create, capping, conversions, reports, login, partner create, password reset | 🟡 Scoped partner API keys; no full network API | P2 |
| Affiliate API: offers, request offer, reports, coupons, OTP, employee login | 🟡 Partner API keys cover campaigns, links, reports | P2 |
| Web SDK (click, impression, conversion; cookieless; user journey with UTM) | 🟡 `relay.js` capture; no conversion SDK | **P1** (needed for Shopify/Woo) |
| Android, iOS, Node SDKs | ❌ | P3 |
| Affiliate offer-table SDK | ❌ | P3 |

## 13. Affiliate-side integrations

| Offer18 function | Relay | Priority |
|---|---|---|
| Google Ads (gclid in tracking template; tag postback; Zapier offline conversions) | 🟡 gclid captured; no ad-platform postbacks | P2 |
| Facebook CAPI, TikTok Events API, Snapchat CAPI as postback types | ❌ | P2 (influencers running paid ads) |
| Deep link from affiliate dashboard | ✅ | — |
| Referral link | ❌ | P3 |

## 14. iGaming

Registration, deposit, bet, win, withdrawal and bonus events, GGR/NGR reports,
user and activity APIs. ❌ in Relay and **P3**: outside the target market.

## Recommended additions to the roadmap

In priority order for fintech, D2C and influencers. Each is a separate project.

1. **Coupons**: codes per campaign and partner, validity and quota, coupon-only
   attribution, coupon reports and import. Unlocks influencer and D2C tracking.
2. **Shopify + Indian checkouts** (Shiprocket, GoKwik, Razorpay Magic, Flexype) and
   **WooCommerce**, using a conversion pixel/Web SDK. Unlocks D2C without developers.
3. **Capping over-actions and fallback offer**: pending/accept/reject/stop/fallback.
4. **Revenue-share model**: commission from the advertiser's `payout`.
5. **Telegram and Slack alerts** for caps, postback errors and fraud.
6. **Login as partner**, **agency team logins**, **account managers**.
7. **Blocked sources and anti-fraud** (already project 5): referrer capture,
   unique-click limit, proxy detection.
8. **MMP partnership with AppsFlyer** (business registration) before building the
   fintech-app integration.
