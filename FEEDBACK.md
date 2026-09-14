# Feedback on Nebius, NVIDIA and Tavily tooling

Dated entries with numbers; this feeds the hackathon's mandatory feedback field.

- (Oct 18) Tavily, `tavily-python 0.8.2`: `TavilyClient(api_key=None)` does not raise — it silently
  goes keyless and still issues the HTTP request. Without a key on the dev machine this would have
  turned every test run into a network call, so `LiveTavilyBackend.__init__` refuses an empty key
  (`RuntimeError`) and all 39 tests run against a fixture backend. Suggest the SDK fail fast.
- (Oct 18) Tavily, offline fixture arithmetic for one ingest run over 2 regulations × their
  allow-listed domains: 14 advanced searches × 2 credits = 28, plus 1 advanced extract of 16 URLs
  (2 credits per 5 URLs → 8) = **36 credits per ingest**; estimated from documented pricing, live
  figure pending `TAVILY_API_KEY` (`uv run pytest -m live -s tests/test_ingest_live.py` prints it).
- (Oct 19) Nemotron 3.5 Lightning: quote-fidelity rate on normalisation (how often quotes are not verbatim).
- (Oct 20) Nemotron 3 Ultra: applicability accuracy on the 6 fixtures; structured-output retries.
- (Oct 23) Nebius Serverless Endpoint + Job: deploy steps, cold start, scheduling.
