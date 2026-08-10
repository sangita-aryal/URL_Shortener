# Quality Gates Report

**Project:** URL Shortener  
**Run Date:** 2026-08-09  
**Branch:** feature/final_hardening  
**Python:** 3.13.0  
**Tools:** ruff 0.x · bandit 1.9.4 · pytest-cov 7.1.0

---

## Summary

| Gate | Tool | Status | Result |
|---|---|---|---|
| Linting | ruff | **PASS** | 0 issues |
| Security scan | bandit | **PASS** | 1 Low / 0 Medium / 0 High |
| Test suite | pytest | **PASS** | 190 / 190 passed (1.41 s) |
| Test coverage | pytest-cov | **PASS** | 92% overall |

---

## 1. Linting — ruff

**Command:** `ruff check app/ tests/`  
**Result:** PASS — `All checks passed!`

Ruff is configured in [`ruff.toml`](../ruff.toml) with the following rule sets:

| Rule set | Code | Purpose |
|---|---|---|
| pycodestyle | `E`, `W` | Style errors and warnings |
| pyflakes | `F` | Unused imports, undefined names |
| pyupgrade | `UP` | Deprecated stdlib usage (e.g. `typing` → `collections.abc`) |
| isort | `I` | Import ordering |
| flake8-blind-except | `BLE` | Overly broad exception catches |
| flake8-bandit | `S` | Security anti-patterns |
| refurb | `FURB` | Modern Python idioms |
| flake8-async | `ASYNC` | Blocking calls in async context |
| flake8-return | `RET` | Redundant return statements |
| pylint-refactor | `PLR` | Refactor suggestions |

### Per-file ignores (`tests/`)

| Rule | Reason |
|---|---|
| `S101` | `assert` is the correct idiom in pytest — pytest rewrites assert statements for rich failure messages |
| `PLR2004` | Numeric literals in test comparisons are intentional and readable |
| `ASYNC230` | Sync file reads in test assertions are intentional: the async write has already completed before `open()` runs; no concurrent coroutines exist in a single-test event loop |

### Suppressed inline (`app/telemetry.py:37`)

```python
except Exception:  # noqa: BLE001, S110 — telemetry loss is preferable to a failed redirect
    pass
```

`BLE001` (blind exception catch) and `S110` (try/except/pass) are suppressed here by design. Swallowing I/O errors on the telemetry write path is the documented fire-and-forget contract: a redirect must succeed even when the analytics log write fails.

---

## 2. Security Scan — bandit

**Command:** `bandit -r app/ -f txt`  
**Result:** PASS — 1 Low severity / 0 Medium / 0 High

### Finding

#### `B110:try_except_pass` — Low severity, High confidence
**File:** `app/telemetry.py:37`  
**CWE:** CWE-703 (Improper Check or Handling of Exceptional Conditions)

```python
try:
    await asyncio.to_thread(self._append, entry)
except Exception:  # noqa: BLE001, S110
    pass
```

**Assessment: Accepted risk.** Bandit flags the `try/except/pass` pattern as potentially masking errors. This is intentional — the telemetry pipeline is fire-and-forget by contract. A failed disk write (full disk, missing directory, permission error) must never propagate to the route handler and cause a redirect failure. Telemetry loss is the documented trade-off.

**Suppression path:** Add `# nosec B110` inline to silence bandit alongside the existing ruff `# noqa`.

### Scan metrics

```
Total lines of code scanned : 526
Total lines skipped          : 0

Total issues (by severity):
  High   : 0
  Medium : 0
  Low    : 1

Files skipped: 0
```

No injection vulnerabilities, hardcoded secrets, weak cryptography, insecure deserialization, or unsafe subprocess usage found across 526 lines.

---

## 3. Test Suite — pytest

**Command:** `pytest tests/ --cov=app --cov-report=term-missing`  
**Result:** PASS — 190 / 190 passed in 1.41 s

### Results by module

| Test file | Tests | Result |
|---|---|---|
| `tests/test_app.py` | 18 | PASS |
| `tests/test_id_generator.py` | 37 | PASS |
| `tests/test_ssrf_validator.py` | 53 | PASS |
| `tests/test_stats_analytics.py` | 26 | PASS |
| `tests/test_telemetry.py` | 35 | PASS |
| `tests/test_url_repository.py` | 21 | PASS |
| **Total** | **190** | **PASS** |

---

## 4. Test Coverage — pytest-cov

**Result:** 92% overall (306 statements, 26 missed)

### Coverage by module

| Module | Stmts | Miss | Cover | Missing lines |
|---|---|---|---|---|
| `app/__init__.py` | 0 | 0 | **100%** | — |
| `app/analytics.py` | 53 | 2 | **96%** | 50–51 |
| `app/app.py` | 99 | 20 | **80%** | 57–79, 89, 93, 97, 106, 110, 114, 118, 163 |
| `app/id_generator.py` | 73 | 2 | **97%** | 27, 32 |
| `app/ssrf_validator.py` | 50 | 2 | **96%** | 45–46 |
| `app/telemetry.py` | 15 | 0 | **100%** | — |
| `app/url_repository.py` | 16 | 0 | **100%** | — |
| **TOTAL** | **306** | **26** | **92%** | |

### Coverage improvement

The addition of `tests/test_stats_analytics.py` (26 tests) brought `app/app.py` from **0% → 66%** by exercising the two new endpoints via `httpx.AsyncClient` with a null lifespan fixture. The subsequent addition of `tests/test_app.py` (18 integration tests for `POST /shorten` and `GET /{short_code}`) pushed overall coverage from **87% → 92%** and `app/app.py` from **66% → 80%**.

### Coverage gap detail

#### `app/app.py` — 80%

Missing lines are the lifespan startup/shutdown block (lines 57–79) and the DI provider functions for MongoDB, Redis, and the resolver (lines 89–118, 163). These require live MongoDB/Redis connections to exercise. All route handlers (`POST /shorten`, `GET /{short_code}`, `GET /stats/{code}`, `GET /analytics`) are now covered.

**Path to remaining coverage:** Spin up a real MongoDB/Redis in CI (e.g. via `docker compose` service fixtures) to exercise the lifespan block and DI providers.

#### `app/analytics.py` — 96% (lines 50–51)

The `except (KeyError, ValueError): continue` branch inside `entries_for_date()` is not exercised. No test currently passes an entry with a valid-JSON body but a missing or unparseable `ts` field.

**Fix:** Add a test case with `{"code": "x", "ip": "1.2.3.4"}` (no `ts` key) and verify it is skipped.

#### `app/id_generator.py` — 97% (lines 27, 32)

The two `TypeError` raise paths in `FeistelCipher.__init__` are not exercised:
- Line 27: `keys` is not a `list`
- Line 32: an element of `keys` is not an `int`

**Fix:** Add `FeistelCipher(keys="not-a-list")` and `FeistelCipher(keys=[1, 2, 3, "x"])` to `TestFeistelCipherRoundCount`.

#### `app/ssrf_validator.py` — 96% (lines 45–46)

The `except ValueError: return False` branch in `_is_blocked_address()` is not exercised. This handles the edge case where a zone-ID-stripped string is still not parseable as an IP address.

**Fix:** Add a test for `_is_blocked_address("not-an-ip%zone")` returning `False`.

---

## How to reproduce

```bash
# Linting
ruff check app/ tests/

# Security scan
bandit -r app/ -f txt

# Test coverage
pytest tests/ --cov=app --cov-report=term-missing
```

All tools are included in `requirements.txt` — no separate install step needed after `pip install -r requirements.txt`.

---

## Recommended next actions

| # | Action | Effort | Impact |
|---|---|---|---|
| 1 | Add `# nosec B110` to `app/telemetry.py:37` | 1 min | Silences bandit on the accepted-risk finding |
| 2 | Add missing `TypeError` constructor tests to `TestFeistelCipherRoundCount` | 15 min | `app/id_generator.py` 97% → 100% |
| 3 | Add malformed-`ts` test to `TestDateIsolation` | 10 min | `app/analytics.py` 95% → 100% |
| 4 | Add `_is_blocked_address` edge-case test | 10 min | `app/ssrf_validator.py` 96% → 100% |
| 5 | Add live-stack CI fixtures (MongoDB + Redis via Compose) | 2–4 hrs | Covers lifespan block and DI providers; `app/app.py` 80% → 95%+ |
