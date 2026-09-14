<!-- fmt: off -->
# Oct 17 gap-fill plan — profile + config

All six orchestrator gaps verified against `f5714ac`. Gate currently green on the (wrong) 3.13 venv: `6 passed`. `uv lock --check --python 3.12` resolves cleanly → **uv.lock does not change**. One extra gap found: platform options are hardcoded in the template, so "renders back" for platform needs a single source of truth → add `config.PLATFORMS`.

## 1. Files

**Touch**
- `.python-version` (new, content `3.12\n`)
- `regwatch/config.py` (additive: `PLATFORMS`, `REGULATION_TAGS`, `sources_for`)
- `regwatch/schemas.py` (additive: `Document`)
- `regwatch/db.py` (additive: `DocumentRow.to_schema`)
- `regwatch/app.py` (pass `platforms` to the profile template; no behaviour change)
- `regwatch/templates/profile_form.html` (platform `<select>` loops `platforms` with `selected`)
- `tests/test_app.py` (append tests)
- `tests/test_config.py` (new), `tests/test_seed_run.py` (new), `tests/test_schemas.py` (append `Document` tests)

**Do NOT touch**: `regwatch/ingest/*`, `regwatch/understand/*`, `regwatch/jobs/crawl.py`, `regwatch/cli.py`, `examples/seed-run/*`, `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml`, `ops/*`, `docs/*` (except docs/plans/oct17.md), `CLAUDE.md`, `README.md`, `FEEDBACK.md`, the other templates. Nothing existing in `schemas.py` is removed or tightened.

## 2. Public signatures (exact)

`regwatch/config.py` — add after `EU_COUNTRIES`:
```python
PLATFORMS: list[tuple[str, str]] = [("shopify", "Shopify"), ("woocommerce", "WooCommerce"), ("other", "Other")]
REGULATION_TAGS: frozenset[str] = frozenset(r["tag"] for r in SEED_REGULATIONS)

def sources_for(tag: str) -> list[str]:
    """Allow-listed domains to search for one regulation tag, in SOURCE_DOMAINS order."""
    return [d for d, tags in SOURCE_DOMAINS.items() if tag in tags]
```
Decision on `Source`: **helper, not a model**. No `Source` table exists in `db.py`, Oct 18's `search_regulation(tag, query)` only needs "which domains for this tag", and a model would duplicate `SOURCE_DOMAINS`.

`regwatch/schemas.py` — add after `Profile` (import `SOURCE_DOMAINS` from `regwatch.config`; config does not import schemas, so no cycle; also `from datetime import datetime` and `field_validator`):
```python
class Document(BaseModel):
    """One extracted page. content_hash = ingest.diff.content_hash(markdown); computed by the caller."""
    url: str
    content_hash: str
    published_date: str | None = None   # as Tavily returns it; str mirrors DocumentRow
    fetched_at: datetime
    markdown: str
    source_domain: str

    @field_validator("source_domain")
    @classmethod
    def _allow_listed(cls, v: str) -> str:
        if v not in SOURCE_DOMAINS:
            raise ValueError(f"{v!r} is not in config.SOURCE_DOMAINS")
        return v
```
`regwatch/db.py` — add to `DocumentRow`:
```python
def to_schema(self) -> Document:
    return Document(url=self.url, content_hash=self.content_hash, published_date=self.published_date,
                    fetched_at=self.fetched_at, markdown=self.markdown, source_domain=self.source_domain)
```
`regwatch/app.py` — `profile_form` context gains `"platforms": PLATFORMS` (import from config). `profile_save` signature unchanged.

`profile_form.html` — replace the hardcoded platform select with:
```html
<select id="platform" name="platform">{% for key,name in platforms %}<option value="{{key}}" {% if profile and profile.platform==key %}selected{% endif %}>{{name}}</option>{% endfor %}</select>
```

## 3. Tests

Add to `tests/test_app.py` a second fixture `empty_client` (same env-var + reload pattern, **no seed**). Load the demo profile via `json.loads(Path("examples/seed-run/profile.json").read_text())` (resolve relative to `tests/__file__`).

| File | Test | Asserts |
|---|---|---|
| test_app.py | `test_post_profile_stores_demo_profile` | POST form of profile.json → 303 to `/feed`; `current_profile(...).to_schema() == Profile(**demo)` |
| test_app.py | `test_get_profile_renders_saved_profile_back` | after POST, GET /profile: `<option value="DE" selected>`, each ship_to value `selected`, each category `selected`, `value="woocommerce" selected` when posted with woocommerce, and `value="shopify" selected` NOT present |
| test_app.py | `test_all_eu_expands_to_every_eu_country` | POST `ship_to=["ALL_EU"]` → stored ship_to `== sorted(EU_COUNTRIES)` and `"ALL_EU"` not in it |
| test_app.py | `test_profile_normalises_country_case_and_dedupes_lists` | POST `establishment_country="de"`, `ship_to=["FR","AT","FR"]`, `categories=["leather","leather"]` → `"DE"`, `["AT","FR"]`, `["leather"]` |
| test_app.py | `test_profile_form_renders_when_empty` | GET /profile on empty DB → 200, no `selected` attribute in the response |
| test_config.py | `test_every_source_tag_is_a_seed_regulation` | `set().union(*SOURCE_DOMAINS.values()) <= REGULATION_TAGS` |
| test_config.py | `test_seed_regulations_have_iso_dates_and_unique_tags` | `date.fromisoformat(r["applies_from"])` for all; tags unique; each has `instrument`, `query` non-empty |
| test_config.py | `test_sources_for_each_tag_nonempty_and_allow_listed` | for tag in REGULATION_TAGS: `sources_for(tag)` non-empty, `⊂ SOURCE_DOMAINS`; `sources_for("nope") == []`; `"eur-lex.europa.eu" in sources_for("withdrawal")` |
| test_config.py | `test_category_and_platform_keys_unique` | keys of CATEGORIES / PLATFORMS unique; `"shopify"` in PLATFORMS keys; EU_COUNTRIES 27 unique 2-letter upper |
| test_seed_run.py | `test_demo_profile_matches_config` | `Profile.model_validate_json(...)`; `establishment_country in EU_COUNTRIES`; `set(ship_to) <= set(EU_COUNTRIES)`; `set(categories) <= {k for k,_ in CATEGORIES}`; `platform in {k for k,_ in PLATFORMS}` |
| test_seed_run.py | `test_seed_changes_are_grounded` (parametrised over `change-*.json`) | `Applicability.ungrounded_todos(change) == []`; every `obligation.quote.strip()`; every todo `citation_url == change.document_url`; `urlparse(document_url).hostname in SOURCE_DOMAINS`; every todo has `due` |
| test_schemas.py | `test_document_rejects_domain_outside_allow_list` | `Document(..., source_domain="example.com")` raises `ValidationError`; `"eur-lex.europa.eu"` accepted |
| test_schemas.py | `test_document_row_round_trip` | `DocumentRow(...).to_schema()` equals the `Document` built from the same values |

**Theater check** (the verifier runs this; you just need to know which tests are load-bearing): `test_get_profile_renders_saved_profile_back` (the `woocommerce selected` assertion) ↔ platform loop in the template; `test_all_eu_expands_to_every_eu_country` ↔ `if "ALL_EU"` block; `test_profile_normalises_country_case_and_dedupes_lists` ↔ `.upper()` / `sorted(set())`; `test_document_rejects_domain_outside_allow_list` ↔ the validator; `test_every_source_tag_is_a_seed_regulation` ↔ SOURCE_DOMAINS tags.

## 4. Done-when

```
uv sync --dev && uv run python --version && uv run ruff check . && uv run ruff format --check . && uv run mypy regwatch && uv run pytest -q
```
Expected: `Python 3.12.x` · `All checks passed!` · `N files already formatted` · `Success: no issues found in 15 source files` · `19 passed` (6 existing + 13 new; parametrised seed test counts as 2 → 19 or 20; report the actual number).

## 5. Risks / gotchas

- **3.12 switch**: write `.python-version` = `3.12` (3.12.13 is already installed). The existing `.venv` is 3.13 — `uv sync --dev` recreates it; commit `.python-version`, not `.venv`. `uv.lock` stays byte-identical; if `uv sync` rewrites it anyway, stop and report rather than commit a lock diff.
- **Env precedence**: tests set `REGWATCH_DB_URL` via monkeypatch and `importlib.reload(regwatch.app)`; env vars beat `.env` in pydantic-settings. Keep the reload pattern in the new fixture; do not import `regwatch.app` at test-module top level.
- **Multi-value form fields**: `TestClient.post("/profile", data={"ship_to": ["AT","FR"], ...})` sends repeated keys — required for `list[str] = Form(default=[])`. Do not JSON-encode.
- **Profile equality**: `ProfileRow.to_schema()` decodes JSON lists; compare to `Profile(**demo)` — profile.json's ship_to is already sorted.
- **mypy --strict**: `field_validator` needs `@classmethod` under it; `sources_for` return type explicit; in tests annotate fixtures `-> TestClient` and helper returns; `json.loads` returns `Any` — assign to `dict[str, Any]`. Don't add `# type: ignore` to new code.
- **Import cycle guard**: `schemas.py` may import `regwatch.config`. Never make `config.py` import `schemas.py`.
- **Grounding invariants untouched**: `validate_quotes` / `ungrounded_todos` bodies must not change; `seed()` in `crawl.py` unchanged.
- `ruff` E501 is ignored for `config.py`; the `PLATFORMS` one-liner is fine. Template `{% if profile and profile.platform==key %}` — `profile` is a `Profile` pydantic object.

## 6. Commits (in order)

1. `feat(oct17): pin Python 3.12 via .python-version` — `.python-version` only.
2. `feat(oct17): render saved platform back; PLATFORMS + sources_for in config` — config.py, app.py, profile_form.html.
3. `feat(oct17): add Document schema and DocumentRow.to_schema for Oct 18 ingest` — schemas.py, db.py.
4. `test(oct17): cover POST/GET /profile round-trip, config invariants, seed-run grounding` — tests/*.

Each commit must pass the §4 gate on its own.
