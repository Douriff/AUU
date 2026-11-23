"""Helpers for tests that need a ready user store before public HTTP signup."""
from __future__ import annotations

from app.auth.accounts import bootstrap_admin


def seed_admin(name: str = "owner", password: str = "password1", **kwargs):
    """Create the first admin through the controlled bootstrap path, not public HTTP."""
    return bootstrap_admin(name, password, **kwargs)
