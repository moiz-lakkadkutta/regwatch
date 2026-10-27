# Devpost submission — RegWatch

Paste-ready text for the Devpost form. Tracks: **Best Apps and Agents**, and the **Best Use of Tavily** prize.
Every figure below is from a live run unless it says otherwise; sources are `README.md` and `FEEDBACK.md`.

**Name:** RegWatch

**Tagline (≤ 200 chars):** Tells a small EU online shop which new rules apply to it and what to do by when. Every
to-do quotes the official sentence it comes from. Nemotron on Nebius + Tavily.

**Links**
- Live demo: https://port8000-jmda1596h1jt4g9.tunnel.applications.eu-west2.nebius.cloud (stays up through judging, Dec 1–15)
- Code (MIT): https://github.com/moiz-lakkadkutta/regwatch
- Video: _add after upload_

**Built with:** python, fastapi, jinja2, htmx, sqlite, sqlmodel, nebius-token-factory, nebius-ai-cloud,
nvidia-nemotron, tavily, docker, github-actions

---

## Inspiration

A one-person shop in Germany selling oak tables and leather bags across the EU has no compliance team. In 2026
alone it has to add a "withdraw from contract" button to its checkout (Directive (EU) 2023/2673, § 356a BGB, from
19 June 2026), put the new EU guarantee notice on its product pages (Directive (EU) 2024/825, from 27 September
2026) and check whether the EU Deforestation Regulation covers its wood and leather. That regulation has been
postponed twice; for a small shop the wooden furniture is now due 30 December 2026 and the leather 30 June 2027.
The law is public, but it is hundreds of pages across EUR-Lex, German ministries, chambers of commerce and
platform help pages. Generic chatbots answer confidently and cite nothing. A shop owner needs the opposite: a
short list of what to do by when, and proof for every line.

## What it does

You describe your store in six fields: country, where you ship, what you sell, which shop platform, company size,
and whether you import your goods or buy them in the EU ("not sure" is allowed). RegWatch then shows:

- **This month** (`/month`): to-dos grouped as *Already in force*, *Due this month*, *Coming up*, *No fixed date*,
  each with a check-off.
- **Feed** (`/feed`): every regulation change it found, marked **applies**, **doesn't apply** or **unclear**.
  "Unclear" is a real answer: it names the missing fact that would decide it (for EUDR: whether you are the
  first to place the goods on the EU market).
- **Change page**: why it applies in your store's terms, the to-dos, the verbatim sentence each to-do comes from,
  and a link to the official page.
- **"This deadline moved"**: when an EUDR date was postponed by Regulation (EU) 2025/2650, a banner quotes the
  old and the new sentence from the two acts, so the owner sees why the date is not the one they read about.
- **Let the agent check**: on an unclear change, a Nemotron 3 Ultra agent searches the allow-listed sources with
  Tavily, reads a page, asks the owner one multiple-choice question and decides. Each step shows its Tavily
  credits and model cost, and its result passes the same grounding checks as the crawl.
- **Four example stores**, each classified live in advance: wooden furniture in Germany (small), a coffee
  importer in France (larger), a clothing shop in the Netherlands and a stationery shop in Austria. Judges can
  switch between them without touching the shared profile. Entering your own store is closed on the public
  demo on purpose: the URL is open to anyone with no login, and every new store costs about 35 Nemotron 3 Ultra
  calls (about $0.24). Running locally, `/profile` takes your own six answers.

The demo covers five EU laws: the withdrawal button (2023/2673), the Deforestation Regulation (2023/1115), the
green-claims directive (2024/825), the packaging regulation (PPWR, 2025/40) and the product-safety regulation
(GPSR, 2023/988). For the default furniture store the live data has 33 changes from 9 official and guidance
pages: 13 apply, 16 don't, 4 unclear, 51 to-dos, each with its quote and source.

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
   unclear, a reason in the owner's terms, and to-dos that must cite one of the extracted obligations. It also
   gets the date notes for the store's products, so it writes one to-do per product group when the dates differ
   (live: wooden furniture 30 Dec 2026, leather 30 June 2027).
5. **Agent — Nemotron 3 Ultra + Tavily.** For an unclear change, a loop of at most 6 steps where the model picks
   one tool per step: Tavily search on one allow-listed domain, read a page it was shown, ask the owner, or
   decide. Capped in code at 10 credits per run and $1 + 24 Tavily credits per day for all visitors together.
6. **Check in code, not in the prompt.** A quote is stored only if it is a substring of the full page
   (`RegChange.validate_quotes()`); a to-do only if its quote is one of the change's obligations
   (`Applicability.ungrounded_todos()`). One retry with the rejected quotes listed, then anything still
   ungrounded is dropped. Citation links and due dates are set by code; a model date that is not one of the
   change's effective dates is removed. In the recorded live crawl: 105 to-dos stored, 0 ungrounded.
7. **Run on Nebius AI Cloud.** One Docker image, built by GitHub Actions, two modes: the web app as a
   **Serverless Endpoint** (eu-west2, 2 vCPU / 8 GB) and the crawl as a **Serverless Job**, with API keys from a
   Nebius MysteryBox secret. A Job without the secret refuses to start rather than fall back to test data.
8. **Measure everything.** Every Tavily response's `usage` and every model call's tokens are logged; the feed
   footer shows the credits and dollars of each crawl, and the agent card shows them per step.

**Where Token Factory sped us up.** Both Nemotron models sit behind one OpenAI-compatible API and one key, so
the extractor and the classifier are two env vars (`REGWATCH_EXTRACTOR_MODEL`, `REGWATCH_CLASSIFIER_MODEL`). Trying
Ultra as the extractor, or Lightning with thinking on, took a config change and a rerun, not a new client. Every
response carries token `usage`, which is how the app prices each crawl and each agent step at catalog rates
(Lightning $0.06 / $0.24, Ultra $1.00 / $3.00 per Mtok). Lightning does the bulk work cheaply: 115 calls, 683k
prompt tokens, $0.05 per crawl. Ultra is used only where judgement matters, which is where about 87% of the spend
goes.

| Live run | Figure |
|---|---|
| Full crawl, 2026-10-06 | 8 min 03 s, 40 Tavily credits (14 searches, 2 extracts), $0.41 in tokens |
| Lightning, 29 documents → 60 changes | 682,890 prompt / 58,210 completion tokens, $0.0549 |
| Ultra, 60 changes | 102,279 prompt / 84,830 completion tokens, $0.3568 |
| Nebius Serverless Job, 2026-10-10 | 52 new documents, 47 new changes, 52 Tavily credits, $0.25, about 9 min |
| Three more laws, 2026-10-15 | 43 Tavily credits (10 searches, 4 extracts), 7 new changes |
| One example store classified, 2026-10-19 | 33 changes, 35 Ultra calls, $0.2363, 0 ungrounded |

## Challenges we ran into

- **The model said the withdrawal-button law didn't apply.** The key article (Art. 11a of 2023/2673) sat past a
  60,000-character input cap, and the act's title framed it as financial services. Prompt rules, thinking mode and
  using Ultra as the extractor all failed. What worked: split long acts per article, raise the cap to 200k
  characters, and name each section by the short act id. Art. 11a now comes out as **applies**; the feed folds
  the provisions that don't apply into one line per act.
- **The law moved while we built.** Regulation (EU) 2025/2650 postponed the EUDR again, with an exception for
  wood products already covered by the old timber regulation. The dates are now data with their own grounding
  check (the banner's sentences must be verbatim in both acts), not hard-coded text.
- **Wrong dates from the model.** For the green-claims directive, Lightning picked the member states'
  transposition deadline (27 March 2026) as the shop's due date in two of three runs; the consolidated GPSR text
  gave its consolidation date. Those sections stayed out of the demo, and a test now fails if any EUDR to-do in the
  demo data is dated before the rules reach shops.
- **Lightning answers were cut off.** Replies stopped at 4,096 tokens because the model reasoned inside the
  answer. `/no_think` and `reasoning_effort` changed nothing; `chat_template_kwargs: {enable_thinking: false}` fixed
  it.
- **Off-target pages.** The first live crawl kept 109 documents, many from the wrong Shopify sections or in other
  languages. A path rule for shopify.com, an English/German filter and content dedupe brought it to 36, and Tavily
  use fell from 72 to 42 credits.
- **No shared disk between Endpoint and Job.** We found no documented persistent volume, so the Job cannot update
  the web app's database, and a scheduled daily crawl would not reach the demo. The demo serves a seed recorded
  from live crawls (`regwatch record-seed`); new results ship by re-recording and redeploying. Everything is in
  `FEEDBACK.md` with numbers.

## Accomplishments that we're proud of

- No to-do without proof: grounding is enforced by code at the point of storage, and a test checks that a
  leaked ungrounded to-do is never stored. The agent's evidence and the deadline banner pass the same kind of check.
- It's honest about what it doesn't know: "unclear" is a first-class result, with the missing fact named, and
  the agent can resolve it by asking one question.
- It's cheap and measured: about $0.41 and 40 Tavily credits per full crawl, shown in the app.
- 492 automated tests, ruff, mypy --strict and CI on every push; the whole pipeline also runs offline from
  recorded replies, so anyone can run it without keys.

## What we learned

- The fix for hallucinated citations is a substring check, not a better prompt.
- Long legal texts need structure-aware splitting; a bigger context window alone missed the one article that
  mattered.
- Restricting Tavily to a short allow-list of official domains makes the search both better and cheaper.
- Dates are the hardest part of legal text: an act has many dates, and only some of them are a shop's deadline.

## Testing instructions

- Open the live demo. No login, no keys, no cost to the tester. It lands on **This month** for the default store
  (wooden furniture, Germany, small company).
- Use **"See the results for another example store"** (top of each page) to switch to the other three: coffee importer in France,
  clothing in the Netherlands, stationery in Austria. Each was classified live in advance. Entering a new store is
  switched off on the public demo: it is an open URL with no login, and checking a new store costs real AI API
  spend (about 35 Ultra calls, ~$0.24 each time), so the shared profile stays locked. To try your own store, run
  it locally (below), fill in `/profile`, then run `uv run regwatch crawl --stage classify` with a
  `NEBIUS_API_KEY`.
- Open any to-do to see its verbatim quote and the source link. `/feed` lists every change with its verdict, and the
  footer shows the credits and dollars of the crawl.
- **Let the agent check:** open the unclear deforestation change (`/change/12`) and press the button. It runs live
  on Nemotron 3 Ultra + Tavily. All visitors together share a cap of $1 + 24 Tavily credits per day. The card shows
  today's spend; if the cap is reached it says "Try again tomorrow (UTC)", and everything else keeps working.
- Run it yourself without keys: `uv sync --dev && uv run regwatch seed && uv run regwatch serve` (see README). The
  whole pipeline also replays offline: `uv run pytest -q`.
- The demo URL stays up until the end of judging (15 December 2026, 12:00 PT).

## Feedback on Nebius Token Factory, AI Cloud and NVIDIA models

Full dated log with numbers: `FEEDBACK.md` in the repo. The points that cost us the most:

1. **Nemotron 3.5 Lightning writes its reasoning into `content`.** By default it reasons, puts the trace
   ("Here's a thinking process: …") in `message.content` and leaves `reasoning_content` empty. In our second live
   crawl, 53 of 63 calls ended at exactly 4,096 completion tokens mid-JSON, and 24 of 36 documents failed.
   `/no_think` and `reasoning_effort="low"` changed nothing. `chat_template_kwargs: {enable_thinking: false}` gave
   262 tokens of valid JSON in 1.3 s instead of 13.9 s. Ask: document the switch on the model card and return the
   trace in `reasoning_content`.
2. **No in-place update for Serverless Endpoints.** `nebius ai endpoint` has create / delete / start / stop /
   restart but no `update`, so a new image means a new endpoint and a new public URL. We went through five URLs
   (web → web-5) and had to edit every doc each time. Create → RUNNING was a steady ~254 s. Ask: `endpoint update`,
   or a stable URL per endpoint name.
3. **No storage shared between a Job and an Endpoint.** We found no documented persistent volume for Endpoints, so a
   Serverless Job (which ran fine: 531 s, 52 documents, $0.25) cannot update the web app's database. Jobs also have
   no schedule field. A daily crawl would have needed a GitHub Actions workflow and 4 repo secrets, and its results
   still would not reach the demo.
4. **Token Factory access doesn't mean an AI Cloud tenant.** With only a Token Factory project,
   `iam v2 project get` said "id must be a project id with 'project-' prefix", `tenant list` returned `{}` and
   `registry list` returned PermissionDenied "unknown subject". Nothing in the hackathon resources says AI Cloud
   needs its own tenant and billing. It worked within seconds once those existed.
5. **Misleading CLI errors.** A Job whose container started and exited 2 reported "Job failed before startup
   completed". An instance-quota failure (`compute.instance.count (limit 2, requested 3)`) came with hints about
   profiles and authentication. `nebius profile create` fails under a non-TTY with `Error: ^D`.
6. **What worked well:** `--dry-run` on endpoint and job create (please make it more prominent in the docs),
   MysteryBox secrets via `--env-secret` first time, anonymous pulls from a public GHCR image, `endpoint logs`
   showing a startup traceback (a 5-minute fix), and Nemotron 3 Ultra's judgement. With per-product date notes it
   split wooden furniture (30 Dec 2026) from leather (30 June 2027) with no hand-written rules, for $0.24 for a
   whole store.

## What's next

- "Check my store" on the hosted app: a profile per visitor, checked on demand in the background under a daily
  spend cap. It is closed today because the demo URL is public and each check costs Nemotron calls.
- The European Accessibility Act and more EU countries' guidance pages.
- A persistent database shared by the Job and the Endpoint, so a scheduled crawl updates the app directly.
- Alerts when a change that applies to you is published, and dates in the owner's language.
