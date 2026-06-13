---
name: bootstrap
description: Onboarding flow for non-technical small-business owners. Captures a structured Business Brief (name, industry, contact, locations, hours, aesthetic, audience, colors, pages, CTA, socials) with industry-aware defaults, confirms it, and makes it part of the session context that drives the build and the final audit.
---

# Bootstrap — onboarding skill

Guide a non-technical small-business owner from a vague idea to a confirmed **Business Brief**. The user may be unsure about everything, so **every question carries a sensible default** — present it, let them accept ("sounds good") or change it, and move on. Ask in **small batches** (2–4 questions), never all at once. Use the `ask_user` tool to ask and `request_approval` to confirm the final brief.

## How to run it
1. Greet briefly; ask what the business is (free text). Infer the **industry** if you can; otherwise ask.
2. Load the matching **industry default profile** (below) to pre-fill aesthetic, colors, pages, and CTA.
3. Walk the question groups, always showing the default. Capture answers into the Business Brief.
4. When complete, present a **one-screen summary** of the brief and get sign-off with `request_approval`. Do **not** start building until approved.
5. The approved brief becomes part of the session context (persisted), so every later step stays accurate, and the final **intent audit** checks the site against it.

## Questions (each with a default)
**Identity**
- Business name — *default:* placeholder "Your Business Name" (required before publish).
- One-line description / tagline — *default:* generated from the industry.
- Industry — *default:* inferred from the description.

**Contact** (publish only what they want public)
- Phone — *default:* omit if not provided.
- Email — *default:* a contact form rather than a raw address.
- Physical address — *default:* omit; show "Serving {area}" instead.

**Location & reach**
- Service areas / locations — *default:* the user's city.
- Hours of operation — *default:* industry-typical, or "by appointment".

**Look & audience**
- Aesthetic — *default:* the industry profile below.
- Color scheme — *default:* the industry palette below.
- Target market — *default:* the industry-typical audience.

**Structure & action**
- Pages / sections — *default:* the industry page set below.
- Primary call-to-action — *default:* the industry CTA below.
- Social links — *default:* none; offer Instagram/Facebook fields.
- Logo — *default:* a text wordmark.

## Industry default profiles
- **Restaurant** — warm/inviting; palette: deep red + cream; pages: Home, Menu, About, Hours & Location, Contact; CTA: Reserve a table; audience: local diners & families.
- **Coffee shop** — cozy/rustic; palette: warm browns + cream; pages: Home, Menu, About, Location, Instagram; CTA: Visit us / Order; audience: locals & remote workers.
- **Social-media influencer** — bold/modern; palette: high-contrast accent; pages: Home (link-in-bio), About, Media Kit, Contact, Socials; CTA: Follow / Work with me; audience: followers & brands.
- **Photographer** — minimal/gallery-first; palette: monochrome + one accent; pages: Home, Portfolio, About, Contact; CTA: Book a shoot; audience: clients seeking their style.
- **Home service (HVAC, lawn care, plumbing)** — clean/trustworthy; palette: blue (HVAC) or green (lawn) + white; pages: Home, Services, Service Areas, Reviews, Contact; CTA: Call for a free quote; audience: local homeowners.
- **Other / unknown** — clean/professional; palette: neutral blue-gray; pages: Home, About, Services, Contact; CTA: Contact us.

## Output: the Business Brief
A structured record of every field above (the chosen value, and whether it was a default). It is confirmed by the user, stored with the session, injected into the worker's context on every turn, and is the reference the final **intent audit** verifies the finished site against.
