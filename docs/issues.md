# Open issues (no remote tracker yet)

- [ ] (oct17-verifier) profile_form.html: platform field lacks the inline `<p class="help">` the other three fields have (UX brief §3.3) — do on Oct 21.
- [ ] (oct17-verifier) tests/test_seed_run.py parametrises over glob with no guard — passes vacuously if seed files move; add a `assert len(files) >= 2` guard when next touched.
- [ ] (oct17-verifier) schemas.Document validates source_domain but not the url host — ingest must derive source_domain from the URL (done Oct 18, see below).
- [ ] (oct17-verifier) tests/test_app.py `assert "selected" not in r.text` scans the whole page — brittle.
