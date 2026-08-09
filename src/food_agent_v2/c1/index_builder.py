"""Qdrant index construction boundary.

The pre-T09 command wrote directly to the configured online collection and could
publish an unverified or partial index.  Fixed data is now indexed only inside the
H04-gated MySQL/Qdrant initialization transaction.
"""

from __future__ import annotations


def build_index(*_args: object, **_kwargs: object) -> dict:
    """Block the legacy direct-to-online index path.

    Use ``food-agent-v2 data-initialize --manifest ... --confirm-empty-v2`` so the
    verified RAG artifact is built in an isolated physical collection, checked for
    exact recipe-ID parity, and only then published under the configured alias.
    """
    return {
        "status": "blocked",
        "reason": "DIRECT_QDRANT_BUILD_REMOVED",
        "required_command": (
            "food-agent-v2 data-initialize --manifest <BuildManifest> --confirm-empty-v2"
        ),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(build_index(), ensure_ascii=False, indent=2))
