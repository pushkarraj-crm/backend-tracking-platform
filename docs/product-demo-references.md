# Trackier and Offer18 product references

Captured 2026-10-21 from recordings and transcript supplied by the user.
These demos are product references, not instructions to execute, a replacement
for the accepted PRD, or evidence that our platform has feature parity.
Code observations below identify foundations only; they are not acceptance tests.

## Sources

- Trackier campaign creation recording: 128 seconds. Reviewed sampled frames.
- Offer18 complete demo recording: 825 seconds. Reviewed sampled frames across
  the recording and the supplied timestamped transcript. Audio was not transcribed
  independently. Transcript timestamps refer to the source video; sampled recording
  times may differ because of playback or recording timing.

## Interface direction

Use Trackier's grouped creation form and Offer18's campaign workspace structure:
Overview, General, Targeting, Landing pages, Creatives, Publishers, Caps,
Events and payouts, Integration, Anti-fraud, and Activity.
Keep the current product branding. Use readable labels, helper text, consistent
save/error feedback, explicit currency, and clear active/paused state.
Show a campaign summary and relevant actions above the tabs. Retain role-scoped
views so publishers cannot see advertiser credentials, revenue, or internal notes.
Replace routine JSON entry with labelled inputs and condition rows. Keep advanced
configuration available only where a structured editor cannot express the rule.

## Functional comparison

| Demo area and transcript time | Current code foundation | Remaining work / acceptance target |
| --- | --- | --- |
| Dashboard, sales reporting, rankings, AI insights (0:06–0:20) | Scoped reporting and dashboard UI | Verify rankings and sales totals; define useful insights against real data; no claim of AI implementation |
| Campaign home/general (0:29–0:46) | Campaign create/profile, CPL/CPS, access, clone, active/paused | Build coherent overview and navigation; verify create/edit/reload consistency |
| Landing variants, targeting, weighted fallback (0:44–1:24) | Campaign destinations with weights, country/publisher restrictions; profile fallback URL | Nested fallback pages and expanded per-page conditions are not equivalent to existing weighted variants; define failure routing before extending |
| Targeting at click/conversion/impression (1:26–1:34) | Tracking rules and conditional click/conversion rules | Structured condition builder; verify impression policy separately |
| Creatives (1:36) | Banner/logo/text asset URLs | Direct file upload/storage and previews need separate implementation |
| Advertiser/affiliate caps, actions and thresholds (1:41–2:10) | Campaign/publisher/event period caps for clicks, conversions, revenue, payout | Threshold alerts, selectable overflow actions and any cross-campaign advertiser aggregate cap need explicit implementation/tests |
| Events and conditional publisher prices (2:12–2:35) | Named events, commission rules, publisher/event conditions | Replace JSON conditions with usable rule controls; verify rule priority and financial results |
| Fraud controls (2:47–3:48) | Bot checks, IP networks, frequency and minimum delay rules | Verify global versus campaign scope; VPN/proxy intelligence, suppression and retargeting are separate capabilities |
| S2S, pixel, iframe, GTM, Web SDK (3:51–4:12) | Postbacks, web tracking runtime/SDK and integration setup | Verify every advertised integration end to end; browser restrictions and cross-site claims require actual browser tests |
| Partner teams and employees (4:14–4:30, 9:46–10:17) | Partner accounts, staff permissions | Partner-company subaccounts, manager role templates and permission editing need comparison against accepted tenant model |
| Reports and logs (4:34–6:01) | Scoped filters/grouping/export, saved report templates, callback/conversion diagnostics | Column editor, previous-period comparison, KPI comparison and actionable fraud highlighting require verification/extension |
| Coupons and product feeds (6:04–6:44) | No coupon/feed implementation found in reviewed app/src files | Coupon attribution, assignments, quotas, CSV validation, feed storage and API ingestion; follow PRD attribution policy |
| MMP and Android SDK (6:46–6:58) | Integration configuration foundation | Functional MMP adapters, SDK and signed/idempotent event ingestion require separate delivery |
| Offer sync, mailer, conversion CSV (7:02–7:42) | Campaign import, bulk conversion status tools, integration configuration | Partner offer sync is not campaign import; conversion ingestion is not status update; bulk mailer needs explicit sending authorization |
| Optimization automation (7:47–8:51) | Campaign-level clicks/conversions/CR rules; notify/pause and cooldown | Affiliate/subsource levels, CTR/eCPM/UCR, whitelist, automatic resume and richer logs are extensions; cooldown is not automatic resume |
| Weighted token fillers and assignment rules (8:53–9:44) | URL macros, publisher assignments, smart links | Weighted token substitution and conditional bulk assignment are distinct features |
| Domains, branding, terminology and portal options (10:23–12:04) | Domains, branding settings, scoped portals | Verify custom portal domains; terminology mappings, configurable signup/report fields, chat/tickets need separate scope |
| APIs and SDK triggers/journeys (12:08–13:34) | Auth/campaign/report APIs, API keys and web SDK | Document exact supported contracts; URL/path/host conversion triggers, journey and cross-site tracking need verification/implementation |

## Recommended implementation sequence

1. Campaign workspace: overview, direct navigation from campaign list, grouped
   profile editing, integration guidance and consistent save/error feedback.
2. Structured targeting and payout rule builders, landing variants, cap usage
   and threshold alerts. Verify click, conversion and publisher access behavior.
3. Reports: filters, selectable columns, prior-period comparisons and diagnostics.
4. PRD attribution and financial foundations, including coupon attribution,
   reversals and ledger accounting before any real payment automation.
5. Commerce feeds, partner sync, mobile integrations and advanced automation as
   separately testable increments.

Preserve the existing PRD delivery constraints and deferred 2FA decision. Do not
activate providers, send bulk messages, transfer funds or deploy solely because
those actions are demonstrated in a reference video.
