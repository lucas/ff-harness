from __future__ import annotations

import uuid

from harness.models.ids import new_id


def test_new_id_is_valid_uuid7_string():
    s = new_id()
    assert isinstance(s, str)
    assert len(s) == 36
    parsed = uuid.UUID(s)
    assert parsed.version == 7


def test_new_id_is_strictly_monotonic_over_100_calls():
    ids = [new_id() for _ in range(100)]
    assert ids == sorted(ids), "UUID7 must be lexicographically monotonic per insertion order"
    assert len(set(ids)) == 100, "all ids must be unique"
