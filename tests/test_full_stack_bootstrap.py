from copy import deepcopy
from typing import Any

import pytest

from tests.e2e.bootstrap_full_stack import _has_exact_runtime_scope

LIBRARY_ID = "00000000-0000-0000-0000-000000000123"


def _identity() -> dict[str, Any]:
    return {
        "principal": {"principalKind": "api_token"},
        "user": None,
        "workspaceMemberships": [],
        "effectiveGrants": [
            {
                "resourceKind": "library",
                "resourceId": LIBRARY_ID,
                "permissionKind": "library_read",
            },
            {
                "resourceKind": "library",
                "resourceId": LIBRARY_ID,
                "permissionKind": "library_write",
            },
        ],
    }


def test_exact_runtime_scope_accepts_only_required_library_grants() -> None:
    assert _has_exact_runtime_scope(_identity(), LIBRARY_ID)


@pytest.mark.parametrize(
    "extra_grant",
    [
        {
            "resourceKind": "library",
            "resourceId": LIBRARY_ID,
            "permissionKind": "library_admin",
        },
        {
            "resourceKind": "library",
            "resourceId": "00000000-0000-0000-0000-000000000999",
            "permissionKind": "library_read",
        },
        {
            "resourceKind": "system",
            "resourceId": "00000000-0000-0000-0000-000000000000",
            "permissionKind": "system_admin",
        },
    ],
)
def test_exact_runtime_scope_rejects_any_extra_grant(extra_grant: dict[str, str]) -> None:
    identity = _identity()
    identity["effectiveGrants"].append(extra_grant)

    assert not _has_exact_runtime_scope(identity, LIBRARY_ID)


def test_exact_runtime_scope_rejects_duplicate_grant() -> None:
    identity = _identity()
    identity["effectiveGrants"].append(deepcopy(identity["effectiveGrants"][0]))

    assert not _has_exact_runtime_scope(identity, LIBRARY_ID)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("principal", {"principalKind": "user"}),
        ("user", {"id": "00000000-0000-0000-0000-000000000456"}),
        ("workspaceMemberships", [{"workspaceId": "00000000-0000-0000-0000-000000000789"}]),
    ],
)
def test_exact_runtime_scope_rejects_non_token_identity_context(field: str, value: object) -> None:
    identity = _identity()
    identity[field] = value

    assert not _has_exact_runtime_scope(identity, LIBRARY_ID)
