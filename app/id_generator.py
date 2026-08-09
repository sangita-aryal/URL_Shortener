"""
Distributed ID generation pipeline.

  FeistelCipher     — bijective 42-bit scrambler (4 rounds)
  SequenceLeaseManager — atomic MongoDB segment lease; O(1) in-memory counter
  IDGenerator       — integrates both into a 7-char Base-62 short code
"""
import asyncio


class FeistelCipher:
    """
    42-bit, 4-round balanced Feistel network.

    Splits a 42-bit integer into two 21-bit halves and runs 4 Feistel rounds.
    The structure guarantees bijectivity for any round function, so
    decrypt(encrypt(x)) == x for every x in [0, 2^42).
    """

    BITS: int = 42
    HALF_BITS: int = 21
    ROUNDS: int = 4
    HALF_MASK: int = (1 << 21) - 1

    def __init__(self, keys: list) -> None:
        if not isinstance(keys, list):
            raise TypeError("keys must be a list of exactly 4 integers")
        if len(keys) != self.ROUNDS:
            raise ValueError(f"keys must contain exactly {self.ROUNDS} integers, got {len(keys)}")
        for k in keys:
            if not isinstance(k, int):
                raise TypeError(f"each round key must be an int, got {type(k).__name__}")
        self._keys = list(keys)

    # ── round function ───────────────────────────────────────────────────────

    def _f(self, half: int, key: int) -> int:
        """
        Pseudo-random round function mapping 21-bit × key → 21-bit.
        Correctness of bijectivity is independent of this function's quality;
        the Feistel structure guarantees it.  This mixing provides good
        dispersion to prevent sequential short-code crawling.
        """
        v = (half ^ key) * 0x9E3779B9 & 0xFFFF_FFFF
        v ^= v >> 16
        v *= 0x85EBCA6B
        v &= 0xFFFF_FFFF
        v ^= v >> 13
        return v & self.HALF_MASK

    # ── public API ───────────────────────────────────────────────────────────

    def encrypt(self, plaintext: int) -> int:
        """Scramble a 42-bit integer.  Result is in [0, 2^42)."""
        left = (plaintext >> self.HALF_BITS) & self.HALF_MASK
        right = plaintext & self.HALF_MASK
        for key in self._keys:
            left, right = right, left ^ self._f(right, key)
        return (left << self.HALF_BITS) | right

    def decrypt(self, ciphertext: int) -> int:
        """Inverse of encrypt.  decrypt(encrypt(x)) == x for all x in [0, 2^42)."""
        left = (ciphertext >> self.HALF_BITS) & self.HALF_MASK
        right = ciphertext & self.HALF_MASK
        for key in reversed(self._keys):
            left, right = right ^ self._f(left, key), left
        return (left << self.HALF_BITS) | right


class SequenceLeaseManager:
    """
    Atomically leases a contiguous block of sequence IDs from MongoDB.

    On the first call (and each time the local block is exhausted) it
    executes a single find_one_and_update with $inc to advance the
    global counter by lease_size.  All subsequent calls within that
    block are pure in-memory increments — O(1), no I/O.

    Thread-safety is provided by asyncio.Lock; this is safe for use inside
    any single asyncio event loop (one per Uvicorn worker process).
    """

    DEFAULT_LEASE_SIZE: int = 1_000_000

    def __init__(self, collection, *, lease_size: int = 1_000_000) -> None:
        self._collection = collection
        self._lease_size = lease_size
        self._current: int = 0
        self._ceiling: int = 0   # exclusive; _current == _ceiling triggers a fetch
        self._lock: asyncio.Lock = asyncio.Lock()

    async def next_id(self) -> int:
        async with self._lock:
            if self._current >= self._ceiling:
                await self._fetch_lease()
            value = self._current
            self._current += 1
            return value

    async def _fetch_lease(self) -> None:
        result = await self._collection.find_one_and_update(
            {"_id": "url_sequence"},
            {"$inc": {"seq": self._lease_size}},
            upsert=True,
            return_document=True,
        )
        seq = result["seq"]
        self._ceiling = seq
        self._current = seq - self._lease_size


class IDGenerator:
    """
    Integrates SequenceLeaseManager and FeistelCipher to produce a
    7-character Base-62 short code for each new URL.

    Pipeline:
        sequential integer  →  Feistel encrypt  →  Base-62 encode (7 chars)

    Cycle-walking ensures the final encoded integer always fits within
    [0, 62^7) even though the Feistel domain is [0, 2^42).
    """

    BASE62_CHARSET: str = (
        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    )
    _OUTPUT_LEN: int = 7
    _MAX_CODE: int = 62 ** 7  # 3_521_614_606_208

    def __init__(self, lease_manager: SequenceLeaseManager, cipher: FeistelCipher) -> None:
        self._lease_manager = lease_manager
        self._cipher = cipher

    async def generate(self) -> str:
        seq_id = await self._lease_manager.next_id()
        scrambled = self._cipher.encrypt(seq_id)
        # Cycle-walk: if the Feistel output exceeds 62^7-1, follow the
        # permutation chain until it lands inside the Base-62 domain.
        # Terminates because the cipher is a bijection of a finite set.
        while scrambled >= self._MAX_CODE:
            scrambled = self._cipher.encrypt(scrambled)
        return self._encode(scrambled)

    def _encode(self, n: int) -> str:
        digits = []
        for _ in range(self._OUTPUT_LEN):
            digits.append(self.BASE62_CHARSET[n % 62])
            n //= 62
        return "".join(reversed(digits))
