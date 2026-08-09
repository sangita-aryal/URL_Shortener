# Quality Gates Report

**Project:** URL Shortener  
**Run Date:** 2026-08-09  
**Branch:** feature/quality_gates  
**Python:** 3.13.0  
**Tools:** ruff 0.x · bandit 1.9.4 · pytest-cov 7.1.0

---

## Summary

| Gate | Tool | Status | Result |
|---|---|---|---|
| Linting | ruff | **PASS** | 0 issues |
| Security scan | bandit | **PASS** | 1 Low / 0 Medium / 0 High |
| Test suite | pytest | **PASS** | 146 / 146 passed (0.54 s) |
| Test coverage | pytest-cov | **PASS** | 70% overall |

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
Total lines of code scanned : 462
Total lines skipped          : 0

Total issues (by severity):
  High   : 0
  Medium : 0
  Low    : 1

Files skipped: 0
```

No injection vulnerabilities, hardcoded secrets, weak cryptography, insecure deserialization, or unsafe subprocess usage found across 462 lines.

---

## 3. Test Suite — pytest

**Command:** `pytest tests/ --cov=app --cov-report=term-missing`  
**Result:** PASS — 146 / 146 passed in 0.54 s

### Results by module

| Test file | Tests | Result |
|---|---|---|
| `tests/test_id_generator.py` | 37 | PASS |
| `tests/test_ssrf_validator.py` | 53 | PASS |
| `tests/test_telemetry.py` | 35 | PASS |
| `tests/test_url_repository.py` | 21 | PASS |
| **Total** | **146** | **PASS** |

---

## 4. Test Coverage — pytest-cov

**Result:** 70% overall (264 statements, 79 missed)

### Coverage by module

| Module | Stmts | Miss | Cover | Missing lines |
|---|---|---|---|---|
| `app/__init__.py` | 0 | 0 | **100%** | — |
| `app/analytics.py` | 37 | 2 | **95%** | 46–47 |
| `app/app.py` | 73 | 73 | **0%** | 13–199 (all) |
| `app/id_generator.py` | 73 | 2 | **97%** | 27, 32 |
| `app/ssrf_validator.py` | 50 | 2 | **96%** | 45–46 |
| `app/telemetry.py` | 15 | 0 | **100%** | — |
| `app/url_repository.py` | 16 | 0 | **100%** | — |
| **TOTAL** | **264** | **79** | **70%** | |

### Coverage gap detail

#### `app/app.py` — 0% (expected)

`app.py` is the FastAPI wiring layer (lifespan, DI providers, route handlers). The test suite uses unit/contract tests with mocked dependencies and does not spin up a live FastAPI instance. The 0% is expected — the correctness of each composed module is verified at 95–100% in isolation.

**Path to coverage:** Add `tests/test_app.py` using FastAPI's `TestClient` or `httpx.AsyncClient` with a mocked lifespan to exercise the HTTP round-trip.

#### `app/analytics.py` — 95% (lines 46–47)

The `except (KeyError, ValueError): continue` branch inside `entries_for_date()` is not exercised. No test currently passes an entry with a valid-JSON body but a missing or unparseable `ts` field to `entries_for_date`.

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
| 5 | Add `tests/test_app.py` integration tests with `TestClient` | 2–4 hrs | `app/app.py` 0% → meaningful coverage; validates full HTTP round-trip |
