from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.config import get_settings
from app.db.models import AdminMembership, AdminRole
from app.db.session import get_db


class AdminPermission(StrEnum):
    researchers_read = "researchers:read"
    researchers_write = "researchers:write"
    overview_read = "overview:read"
    users_read = "users:read"
    users_invite = "users:invite"
    users_suspend = "users:suspend"
    users_delete = "users:delete"
    agents_read = "agents:read"
    agents_restart = "agents:restart"
    agents_manage = "agents:manage"
    integrations_read = "integrations:read"
    audit_read = "audit:read"
    audit_export = "audit:export"
    memberships_manage = "memberships:manage"
    directory_sync = "directory:sync"
    runtime_read = "runtime:read"
    runtime_write = "runtime:write"
    system_read = "system:read"
    knowledge_read = "knowledge:read"
    knowledge_write = "knowledge:write"
    planning_write = "planning:write"
    catalog_read = "catalog:read"
    catalog_write = "catalog:write"
    catalog_publish = "catalog:publish"
    groups_write = "groups:write"


ROLE_PERMISSIONS = {
    AdminRole.super_admin: set(AdminPermission),
    AdminRole.operator: {
        AdminPermission.catalog_read,
        AdminPermission.researchers_read,
        AdminPermission.overview_read,
        AdminPermission.users_read,
        AdminPermission.users_invite,
        AdminPermission.users_suspend,
        AdminPermission.agents_read,
        AdminPermission.agents_restart,
        AdminPermission.agents_manage,
        AdminPermission.integrations_read,
        AdminPermission.audit_read,
        AdminPermission.audit_export,
        AdminPermission.runtime_read,
        AdminPermission.system_read,
        AdminPermission.knowledge_read,
    },
    AdminRole.campus_admin: {
        AdminPermission.catalog_read,
        AdminPermission.catalog_write,
        AdminPermission.catalog_publish,
        AdminPermission.researchers_read,
        AdminPermission.overview_read,
        AdminPermission.users_read,
        AdminPermission.agents_read,
        AdminPermission.agents_restart,
        AdminPermission.integrations_read,
        AdminPermission.runtime_read,
        AdminPermission.system_read,
        AdminPermission.knowledge_read,
        AdminPermission.knowledge_write,
        AdminPermission.planning_write,
        AdminPermission.groups_write,
    },
}


@dataclass(frozen=True)
class AdminPrincipal:
    user: AuthenticatedUser
    role: AdminRole
    organization_id: UUID | None
    permissions: frozenset[AdminPermission]
    bootstrap: bool = False


async def get_admin_principal(
    user: AuthenticatedUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> AdminPrincipal:
    bootstrap = str(user.id) in get_settings().admin_bootstrap_ids
    membership = await db.get(AdminMembership, user.id)
    if bootstrap:
        role, organization_id = AdminRole.super_admin, None
    elif membership is not None:
        role, organization_id = membership.role, membership.organization_id
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return AdminPrincipal(user, role, organization_id, frozenset(ROLE_PERMISSIONS[role]), bootstrap)


def require(permission: AdminPermission):
    async def dependency(principal: AdminPrincipal = Depends(get_admin_principal)) -> AdminPrincipal:
        if permission not in principal.permissions:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Permission denied")
        return principal

    return dependency
