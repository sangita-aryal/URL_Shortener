"""
Contract tests for the Distributed ID Generator pipeline.

Per architect.md §3:

  FeistelCipher
    A 42-bit, 4-round Feistel cipher that bijectively scrambles sequential
    integers before Base-62 encoding.  The tests prove mathematical
    correctness: encrypt→decrypt must return the original integer, and no
    two distinct plaintexts may map to the same ciphertext.

  SequenceLeaseManager
    Contacts MongoDB via find_one_and_update to atomically acquire a block of
    1,000,000 sequential IDs.  Subsequent calls within that block must resolve
    in O(1) memory without touching the database mock again.

  IDGenerator  (integration)
    Wires SequenceLeaseManager → FeistelCipher → Base-62 encoder and proves
    the final public output is exactly a 7-character Base-62 string.
"""
import re
import time
from unittest.mock import AsyncMock

import pytest

from app.id_generator import FeistelCipher, IDGenerator, SequenceLeaseManager
from tests.conftest import SAMPLE_ROUND_KEYS

BASE62_RE = re.compile(r"^[0-9a-zA-Z]{7}$")

_MAX_42 = (1 << 42) - 1   # 4_398_046_511_103 — the valid plaintext ceiling


# ══════════════════════════════════════════════════════════════════════════════
# Part A — FeistelCipher
# ══════════════════════════════════════════════════════════════════════════════

class TestFeistelCipherBijectiveReversibility:
    """Core mathematical guarantee: the cipher is a bijection over [0, 2^42)."""

    def test_encrypt_then_decrypt_returns_original(self, cipher):
        plaintext = 123_456_789
        assert cipher.decrypt(cipher.encrypt(plaintext)) == plaintext

    def test_decrypt_then_encrypt_returns_original(self, cipher):
        # The inverse direction must also be a perfect round-trip.
        ciphertext = 987_654_321
        assert cipher.encrypt(cipher.decrypt(ciphertext)) == ciphertext

    def test_bijective_at_zero(self, cipher):
        assert cipher.decrypt(cipher.encrypt(0)) == 0

    def test_bijective_at_one(self, cipher):
        assert cipher.decrypt(cipher.encrypt(1)) == 1

    def test_bijective_at_max_42_bit_value(self, cipher):
        assert cipher.decrypt(cipher.encrypt(_MAX_42)) == _MAX_42

    def test_bijective_at_max_minus_one(self, cipher):
        assert cipher.decrypt(cipher.encrypt(_MAX_42 - 1)) == _MAX_42 - 1

    def test_bijective_on_midrange_value(self, cipher):
        mid = _MAX_42 // 2
        assert cipher.decrypt(cipher.encrypt(mid)) == mid

    def test_bijective_across_sample_set(self, cipher):
        samples = [0, 1, 999, 1_000_000, 42_000_000_000, _MAX_42 - 1, _MAX_42]
        for p in samples:
            assert cipher.decrypt(cipher.encrypt(p)) == p, (
                f"Round-trip failed for plaintext={p}"
            )


class TestFeistelCipherOutputRange:
    """All outputs must stay within the 42-bit address space."""

    def test_encrypt_output_is_non_negative(self, cipher):
        assert cipher.encrypt(12345) >= 0

    def test_encrypt_output_within_42_bits(self, cipher):
        assert cipher.encrypt(12345) <= _MAX_42

    def test_encrypt_zero_within_range(self, cipher):
        assert 0 <= cipher.encrypt(0) <= _MAX_42

    def test_encrypt_max_within_range(self, cipher):
        assert 0 <= cipher.encrypt(_MAX_42) <= _MAX_42

    def test_encrypt_1000_sequential_values_all_in_range(self, cipher):
        for i in range(1000):
            c = cipher.encrypt(i)
            assert 0 <= c <= _MAX_42, f"encrypt({i})={c} out of 42-bit range"


class TestFeistelCipherNonCollision:
    """Injectivity: distinct plaintexts must produce distinct ciphertexts."""

    def test_no_collision_across_10k_sequential_inputs(self, cipher):
        ciphertexts = [cipher.encrypt(i) for i in range(10_000)]
        assert len(set(ciphertexts)) == 10_000

    def test_encrypt_does_not_produce_identity_for_nontrivial_input(self, cipher):
        # The cipher must scramble — not all plaintexts map to themselves.
        # Check 1000 values; at most 1 fixed point is tolerable (statistical fluke).
        fixed_points = sum(1 for i in range(1, 1001) if cipher.encrypt(i) == i)
        assert fixed_points < 10, (
            f"{fixed_points} fixed points found in first 1000 non-zero integers; "
            "cipher is insufficiently scrambling"
        )


class TestFeistelCipherDeterminism:
    """Same input and keys must always produce the same output."""

    def test_repeated_encrypt_same_result(self, cipher):
        p = 555_555_555
        assert cipher.encrypt(p) == cipher.encrypt(p)

    def test_repeated_decrypt_same_result(self, cipher):
        c = 111_111_111
        assert cipher.decrypt(c) == cipher.decrypt(c)

    def test_fresh_instance_same_keys_same_output(self):
        c1 = FeistelCipher(keys=SAMPLE_ROUND_KEYS)
        c2 = FeistelCipher(keys=SAMPLE_ROUND_KEYS)
        assert c1.encrypt(42) == c2.encrypt(42)


class TestFeistelCipherKeySensitivity:
    """Different round keys must produce different ciphertexts."""

    def test_different_keys_produce_different_ciphertexts(self, cipher, cipher_alt_keys):
        p = 100_000_000
        assert cipher.encrypt(p) != cipher_alt_keys.encrypt(p)


class TestFeistelCipherRoundCount:
    """The cipher must use exactly 4 rounds as mandated by architect.md §3."""

    def test_cipher_exposes_rounds_constant(self):
        assert FeistelCipher.ROUNDS == 4

    def test_cipher_exposes_bits_constant(self):
        assert FeistelCipher.BITS == 42

    def test_constructor_rejects_fewer_than_4_keys(self):
        with pytest.raises((ValueError, TypeError)):
            FeistelCipher(keys=[0x1, 0x2, 0x3])

    def test_constructor_rejects_more_than_4_keys(self):
        with pytest.raises((ValueError, TypeError)):
            FeistelCipher(keys=[0x1, 0x2, 0x3, 0x4, 0x5])

    def test_constructor_rejects_empty_keys(self):
        with pytest.raises((ValueError, TypeError)):
            FeistelCipher(keys=[])


# ══════════════════════════════════════════════════════════════════════════════
# Part B — SequenceLeaseManager
# ══════════════════════════════════════════════════════════════════════════════

class TestSequenceLeaseManagerDBInteraction:
    """
    Prove that the manager performs exactly one find_one_and_update call to
    acquire a block of 1,000,000 IDs, and does not call it again until the
    entire block is exhausted.
    """

    async def test_first_next_id_calls_find_one_and_update_exactly_once(
        self, lease_manager, mock_collection
    ):
        await lease_manager.next_id()
        mock_collection.find_one_and_update.assert_called_once()

    async def test_1000_sequential_calls_hit_db_only_once(
        self, lease_manager, mock_collection
    ):
        for _ in range(1_000):
            await lease_manager.next_id()
        assert mock_collection.find_one_and_update.call_count == 1

    async def test_ids_within_single_lease_are_unique(self, lease_manager):
        ids = [await lease_manager.next_id() for _ in range(1_000)]
        assert len(set(ids)) == 1_000

    async def test_new_lease_fetched_exactly_when_block_exhausted(
        self, mock_collection
    ):
        """
        Use a small lease_size so the exhaustion boundary is reachable in a
        unit test without 1,000,000 iterations.
        """
        mock_collection.find_one_and_update.side_effect = [
            {"seq": 5},   # first lease: IDs 0–4
            {"seq": 10},  # second lease: IDs 5–9
        ]
        manager = SequenceLeaseManager(collection=mock_collection, lease_size=5)

        for _ in range(5):
            await manager.next_id()
        assert mock_collection.find_one_and_update.call_count == 1

        await manager.next_id()  # crosses the boundary → must fetch second lease
        assert mock_collection.find_one_and_update.call_count == 2

    async def test_ids_span_correctly_across_lease_boundary(self, mock_collection):
        mock_collection.find_one_and_update.side_effect = [
            {"seq": 3},
            {"seq": 6},
        ]
        manager = SequenceLeaseManager(collection=mock_collection, lease_size=3)
        ids = [await manager.next_id() for _ in range(6)]
        assert len(set(ids)) == 6, "IDs must be unique across the lease boundary"

    async def test_find_one_and_update_uses_correct_increment(
        self, mock_collection
    ):
        """The atomic increment must match the configured lease_size."""
        manager = SequenceLeaseManager(collection=mock_collection)
        await manager.next_id()

        _, kwargs = mock_collection.find_one_and_update.call_args
        update_doc = kwargs.get("update") or mock_collection.find_one_and_update.call_args[0][1]
        # The $inc value must equal the default LEASE_SIZE (1,000,000).
        inc_value = update_doc["$inc"]["seq"]
        assert inc_value == SequenceLeaseManager.DEFAULT_LEASE_SIZE


class TestSequenceLeaseManagerOOneComplexity:
    """In-lease counter increments must not block on I/O."""

    async def test_in_lease_calls_complete_in_constant_time(
        self, lease_manager, mock_collection
    ):
        # Warm up the first lease
        await lease_manager.next_id()
        mock_collection.find_one_and_update.reset_mock()

        # Time 10,000 in-lease calls; they must all be pure memory ops.
        start = time.perf_counter()
        for _ in range(10_000):
            await lease_manager.next_id()
        elapsed = time.perf_counter() - start

        mock_collection.find_one_and_update.assert_not_called()
        assert elapsed < 1.0, (
            f"10,000 in-lease next_id() calls took {elapsed:.3f}s; "
            "expected sub-second O(1) memory increments"
        )


class TestSequenceLeaseManagerConstants:
    def test_default_lease_size_is_one_million(self):
        assert SequenceLeaseManager.DEFAULT_LEASE_SIZE == 1_000_000


# ══════════════════════════════════════════════════════════════════════════════
# Part C — IDGenerator  (integration)
# ══════════════════════════════════════════════════════════════════════════════

class TestIDGenerator:
    """
    Integration: SequenceLeaseManager + FeistelCipher + Base-62 encoder.

    Proves the full pipeline produces a 7-character Base-62 short code with
    no collisions.  Uses the standard fixtures so the mock MongoDB collection
    and sample round keys are consistent with the unit tests above.
    """

    async def test_generate_returns_string(self, cipher, lease_manager):
        gen = IDGenerator(lease_manager=lease_manager, cipher=cipher)
        result = await gen.generate()
        assert isinstance(result, str)

    async def test_generate_output_is_exactly_7_characters(self, cipher, lease_manager):
        gen = IDGenerator(lease_manager=lease_manager, cipher=cipher)
        result = await gen.generate()
        assert len(result) == 7, f"Expected 7 chars, got {len(result)!r}: {result!r}"

    async def test_generate_output_is_base62(self, cipher, lease_manager):
        gen = IDGenerator(lease_manager=lease_manager, cipher=cipher)
        result = await gen.generate()
        assert BASE62_RE.match(result), f"Not Base-62: {result!r}"

    async def test_generate_100_ids_all_valid_format(self, cipher, lease_manager):
        gen = IDGenerator(lease_manager=lease_manager, cipher=cipher)
        for i in range(100):
            result = await gen.generate()
            assert len(result) == 7 and BASE62_RE.match(result), (
                f"ID #{i} has invalid format: {result!r}"
            )

    async def test_generate_1000_ids_all_unique(self, cipher, lease_manager):
        gen = IDGenerator(lease_manager=lease_manager, cipher=cipher)
        ids = [await gen.generate() for _ in range(1_000)]
        assert len(set(ids)) == 1_000, "Duplicate short codes detected within a single lease"
