"""UUID7 id generator. Single helper used by every insert site."""

from __future__ import annotations

import uuid_utils


def new_id() -> str:
    return str(uuid_utils.uuid7())
