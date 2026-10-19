# Video script — RegWatch (target 2:00, hard limit < 3:00)

Record on the live URL (`https://port8000-fje4arxh07epk8b.tunnel.applications.eu-west2.nebius.cloud`) in a desktop browser at 1280 × 800, zoom 125 %. Do one dry run first:
the agent spends real credits (≤ 10 Tavily credits per run, cap 24 per day), so record the agent segment in at
most two takes. Voice-over is in quotes; on-screen actions are in brackets.

| Time | Screen | Say |
|---|---|---|
| 0:00–0:12 | [`/month`, default store "Wooden furniture, Germany"] | "This is a one-person furniture shop in Germany. In the next 18 months five EU laws change what it has to do. RegWatch tells it what to do, and by when." |
| 0:12–0:35 | [Scroll *Already in force* → *Coming up*; open one group] | "Every line is a to-do with a date. Wooden furniture is due 30 December 2026, leather 30 June 2027: the deforestation rules were postponed again, and wood kept the earlier date." |
| 0:35–0:50 | [Click a to-do's change → change page; point at the quote and the source link] | "Under every to-do is the exact sentence from the official page, and a link to it. If the quote isn't word-for-word in the page, code refuses to store the to-do. The model never gets the last word." |
| 0:50–1:00 | [Point at the "deadline moved" banner] | "When a deadline moves, RegWatch quotes the old and the new law side by side." |
| 1:00–1:30 | [`/change/12` (EUDR Art. 6–8, unclear) → **Let the agent check** → answer its question] | "Some answers are honestly unclear. Here a Nemotron agent searches official sources with Tavily, reads a page, and asks me the one fact it's missing. Each step shows what it cost." |
| 1:30–1:45 | [Store switcher → "Coffee importer, France" → `/month`] | "Switch the store and the answer changes: a large coffee importer gets 30 December 2026 for everything, and the product-safety duties of an importer." |
| 1:45–2:00 | [`/feed` footer with credits and dollars; then the GitHub README] | "Tavily is the only way it reads the web, limited to nine official domains. A full crawl costs 40 credits and 41 cents of Nemotron on Nebius Token Factory, shown right here. Open source, MIT." |

## Before recording
- Use a fresh browser profile or a private window (no stale `rw_visitor` cookie, no extensions in the toolbar).
- Check `/change/12` is still the unclear EUDR change on the live URL (title "Regulation (EU) 2023/1115 —
  Article 6 – Article 8"); if the id differs, use the one from `/feed` → *Unclear*.
- If the agent hits the daily cap, record it again the next UTC day. Don't redeploy just to reset it: a
  redeploy changes the URL.

## After recording
- Upload as public or unlisted to YouTube. Paste the link into `docs/devpost.md` → **Links** → Video, and into
  the Devpost form.
