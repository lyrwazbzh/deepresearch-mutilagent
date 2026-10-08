"""Bootstrap a disposable identity for real Phase 11 browser acceptance."""

from __future__ import annotations

import argparse

from src.api_view.auth import AuthService
from src.agent.container import build_container


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--tenant-name", default="Phase 11 Browser")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    container = build_container()
    AuthService(container.session_factory, container.settings).create_identity(
        tenant_id=args.tenant_id,
        tenant_name=args.tenant_name,
        user_id=args.user_id,
        email=args.email,
        password=args.password,
        role="admin",
    )
    print(args.user_id)


if __name__ == "__main__":
    main()
