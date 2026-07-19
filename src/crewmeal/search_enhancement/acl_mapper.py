from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID


class UnsupportedAclError(ValueError):
    """Raised when SharePoint permissions cannot be represented without widening."""


@dataclass(frozen=True, slots=True)
class ConnectorAcl:
    type: str
    value: str
    access_type: str = "grant"

    def as_dict(self) -> dict[str, str]:
        return {
            "type": self.type,
            "value": self.value,
            "accessType": self.access_type,
        }


def map_drive_item_permissions(
    permissions: list[dict[str, Any]],
) -> tuple[ConnectorAcl, ...]:
    entries: set[ConnectorAcl] = set()
    unsupported: list[str] = []

    for permission in permissions:
        permission_id = str(permission.get("id") or "unknown")
        if permission.get("link") is not None or permission.get("invitation") is not None:
            unsupported.append(f"{permission_id}: sharing link or invitation")
            continue

        identities = _permission_identities(permission)
        if not identities:
            unsupported.append(f"{permission_id}: missing Entra identity")
            continue

        for identity in identities:
            mapped = _map_identity(identity)
            if mapped is None:
                unsupported.append(f"{permission_id}: unsupported principal type")
            else:
                entries.add(mapped)

    if unsupported:
        raise UnsupportedAclError(
            "ACL_UNSUPPORTED: " + "; ".join(sorted(unsupported))
        )
    if not entries:
        raise UnsupportedAclError("ACL_EMPTY: no supported Entra principals were found.")
    return tuple(sorted(entries, key=lambda entry: (entry.type, entry.value)))


def acl_hash(entries: tuple[ConnectorAcl, ...]) -> str:
    ordered = sorted(entries, key=lambda entry: (entry.type, entry.value, entry.access_type))
    canonical = json.dumps(
        [entry.as_dict() for entry in ordered],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _permission_identities(permission: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    identity = permission.get("grantedToV2")
    if not isinstance(identity, dict):
        identity = permission.get("grantedTo")
    if isinstance(identity, dict):
        values.append(identity)

    identities = permission.get("grantedToIdentitiesV2")
    if not isinstance(identities, list):
        identities = permission.get("grantedToIdentities")
    if isinstance(identities, list):
        values.extend(identity for identity in identities if isinstance(identity, dict))
    return values


def _map_identity(identity_set: dict[str, Any]) -> ConnectorAcl | None:
    user = identity_set.get("user")
    if isinstance(user, dict) and _valid_object_id(user.get("id")):
        return ConnectorAcl(type="user", value=str(UUID(str(user["id"]))))

    group = identity_set.get("group")
    if isinstance(group, dict) and _valid_object_id(group.get("id")):
        return ConnectorAcl(type="group", value=str(UUID(str(group["id"]))))
    return None


def _valid_object_id(value: object) -> bool:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True
