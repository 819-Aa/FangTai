"""Retired mutable seed entry points.

V2 runtime projections and Qdrant points must come from one verified BuildManifest via
``data-initialize``. Generating either store independently would break build identity and
payload parity, so the former cleaned-directory seed path is deliberately unavailable.
"""

from __future__ import annotations


class DirectSeedGenerationRemoved(RuntimeError):
    """The legacy independent MySQL/Qdrant seed path is no longer authoritative."""


_MESSAGE = (
    "DIRECT_SEED_GENERATION_REMOVED: use data-initialize --manifest <BuildManifest> "
    "--confirm-empty-v2"
)


def generate_mysql_seed() -> str:
    raise DirectSeedGenerationRemoved(_MESSAGE)


def generate_qdrant_payloads() -> str:
    raise DirectSeedGenerationRemoved(_MESSAGE)
