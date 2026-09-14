# Open issues (no remote tracker yet)

- [ ] (oct17-verifier) profile_form.html: platform field lacks the inline `<p class="help">` the other three fields have (UX brief §3.3) — do on Oct 21.
- [ ] (oct17-verifier) tests/test_seed_run.py parametrises over glob with no guard — passes vacuously if seed files move; add a `assert len(files) >= 2` guard when next touched.
- [ ] (oct17-verifier) schemas.Document validates source_domain but not the url host — ingest must derive source_domain from the URL (done Oct 18, see below).
- [ ] (oct17-verifier) tests/test_app.py `assert "selected" not in r.text` scans the whole page — brittle.
- [ ] (oct18-verifier) extract.py: an extracted result whose URL no longer matches a Hit after normalize_url (redirect/canonicalisation) is stored with regulation_tags=[] and published_date=None, and then silently skipped by tag filters — log at WARNING when hit is None and consider matching by allowed_domain+path. (touch on Oct 23 hardening)
- [ ] (oct18-verifier) search.py: an exception raised after Tavily billed the call skips the ledger → credits undercounted; note in FEEDBACK when a live number exists.
- [x] (oct18-verifier) cli.py imports `DEFAULT_OUT` from jobs.crawl at module top → `regwatch --help` imports sqlmodel; make lazy when next touched. (Oct 19: `crawl` now delegates to `jobs.crawl.main` with a lazy import; `--out` defaults to None.)
- [ ] (oct18-verifier) Footer "N Tavily credits used" means the seed sum before any crawl and the last crawl's credits after — Oct 21 should label the two states distinctly.
- [ ] (oct18-verifier) examples/seed-run/documents.jsonl must be re-recorded from a LIVE ingest before the Oct 24 freeze so every demo citation points at real page text (Oct 23).
- [ ] (oct18-verifier) manifest: `shopify-help-eu-withdrawal` carries tags ['eudr','withdrawal'] only to exercise tag-merging; Oct 20 must not treat it as an EUDR source (its classify fixture is `unclear`/`does_not`, never `applies` for EUDR).
