"""Pure guardrail predicates: tool allow-list, sandbox path, turn cap, spend cap.

All four functions are total (no exceptions) and side-effect free. Callers
treat a True from the cap checks as "limit reached -> pause".
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


def is_tool_allowed(tool_name: str, allow_list: Iterable[str]) -> bool:
    """Case-sensitive membership check. Empty allow-list denies everything."""
    return tool_name in set(allow_list)


def is_path_safe(candidate: str | Path, sandbox_root: str | Path) -> bool:
    """True iff candidate resolves to sandbox_root or any descendant.

    Resolves both sides to absolute paths (following symlinks where they
    exist) and compares via Path.is_relative_to so any escape via ``..`` or
    symlink-out-of-tree is rejected. strict=False is used so non-existent
    paths (the common write_file case for new files) still resolve.
    """
    try:
        resolved_candidate = Path(candidate).resolve(strict=False)
        resolved_root = Path(sandbox_root).resolve(strict=False)
    except (OSError, ValueError):
        return False
    return resolved_candidate == resolved_root or resolved_candidate.is_relative_to(
        resolved_root
    )


def check_turn_cap(iter_since_approval: int, max_iters: int = 10) -> bool:
    """True iff iter_since_approval is at or above the cap. Negative -> False."""
    if iter_since_approval < 0:
        return False
    return iter_since_approval >= max_iters


def check_spend_cap_today(spent_usd: float, cap_usd: float = 1.0) -> bool:
    """True iff today's spend is at or above the cap. Negative spent -> False."""
    if spent_usd < 0:
        return False
    return spent_usd >= cap_usd
