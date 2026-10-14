# Devpost submission — RegWatch

Paste-ready text for the Devpost form. Category: **Best Apps and Agents**. Every figure below is from a live run
unless it says otherwise; sources are `README.md` and `FEEDBACK.md`.

**Name:** RegWatch

**Tagline (≤ 200 chars):** Tells a small EU online shop which new rules apply to it and what to do by when — every
to-do quotes the official sentence it comes from. Nemotron on Nebius + Tavily.

**Links**
- Live demo: https://port8000-x8f9rz0wxq15br2.tunnel.applications.eu-west2.nebius.cloud
- Code (MIT): https://github.com/moiz-lakkadkutta/regwatch
- Video: _add after upload_

**Built with:** python, fastapi, jinja2, htmx, sqlite, sqlmodel, nebius-token-factory, nebius-ai-cloud,
nvidia-nemotron, tavily, docker, github-actions

---

## Inspiration

A one-person shop in Germany selling oak tables and leather bags across the EU has no compliance team. In 2026
alone it has to add a "withdraw from contract" button to its checkout (Directive (EU) 2023/2673, § 356a BGB, from
19 June 2026) and check whether the EU Deforestation Regulation covers its wood and leather (SME application from
30 June 2026). The law is public, but it is hundreds of pages across EUR-Lex, German ministries, chambers of
commerce and platform help pages. Generic chatbots answer confidently and cite nothing. A shop owner needs the
opposite: a short list of what to do by when, and proof for every line.

## What it does

You describe your store in four fields — country, where you ship, what you sell, which shop platform. RegWatch
then shows:

- **This month** (`/month`): to-dos grouped as *Already in force*, *Due this month*, *Coming up*, *No fixed date*,
  each with a check-off.
- **Feed** (`/feed`): every regulation change it found, marked **applies**, **doesn't apply** or **unclear**.
  "Unclear" is a real answer: it names the missing fact that would decide it (for EUDR: whether you are the
  first to place the goods on the EU market, and your company size).
- **Change page**: why it applies in your store's terms, the to-dos, the verbatim sentence each to-do comes from,
  and a link to the official page.

For the demo store (DE, ships to 10 EU countries, wood furniture + leather, Shopify) the live data has 29 changes
from 4 official and guidance pages: 12 apply, 16 don't, 1 unclear, 37 to-dos — each with its quote and source.

## How we built it

1. **Search — Tavily.** Advanced search per regulation × domain, restricted with `include_domains` to nine
   official and platform sources (eur-lex.europa.eu, commission.europa.eu, gesetze-im-internet.de, bmj.de,
   bmuv.de, ihk.de, green-forum.ec.europa.eu, help.shopify.com, shopify.com/blog) and a `time_range` window.
   Tavily is the only way RegWatch reads the web.
2. **Extract — Tavily.** Advanced extract to markdown in batches of ≤ 20 URLs; each page gets a content hash, so
   an unchanged page costs no model call on the next crawl.
3. **Normalise — Nemotron 3.5 Lightning** (Token Factory). Turns a page into instrument, effective dates and
   obligations, each with a quote copied from the page. Long EU acts are split per article and per amendment
   point first, so a 200-page act becomes many small, checkable calls.
4. **Classify — Nemotron 3 Ultra** (Token Factory). For each change and the store profile: applies / doesn't /
   unclear, a reason in the owner's terms, and to-dos that must cite one of the extracted obligations.
5. **Check in code, not in the prompt.** A quote is stored only if it is a substring of the full page
   (`RegChange.validate_quotes()`); a to-do only if its quote is one of the change's obligations
   (`Applicability.ungrounded_todos()`). One retry with the rejected quotes listed, then anything still
   ungrounded is dropped. Citation links and due dates are set by code; a model date that is not one of the
   change's effective dates is removed. In the recorded live crawl: 105 to-dos stored, 0 ungrounded.
6. **Run on Nebius AI Cloud.** One Docker image, two modes: the web app as a **Serverless Endpoint**
   (eu-west2, 2 vCPU / 8 GB) and the crawl as a **Serverless Job**, with API keys from a Nebius MysteryBox
   secret. A Job without the secret refuses to start rather than fall back to test data.
7. **Measure everything.** Every Tavily response's `usage` and every model call's tokens are logged; the feed
   footer shows the credits and dollars of each crawl.

| Live crawl (2026-10-06) | Figure |
|---|---|
| Tavily | 40 credits (14 searches, 2 extracts) |
| Lightning, 29 documents → 60 changes | 682,890 prompt / 58,210 completion tokens, $0.0549 |
| Ultra, 60 changes | 102,279 prompt / 84,830 completion tokens, $0.3568 |
| Whole crawl | 8 min 03 s, $0.41 in tokens + 40 credits |

The Nebius Job on 2026-10-10 found 52 new documents and 47 new changes for 52 Tavily credits and $0.25 in tokens,
in about 9 minutes, and left the 4 already-known pages alone.

## Challenges we ran into

- **The model said the withdrawal-button law didn't apply.** The key article (Art. 11a of 2023/2673) sat past a
  60,000-character input cap, and the act's title framed it as financial services. Prompt rules, thinking mode and
  using Ultra as the extractor all failed. What worked: split long acts per article, raise the cap to 200k
  characters, and name each section by the short act id. Art. 11a now comes out as **applies**; the feed folds
  the provisions that don't apply into one line per act.
- **Lightning answers were cut off.** Replies stopped at 4,096 tokens because the model reasoned inside the
  answer. `/no_think` and `reasoning_effort` changed nothing; `chat_template_kwargs: {enable_thinking: false}` fixed
  it.
- **Off-target pages.** The first live crawl kept 109 documents, many from the wrong Shopify sections or in other
  languages. A path rule for shopify.com, an English/German filter and content dedupe brought it to 36, and Tavily
  use fell from 72 to 42 credits.
- **No shared disk between Endpoint and Job.** We found no documented persistent volume, so the Job cannot update
  the web app's database. The demo serves a seed recorded from a live crawl (`regwatch record-seed`); new results
  ship by re-recording and redeploying. Everything is in `FEEDBACK.md` with numbers.

## Accomplishments that we're proud of

- No to-do without proof: grounding is enforced by code at the point of storage, and a test checks that a
  leaked ungrounded to-do is never stored.
- It's honest about what it doesn't know: "unclear" is a first-class result, with the missing fact named.
- It's cheap and measured: about $0.41 and 40 Tavily credits per full crawl, shown in the app.
- 289 automated tests, ruff, mypy --strict and CI on every push; the whole pipeline also runs offline from
  recorded replies, so anyone can run it without keys.

## What we learned

- The fix for hallucinated citations is a substring check, not a better prompt.
- Long legal texts need structure-aware splitting; a bigger context window alone missed the one article that
  mattered.
- Restricting Tavily to a short allow-list of official domains makes the search both better and cheaper.

## What's next

- More regulations (packaging/PPWR, GPSR product safety, accessibility act) and more EU countries.
- A persistent database shared by the Job and the Endpoint, so the daily crawl updates the app directly.
- Alerts when a change that applies to you is published, and dates in the owner's language.
