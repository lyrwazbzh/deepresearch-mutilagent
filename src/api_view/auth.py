from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, cast

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator
from sqlalchemy import select, update

from src.agent.config import Settings
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    AuthSessionRow,
    TenantRow,
    TenantUserMembershipRow,
    UserRow,
    utcnow,
)
from src.agent.errors import ForbiddenError, InvalidRefreshSessionError, UnauthenticatedError
from src.agent.task_service import new_id


NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    user_id: str
    roles: tuple[str, ...]

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: NonBlank
    password: NonBlank
    tenant_id: NonBlank


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = None
    all_sessions: bool = False

    @model_validator(mode="after")
    def exactly_one_target(self) -> "RevokeRequest":
        if bool(self.session_id) == bool(self.all_sessions):
            raise ValueError("Provide either session_id or all_sessions=true.")
        return self


class AccessResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    session_id: str
    user: dict[str, str]
    tenant: dict[str, str]
    roles: list[str]


@dataclass(frozen=True)
class AuthenticationResult:
    response: AccessResponse
    refresh_credential: str


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class AccessTokenCodec:
    def __init__(self, settings: Settings) -> None:
        if settings.auth_jwt_algorithm != "RS256":
            raise ValueError("Phase 10 currently supports the configured RS256 algorithm only.")
        if settings.auth_jwt_private_key:
            loaded_key = serialization.load_pem_private_key(
                settings.auth_jwt_private_key.encode(), password=None
            )
            if not isinstance(loaded_key, rsa.RSAPrivateKey):
                raise ValueError("AUTH_JWT_PRIVATE_KEY must contain an RSA private key.")
            private_key = loaded_key
        else:
            # Local/test fallback only. Production supplies AUTH_JWT_PRIVATE_KEY so
            # independently started API processes share the same signing identity.
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_key = cast(rsa.RSAPrivateKey, private_key)
        self.public_key: rsa.RSAPublicKey = self.private_key.public_key()
        self.algorithm = settings.auth_jwt_algorithm
        self.key_id = settings.auth_jwt_key_id
        self.issuer = settings.auth_jwt_issuer
        self.audience = settings.auth_jwt_audience
        self.ttl_seconds = settings.auth_access_ttl_seconds

    def issue(self, context: AuthContext) -> str:
        now = utcnow()
        return jwt.encode(
            {
                "sub": context.user_id,
                "tenant_id": context.tenant_id,
                "roles": list(context.roles),
                "iss": self.issuer,
                "aud": self.audience,
                "iat": now,
                "exp": now + timedelta(seconds=self.ttl_seconds),
            },
            self.private_key,
            algorithm=self.algorithm,
            headers={"kid": self.key_id, "typ": "JWT"},
        )

    def verify(self, token: str) -> AuthContext:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("kid") != self.key_id or header.get("alg") != self.algorithm:
                raise UnauthenticatedError("The access credential is invalid.")
            payload = jwt.decode(
                token,
                self.public_key,
                algorithms=[self.algorithm],
                issuer=self.issuer,
                audience=self.audience,
                leeway=30,
                options={"require": ["sub", "tenant_id", "roles", "iat", "exp"]},
            )
            roles = tuple(str(role) for role in payload["roles"])
            if not roles or any(role not in {"member", "admin"} for role in roles):
                raise UnauthenticatedError("The access credential is invalid.")
            return AuthContext(
                tenant_id=str(payload["tenant_id"]),
                user_id=str(payload["sub"]),
                roles=roles,
            )
        except UnauthenticatedError:
            raise
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as exc:
            raise UnauthenticatedError("The access credential is invalid or expired.") from exc


class AuthService:
    def __init__(self, session_factory: SessionFactory, settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.passwords = PasswordHasher()
        self.tokens = AccessTokenCodec(settings)

    @staticmethod
    def normalize_email(email: str) -> str:
        return email.strip().casefold()

    @staticmethod
    def _secret_hash(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()

    @staticmethod
    def _credential(session_id: str, secret: str) -> str:
        return f"{session_id}.{secret}"

    @staticmethod
    def _parse_credential(credential: str | None) -> tuple[str, str]:
        if not credential or "." not in credential:
            raise InvalidRefreshSessionError("The refresh session is invalid.")
        session_id, secret = credential.split(".", 1)
        if not session_id.startswith("session_") or len(secret) < 32:
            raise InvalidRefreshSessionError("The refresh session is invalid.")
        return session_id, secret

    def create_identity(
        self,
        *,
        tenant_id: str,
        tenant_name: str,
        user_id: str,
        email: str,
        password: str,
        role: Literal["member", "admin"] = "member",
    ) -> None:
        """Deployment/bootstrap helper; public self-registration is intentionally absent."""
        normalized = self.normalize_email(email)
        with self.session_factory() as session, session.begin():
            tenant = session.get(TenantRow, tenant_id)
            if tenant is None:
                session.add(TenantRow(tenant_id=tenant_id, name=tenant_name, active=True))
            user = session.get(UserRow, user_id)
            if user is None:
                session.add(
                    UserRow(
                        user_id=user_id,
                        email=email.strip(),
                        email_normalized=normalized,
                        password_hash=self.passwords.hash(password),
                        active=True,
                    )
                )
            elif user.email_normalized != normalized:
                raise ValueError("The user_id is already assigned to a different email.")
            membership = session.get(TenantUserMembershipRow, (tenant_id, user_id))
            if membership is None:
                session.add(
                    TenantUserMembershipRow(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        role=role,
                        active=True,
                    )
                )

    def login(self, request: LoginRequest) -> AuthenticationResult:
        normalized = self.normalize_email(request.email)
        with self.session_factory() as session, session.begin():
            row = session.execute(
                select(UserRow, TenantUserMembershipRow, TenantRow)
                .join(
                    TenantUserMembershipRow,
                    TenantUserMembershipRow.user_id == UserRow.user_id,
                )
                .join(TenantRow, TenantRow.tenant_id == TenantUserMembershipRow.tenant_id)
                .where(
                    UserRow.email_normalized == normalized,
                    UserRow.active.is_(True),
                    TenantUserMembershipRow.tenant_id == request.tenant_id,
                    TenantUserMembershipRow.active.is_(True),
                    TenantRow.active.is_(True),
                )
            ).first()
            if row is None:
                raise UnauthenticatedError("The email, password, or tenant is invalid.")
            user, membership, tenant = row
            try:
                self.passwords.verify(user.password_hash, request.password)
            except (VerifyMismatchError, VerificationError, InvalidHashError) as exc:
                raise UnauthenticatedError(
                    "The email, password, or tenant is invalid."
                ) from exc
            if self.passwords.check_needs_rehash(user.password_hash):
                user.password_hash = self.passwords.hash(request.password)
            session_id = new_id("session")
            secret = secrets.token_urlsafe(48)
            now = utcnow()
            session.add(
                AuthSessionRow(
                    session_id=session_id,
                    tenant_id=tenant.tenant_id,
                    user_id=user.user_id,
                    refresh_secret_hash=self._secret_hash(secret),
                    expires_at=now + timedelta(days=self.settings.auth_refresh_ttl_days),
                    created_at=now,
                    updated_at=now,
                )
            )
            context = AuthContext(tenant.tenant_id, user.user_id, (membership.role,))
            response = self._access_response(context, session_id, user.email, tenant.name)
            return AuthenticationResult(response, self._credential(session_id, secret))

    def refresh(self, credential: str | None) -> AuthenticationResult:
        session_id, old_secret = self._parse_credential(credential)
        new_secret = secrets.token_urlsafe(48)
        now = utcnow()
        with self.session_factory() as session, session.begin():
            result = session.execute(
                update(AuthSessionRow)
                .where(
                    AuthSessionRow.session_id == session_id,
                    AuthSessionRow.refresh_secret_hash == self._secret_hash(old_secret),
                    AuthSessionRow.revoked_at.is_(None),
                    AuthSessionRow.expires_at > now,
                )
                .values(
                    refresh_secret_hash=self._secret_hash(new_secret),
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise InvalidRefreshSessionError("The refresh session is invalid or expired.")
            auth_session = session.get(AuthSessionRow, session_id)
            assert auth_session is not None
            row = session.execute(
                select(UserRow, TenantUserMembershipRow, TenantRow)
                .join(
                    TenantUserMembershipRow,
                    (TenantUserMembershipRow.user_id == UserRow.user_id)
                    & (TenantUserMembershipRow.tenant_id == auth_session.tenant_id),
                )
                .join(TenantRow, TenantRow.tenant_id == TenantUserMembershipRow.tenant_id)
                .where(
                    UserRow.user_id == auth_session.user_id,
                    UserRow.active.is_(True),
                    TenantUserMembershipRow.active.is_(True),
                    TenantRow.active.is_(True),
                )
            ).first()
            if row is None:
                auth_session.revoked_at = now
                raise InvalidRefreshSessionError("The refresh session is invalid or expired.")
            user, membership, tenant = row
            context = AuthContext(tenant.tenant_id, user.user_id, (membership.role,))
            response = self._access_response(context, session_id, user.email, tenant.name)
            return AuthenticationResult(
                response,
                self._credential(session_id, new_secret),
            )

    def logout(self, credential: str | None) -> None:
        try:
            session_id, secret = self._parse_credential(credential)
        except InvalidRefreshSessionError:
            return
        now = utcnow()
        with self.session_factory() as session, session.begin():
            session.execute(
                update(AuthSessionRow)
                .where(
                    AuthSessionRow.session_id == session_id,
                    AuthSessionRow.refresh_secret_hash == self._secret_hash(secret),
                    AuthSessionRow.revoked_at.is_(None),
                )
                .values(revoked_at=now, updated_at=now)
            )

    def revoke(self, context: AuthContext, request: RevokeRequest) -> None:
        now = utcnow()
        with self.session_factory() as session, session.begin():
            statement = update(AuthSessionRow).where(
                AuthSessionRow.tenant_id == context.tenant_id,
                AuthSessionRow.user_id == context.user_id,
                AuthSessionRow.revoked_at.is_(None),
            )
            if not request.all_sessions:
                statement = statement.where(AuthSessionRow.session_id == request.session_id)
            session.execute(statement.values(revoked_at=now, updated_at=now))

    def authenticate_access(self, authorization: str | None) -> AuthContext:
        if not authorization or not authorization.startswith("Bearer "):
            raise UnauthenticatedError("A Bearer access credential is required.")
        context = self.tokens.verify(authorization.removeprefix("Bearer ").strip())
        with self.session_factory() as session:
            membership = session.get(
                TenantUserMembershipRow, (context.tenant_id, context.user_id)
            )
            user = session.get(UserRow, context.user_id)
            tenant = session.get(TenantRow, context.tenant_id)
            if (
                membership is None
                or user is None
                or tenant is None
                or not membership.active
                or not user.active
                or not tenant.active
            ):
                raise UnauthenticatedError("The access credential is no longer authorized.")
            return AuthContext(context.tenant_id, context.user_id, (membership.role,))

    @staticmethod
    def require_admin(context: AuthContext) -> None:
        if not context.is_admin:
            raise ForbiddenError("Tenant administrator role is required.")

    def _access_response(
        self,
        context: AuthContext,
        session_id: str,
        email: str,
        tenant_name: str,
    ) -> AccessResponse:
        return AccessResponse(
            access_token=self.tokens.issue(context),
            expires_in=self.settings.auth_access_ttl_seconds,
            session_id=session_id,
            user={"user_id": context.user_id, "email": email},
            tenant={"tenant_id": context.tenant_id, "name": tenant_name},
            roles=list(context.roles),
        )


def validate_cookie_origin(origin: str | None, settings: Settings) -> None:
    if origin is None or origin not in settings.auth_allowed_origins:
        raise ForbiddenError("The request Origin is not allowed for cookie authentication.")


def validate_csrf(
    *,
    header_token: str | None,
    cookie_token: str | None,
    settings: Settings,
) -> None:
    if settings.auth_cookie_samesite != "none":
        return
    if (
        not header_token
        or not cookie_token
        or not secrets.compare_digest(header_token, cookie_token)
    ):
        raise ForbiddenError("CSRF validation failed.")
