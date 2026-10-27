# Video script — RegWatch (as recorded 2026-10-27, 2:23, limit < 3:00)

Recorded on the live URL (`https://port8000-jmda1596h1jt4g9.tunnel.applications.eu-west2.nebius.cloud`) by a
Playwright script: a 1920 × 1080 viewport with the page at 150 % zoom, a visible cursor, and an orange outline
on whatever the voice-over names. The voice-over is Amazon Polly (generative, voice "Matthew"), and the audio is
loudness-normalised to −16 LUFS. There is no music. The agent segment is a real live run on `/change/12`: it
asked one question, searched eur-lex.europa.eu and decided "doesn't apply". The run cost 4 Tavily credits and
$0.031. The wait for the agent is cut down, and the result card is held on screen while the voice-over finishes.

| Time | Screen | Voice-over |
|---|---|---|
| 0:00 | `/month`, default store; the store chip | "This is a one-person furniture shop in Germany that sells across the EU. Five EU laws change what it has to do. RegWatch reads the official sources, and tells the owner what to do, and by when." |
| 0:12 | The "deadline moved" banner | "The deforestation rules were postponed again in late 2025. RegWatch shows that move right at the top, quoting the amending regulation: leather goods now have until 30 June 2027, but wooden furniture keeps 30 December 2026." |
| 0:28 | *Already in force* groups; open the EUDR group | "Below it, every line is a concrete to-do with a date. Product-safety info on every listing applies today. The withdrawal button has been required since June. And the deforestation to-dos are split by product group." |
| 0:41 | An EUDR to-do's Details → change page; *Why this applies*, the quote, the source link | "Open any to-do, and you see why it applies to this store, and under each to-do, the exact sentence from the official text, with a link to it. If a quote is not word-for-word in the source page, the code refuses to store the to-do. The model never gets the last word." |
| 0:58 | `/change/12` (unclear) → **Let the agent check** | "Some answers are honestly unclear. Is this shop the first to place its goods on the EU market? A Nemotron 3 Ultra agent checks. It can search the official sources with Tavily, read a page, or ask the owner one question." |
| 1:10 | The agent's question → "Bought from an EU supplier or wholesaler" → its eur-lex search | "It asks where the goods come from. The owner answers: bought from an EU supplier. Then it searches EUR-Lex for who counts as the operator." |
| 1:21 | "How the agent checked" → Decided: doesn't apply | "So the supplier placed the goods on the EU market first, and the agent decides this article doesn't apply to this shop. Every step shows its Tavily credits and cost, and the result passes the same grounding checks." |
| 1:33 | Store switcher → "Coffee importer, France"; its banner and the 14 GPSR to-dos | "Switch to another store, and the answer changes. A larger coffee importer in France gets 30 December 2026 for the deforestation rules, and the extra product-safety duties of an importer." |
| 1:45 | `/feed` | "The feed lists every change RegWatch found: applies, doesn't apply, or unclear, each with its source." |
| 1:52 | "Under the hood" card, each stage appearing as it is named | "Under the hood, Tavily searches and extracts only nine official and platform domains. Nemotron 3.5 Lightning turns each page into obligations with verbatim quotes, and Nemotron 3 Ultra decides what applies to this store, both on Nebius Token Factory. Code checks every quote before anything is stored. A full crawl costs about 40 Tavily credits and 41 cents." |
| 2:15 | End card: name, one line, repo, MIT | "RegWatch runs on a Nebius Serverless Endpoint, and it's open source, under the MIT license." |

## Notes for a re-record
- Each agent run spends real credits (≤ 10 Tavily credits; the shared cap is $1 + 24 credits per day) and can
  decide differently. A take on the same day ended "still unclear", so keep the take whose decision matches the
  voice-over.
- The agent's result triggers `HX-Refresh` (a full reload), so anything injected into the page for the recording
  has to be re-applied after a reload.
- The feed footer of an example store reads "0 Tavily credits used when it was recorded", so the video
  highlights the verdict badges instead of the footer.

## After recording
- Upload to YouTube as **Public**, because the rules require a publicly visible video. Paste the link into
  `docs/devpost.md` → **Links** → Video, and into the Devpost form.
