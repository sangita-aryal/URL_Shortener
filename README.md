# URL Shortener

A stateless, distributed URL shortener engineered for high-throughput
redirection at production scale.

| Metric | Target |
|---|---|
| Daily read volume | 1 billion redirects (~11,600 avg QPS) |
| Daily write volume | 100 million creations (~2,320 peak QPS) |
| Key length | 7 characters, Base-62 (3.52 trillion unique keys) |

## System Architecture

```
Internet
   │
   ▼
┌─────────────────────────────────┐
│  Nginx  (port 80/443 — public)  │   ← Only component with a public IP.
│  Rate limiting · SSL termination │     Buffers slow clients; protects
│  Reverse proxy to internal net  │     ASGI workers from I/O starvation.
└──────────────┬──────────────────┘
               │  private network
   ┌───────────┴───────────┐
   ▼                       ▼
FastAPI worker 0      FastAPI worker N     ← Stateless. No local session
   │                       │                 or presentation state.
   ├─── read ──────────────┼──▶  Redis cluster   (O(1) cache hit)
   │                       │
   └─── write/miss ────────┴──▶  MongoDB sharded cluster
```

Nginx is the **only** component exposed to the public internet. All
FastAPI, Redis, and MongoDB communication happens over a private internal
network.

---

## Design Decisions

### 1 — 42-Bit Feistel Cipher (ID Scrambling)

**The problem.** Sequential auto-increment IDs allow a client to enumerate
every shortened URL by incrementing the counter. UUIDs avoid this but are
128 bits — far too long for a 7-character short code. Hash functions are
not bijective: two inputs can collide on the same output, requiring an
expensive database read-before-write check.

**The solution.** Each sequential integer produced by the lease manager is
scrambled by a custom in-memory 42-bit Feistel network before encoding.

A balanced Feistel cipher splits the 42-bit integer into two 21-bit halves
and applies 4 rounds of the following transform:

```
Input: (L, R)  ← two 21-bit halves of the plaintext

For each round i:
    (L, R) = (R,  L XOR f(R, key[i]))

Output: join(L, R)  → 42-bit ciphertext
```

The Feistel structure **mathematically guarantees bijectivity** — every
input maps to a unique output — regardless of the round function `f`. This
eliminates collisions without any database lookup.

**Why 42 bits.** 62⁷ = 3,521,614,606,208 fits between 2⁴¹ and 2⁴², so
a 42-bit cipher domain is the smallest that covers the full 7-character
Base-62 space. Values that land above 62⁷ − 1 are walked through the
permutation chain (cycle-walking) until they fall within the encodable
range — a standard format-preserving encryption technique.

Relevant source: [`app/id_generator.py`](app/id_generator.py) — `FeistelCipher`

---

### 2 — MongoDB Range Pre-Allocation (Segment Leasing)

**The problem.** A central atomic counter contacted on every write is a
single point of network contention. Snowflake-style IDs require
synchronized clocks across workers. UUID4 is unordered and at 128 bits
cannot be encoded in 7 characters.

**The solution.** On startup (and whenever its local block is exhausted),
each FastAPI worker contacts MongoDB once to atomically claim a contiguous
block of one million sequence numbers:

```python
result = await collection.find_one_and_update(
    {"_id": "url_sequence"},
    {"$inc": {"seq": 1_000_000}},
    upsert=True,
    return_document=True,
)
```

The worker stores the range locally (`current`, `ceiling`) and increments
a counter in O(1) memory. **999,999 of every 1,000,000 ID assignments
involve zero network I/O.**

**Durability.** The MongoDB connection uses `w="majority"` and `j=True`
(journaling). No lease is granted until the sequence document is durably
written to a majority of replica nodes, preventing lease rollback on a
primary failover.

Relevant source: [`app/id_generator.py`](app/id_generator.py) — `SequenceLeaseManager`

---

### 3 — Non-Blocking aiodns SSRF Shield

**The problem.** `socket.getaddrinfo` is a blocking system call. When
called from an ASGI event loop thread it stalls **every concurrent
coroutine** for the full round-trip duration — often 50–300 ms under load.
This is event-loop starvation: a standard library call silently serialises
all in-flight requests.

**The solution.** DNS resolution is performed exclusively through
[aiodns](https://github.com/saghul/aiodns), which wraps the c-ares
asynchronous resolver. Resolution is dispatched as a non-blocking I/O
event; the coroutine yields until the result arrives without blocking the
thread.

```python
# ✗  Blocks the event loop
socket.getaddrinfo(host, None)

# ✓  Yields to the event loop; other coroutines continue
await resolver.gethostbyname(host, socket.AF_INET)
```

**SSRF policy enforced on every write request:**

| Check | Blocked ranges |
|---|---|
| Private IPv4 (RFC 1918) | `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` |
| Loopback | `127.0.0.0/8`, `localhost` (case-insensitive) |
| Link-local / cloud metadata | `169.254.0.0/16` (incl. `169.254.169.254`) |
| IPv6 loopback | `::1/128` |
| IPv6 link-local | `fe80::/10` |
| IPv6 unique-local | `fc00::/7` |
| IPv4-mapped IPv6 | `::ffff:192.168.x.x` etc. (embedded IPv4 checked) |
| Schemes | Only `http` and `https` |
| DNS failure | Treated as a block (fail-safe) |

Literal IPs in the URL bypass DNS resolution entirely. Hostnames that
resolve to any blocked range are rejected (DNS rebinding defence).

Relevant source: [`app/ssrf_validator.py`](app/ssrf_validator.py) — `validate_url`

---

## Running the Test Suite

The test suite is written with `pytest` and `pytest-asyncio` following a
strict contract-first (test-first) approach. Tests were written before any
implementation code.

**Install dependencies**

```bash
pip install -r requirements.txt
```

**Run the full suite (93 tests)**

```bash
pytest tests/ -v
```

**Run without throughput benchmarks**

```bash
pytest tests/ -v -m "not slow"
```

**Run a single module**

```bash
pytest tests/test_id_generator.py -v
pytest tests/test_ssrf_validator.py -v
```

### Test coverage

| File | Class | What it enforces |
|---|---|---|
| `test_id_generator.py` | `TestFeistelCipherBijectiveReversibility` | `decrypt(encrypt(x)) == x` for boundary and mid-range values |
| | `TestFeistelCipherOutputRange` | All outputs stay within `[0, 2⁴²)` |
| | `TestFeistelCipherNonCollision` | 10,000 sequential inputs → 10,000 distinct outputs |
| | `TestFeistelCipherDeterminism` | Same keys + same input = same output, always |
| | `TestFeistelCipherKeySensitivity` | Different round keys → different ciphertexts |
| | `TestFeistelCipherRoundCount` | `ROUNDS == 4`, `BITS == 42`; constructor rejects ≠ 4 keys |
| | `TestSequenceLeaseManagerDBInteraction` | `find_one_and_update` called once for 1,000 sequential IDs; exhaustion boundary tested at `lease_size=5` |
| | `TestSequenceLeaseManagerOOneComplexity` | 10,000 in-lease calls in under 1 s with zero DB calls |
| | `TestSequenceLeaseManagerConstants` | `DEFAULT_LEASE_SIZE == 1_000_000` |
| | `TestIDGenerator` | Full pipeline: seq ID → Feistel → 7-char Base-62; 1,000 IDs are unique |
| `test_ssrf_validator.py` | `TestHappyPath` | Valid public URLs pass; literal IPs skip DNS |
| | `TestAiodnsUsage` | `resolver.gethostbyname` is called (never `socket.getaddrinfo`); bare hostname passed; called exactly once |
| | `TestPrivateIPv4` | RFC-1918 ranges blocked; off-by-one guards for `172.16/12` |
| | `TestLoopback` | Full `127.0.0.0/8` range; `LOCALHOST` case-insensitive |
| | `TestLinkLocal` | `169.254.0.0/16` including AWS metadata endpoint |
| | `TestIPv6Private` | Loopback, link-local (with zone ID), unique-local, IPv4-mapped |
| | `TestForbiddenSchemes` | `file`, `ftp`, `javascript`, `data`, `gopher`, `dict`; case-insensitive |
| | `TestMalformedURLs` | Empty string, whitespace, bare hostname, missing host |
| | `TestDNSRebindingDefence` | DNS rebinding (hostname resolves to private IP); `aiodns.error.DNSError` → block |
