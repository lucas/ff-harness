# User Stories — Website Builder

The product is a **website builder for non-technical small-business owners**; the harness governs the agent that builds the site. See `overview.md` for the system and `skills/bootstrap.md` for the onboarding flow these stories drive.

## Personas
- **Maria — restaurant owner.** Wants a menu, hours, location/map, and online reservations. Worried it'll look generic.
- **Jordan — coffee shop owner.** Wants a warm, inviting vibe, a menu, location, and an Instagram link.
- **Alex — social-media influencer.** Wants a polished "link in bio" page, social links, a media kit, and a bold aesthetic.
- **Sam — photographer.** Wants a clean portfolio gallery, an about section, and a contact form. Design-sensitive.
- **Pat — home-service owner (HVAC / lawn care).** Wants services, service areas, reviews, and a prominent "call for a quote" button. Cares about being found on Google.

Common thread: non-technical, time-poor, unsure of specifics — they want professional results, to be **findable**, and **guidance/defaults** so they're never stuck.

## Epic 1 — Onboarding (bootstrap)
- As a business owner, I want to be **asked simple questions** about my business so the site reflects me without my knowing web design.
- As an unsure user, I want **a sensible default for every question** so I can accept suggestions and keep moving.
- As Maria, I want to give my **name, industry, hours, address, and phone** so customers can find and reach me.
- As Pat, I want to set my **service areas** so local customers know I serve them.
- As Alex, I want to add **social links and a bold aesthetic** so the page matches my brand.
- As any user, I want to pick an **aesthetic and color scheme** (or accept the industry default) so it looks the way I want.
- As any user, I want to confirm a **summary of everything I provided** before any building begins.
  - *Acceptance:* every question has a baked-in default; answers form a **Business Brief**; the brief is approved before generation and becomes part of the session context.

## Epic 2 — Mockup & approval
- As a non-technical user, I want to **see a simple ASCII mockup** of the layout before it's built, so I can confirm structure without reading code.
- As a user, I want to **approve, tweak, or reject** the mockup so I stay in control.
  - *Acceptance:* the mockup is derived from the same layout spec that builds the page; nothing is generated until I approve.

## Epic 3 — Site generation
- As a user, I want a **simple, fast HTML+CSS+JS page** matching my brief.
- As Sam I want a **portfolio gallery**; as Maria a **menu**; as Pat a **services + service-areas** section — the right pages for my industry by default.
- As any user, I want a clear **call-to-action** (reserve, order, book, call for a quote, follow) appropriate to my business.

## Epic 4 — SEO & being found
- As Pat, I want my site **SEO-optimized** (title, meta description, structured data, local info) so customers find me on Google.
- As any user, I want `sitemap.xml`, `robots.txt`, and `llms.txt` generated and kept current, so search engines and AI assistants can discover my site.
  - *Acceptance:* SEO checks pass as a deterministic gate each iteration.

## Epic 5 — Changes & versioning
- As a user, I want to **request a change in plain language** ("make the header green", "add a specials section") and have it applied.
- As a user, I want every change **saved automatically** and to be able to **undo** to a previous version, so I can experiment safely.
  - *Acceptance:* each accepted change is auto-committed to local git; prior versions are restorable.

## Epic 6 — Guardrails & help
- As an unsure user, I want the harness to **stop and ask** when something is ambiguous rather than guess.
- As a user, I want it to **pause for my approval** if it's iterating a lot, so it never runs away on my behalf (or my budget).

## Epic 7 — Final intent audit
- As a user, I want a **final check that the finished site matches what I asked for** — my name, contact details, services, locations, and the look I chose — so I can trust it before publishing.
  - *Acceptance:* an audit step compares the generated site against the Business Brief; mismatches raise an `intent_mismatch` alarm and are surfaced for correction or approval.
