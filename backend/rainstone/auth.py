"""Viewer authorization.

Three explicit modes, chosen by configuration and never inferred from a URL
prefix:

`anvil-workspace` serves one fixed tenant and one fixed shared Galaxy account
behind the deployment's external admission boundary. No request input can select
another tenant or owner, or grant administrator scope.

`trusted-proxy` accepts an identity only from a proxy that strips client copies
of the same headers first; it has no fixture defaults.

`development` supplies fixture identities and refuses to start outside demo mode.
"""

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from rainstone.config import Settings, get_settings
from rainstone.db import get_session
from rainstone.models import Owner, Tenant

TENANT_HEADER = "x-rainstone-tenant"
USER_HEADER = "x-rainstone-user"
ADMIN_HEADER = "x-rainstone-admin"
SCOPE_HEADERS = (TENANT_HEADER, USER_HEADER, ADMIN_HEADER)


@dataclass(frozen=True)
class Identity:
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    source_id: str
    label: str
    is_admin: bool
    can_view_infrastructure: bool = False
    attribution: str = "Reports are attributed to this Galaxy account."


def _resolve(session: Session, tenant_slug: str, owner_source_id: str) -> tuple[Tenant, Owner]:
    tenant = session.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
    if tenant is None:
        raise HTTPException(401, "Unknown tenant")
    owner = session.scalar(
        select(Owner).where(Owner.tenant_id == tenant.id, Owner.source_id == owner_source_id)
    )
    if owner is None:
        raise HTTPException(403, "User is not enrolled in this tenant")
    return tenant, owner


def _resolve_configured_account(session: Session, tenant: Tenant, account: str) -> Owner:
    """Accept either the Galaxy source owner ID or the account name boot resolved."""
    matches = list(
        session.scalars(
            select(Owner).where(
                Owner.tenant_id == tenant.id,
                (Owner.source_id == account) | (Owner.label == account),
            )
        )
    )
    if not matches:
        raise HTTPException(
            500,
            "The configured shared Galaxy account is not enrolled for this instance; "
            "run bootstrap before serving reports",
        )
    if len(matches) > 1:
        raise HTTPException(500, "The configured shared Galaxy account is ambiguous")
    return matches[0]


def _workspace_identity(request: Request, session: Session, settings: Settings) -> Identity:
    supplied = [name for name in SCOPE_HEADERS if name in request.headers]
    if supplied:
        raise HTTPException(
            403,
            "This deployment reports one fixed workspace scope; "
            f"client identity headers are not accepted ({', '.join(sorted(supplied))})",
        )
    tenant = session.scalar(select(Tenant).where(Tenant.slug == settings.tenant_slug))
    if tenant is None:
        raise HTTPException(500, "This instance has no seeded tenant identity")
    owner = _resolve_configured_account(session, tenant, settings.workspace_owner_source_id)
    return Identity(
        tenant_id=tenant.id,
        owner_id=owner.id,
        source_id=owner.source_id,
        label=owner.label,
        is_admin=False,
        can_view_infrastructure=settings.workspace_infrastructure_visible,
        attribution=(
            "Work is attributed to the shared Galaxy account "
            f"'{owner.label}', not to individual workspace members."
        ),
    )


def current_identity(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Identity:
    if settings.auth_mode == "anvil-workspace":
        return _workspace_identity(request, session, settings)

    tenant_slug = request.headers.get(TENANT_HEADER)
    user = request.headers.get(USER_HEADER)
    if settings.auth_mode == "trusted-proxy":
        if not tenant_slug or not user:
            raise HTTPException(401, "Trusted proxy identity headers are required")
    else:
        tenant_slug = tenant_slug or settings.tenant_slug
        user = user or "alice"
    tenant, owner = _resolve(session, tenant_slug, user)
    requested_admin = request.headers.get(ADMIN_HEADER) == "true"
    if requested_admin and not owner.is_admin:
        raise HTTPException(403, "Administrator scope required")
    is_admin = owner.is_admin and requested_admin
    return Identity(
        tenant_id=tenant.id,
        owner_id=owner.id,
        source_id=owner.source_id,
        label=owner.label,
        is_admin=is_admin,
        can_view_infrastructure=is_admin,
    )
