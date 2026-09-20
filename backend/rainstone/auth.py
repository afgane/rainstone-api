import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from rainstone.config import Settings, get_settings
from rainstone.db import get_session
from rainstone.models import Owner, Tenant


@dataclass(frozen=True)
class Identity:
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    source_id: str
    label: str
    is_admin: bool


def current_identity(
    x_rainstone_tenant: str = Header(default="anvil-demo"),
    x_rainstone_user: str = Header(default="alice"),
    x_rainstone_admin: str | None = Header(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Identity:
    if settings.auth_mode == "trusted-proxy" and (not x_rainstone_tenant or not x_rainstone_user):
        raise HTTPException(401, "Trusted proxy identity headers are required")
    tenant = session.scalar(select(Tenant).where(Tenant.slug == x_rainstone_tenant))
    if tenant is None:
        raise HTTPException(401, "Unknown tenant")
    owner = session.scalar(
        select(Owner).where(Owner.tenant_id == tenant.id, Owner.source_id == x_rainstone_user)
    )
    if owner is None:
        raise HTTPException(403, "User is not enrolled in this tenant")
    requested_admin = x_rainstone_admin == "true"
    if requested_admin and not owner.is_admin:
        raise HTTPException(403, "Administrator scope required")
    return Identity(tenant.id, owner.id, owner.source_id, owner.label, owner.is_admin and requested_admin)
