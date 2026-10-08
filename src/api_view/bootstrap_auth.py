from __future__ import annotations

import argparse
import getpass
import os

from src.api_view.auth import AuthService
from src.agent.config import Settings
from src.agent.backends.persistence.database import create_session_factory


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(
        description="Create the initial Phase 10 tenant user without public registration."
    )
    parser.add_argument("--tenant-id", default=settings.default_tenant_id)
    parser.add_argument("--tenant-name", default="DeepResearch")
    parser.add_argument("--user-id", default=settings.default_user_id)
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", choices=("member", "admin"), default="admin")
    parser.add_argument(
        "--password-env",
        default="PHASE10_BOOTSTRAP_PASSWORD",
        help="Environment variable containing the initial password.",
    )
    args = parser.parse_args()
    password = os.getenv(args.password_env) or getpass.getpass("Initial password: ")
    if len(password) < 12:
        raise SystemExit("The initial password must contain at least 12 characters.")
    AuthService(
        create_session_factory(settings.database_url), settings
    ).create_identity(
        tenant_id=args.tenant_id,
        tenant_name=args.tenant_name,
        user_id=args.user_id,
        email=args.email,
        password=password,
        role=args.role,
    )
    print(
        f"Created or verified {args.role} identity {args.user_id} "
        f"in tenant {args.tenant_id}."
    )


if __name__ == "__main__":
    main()
