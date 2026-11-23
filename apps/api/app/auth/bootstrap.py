"""Controlled first-admin initialization for the existing user store.

This is an offline/ops entry point, not an HTTP route. Public
``POST /api/v1/auth/register`` never grants admin and cannot claim
``AUU_ADMIN_USER``. Existing users and roles are left unchanged.
A missing store may be created; a corrupt store fails closed and is
not overwritten.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from app.auth.accounts import auth_enabled, bootstrap_admin, reserved_admin_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the first admin in the existing AUU user store.")
    parser.add_argument("--name", default="", help="Admin username. Defaults to AUU_ADMIN_USER.")
    parser.add_argument(
        "--password-env",
        default="AUU_BOOTSTRAP_PASSWORD",
        help="Environment variable holding the password (default AUU_BOOTSTRAP_PASSWORD). Avoid passing the password on the command line.",
    )
    args = parser.parse_args(argv)
    if not auth_enabled():
        print("AUU_AUTH is off; bootstrap refused.", file=sys.stderr)
        return 2
    name = (args.name or reserved_admin_name()).strip()
    if not name:
        print("Provide --name or set AUU_ADMIN_USER.", file=sys.stderr)
        return 2
    password = os.getenv(args.password_env, "").strip()
    if not password:
        password = getpass.getpass("bootstrap admin password: ")
    try:
        user = bootstrap_admin(name, password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"admin ready: {user['name']} id={user['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
