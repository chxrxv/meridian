"""ACL model: users, groups (with implication), and document access rules.

Design
------
* A *principal token* is a string: ``user:<id>`` or ``group:<id>``.
* A document lists the principal tokens allowed to read it (allow-only; no deny rules,
  which keeps the evaluation order trivial and removes a class of bypass bugs).
* A user's *effective principals* are their own ``user:`` token plus every group they are in,
  expanded transitively through ``implies`` (e.g. ``finance-managers`` implies ``finance``).
* Every user implicitly holds ``group:everyone``.
* Access check: ``doc.allowed & user.effective_principals != {}``. Empty ACL => nobody.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from pydantic import BaseModel, Field

EVERYONE = "group:everyone"


def user_token(user_id: str) -> str:
    return f"user:{user_id}"


def group_token(group_id: str) -> str:
    return f"group:{group_id}"


class Group(BaseModel):
    id: str
    description: str = ""
    implies: list[str] = Field(default_factory=list)  # group ids this group also grants


class User(BaseModel):
    id: str
    name: str
    title: str
    department: str
    groups: list[str] = Field(default_factory=list)


class Directory(BaseModel):
    """The synthetic identity directory (stand-in for Cloud Identity / Workspace groups)."""

    users: dict[str, User]
    groups: dict[str, Group]

    @classmethod
    def load(cls, path: str | Path) -> Directory:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            users={u["id"]: User(**u) for u in raw["users"]},
            groups={g["id"]: Group(**g) for g in raw["groups"]},
        )

    def get_user(self, user_id: str) -> User:
        try:
            return self.users[user_id]
        except KeyError:
            raise UnknownUserError(user_id) from None

    def effective_principals(self, user_id: str) -> frozenset[str]:
        """Own token + transitive closure of group memberships + everyone.

        Unknown users raise: an unauthenticated caller must never fall back to a default.
        Unknown group ids in a membership list are ignored (they grant nothing).
        """
        user = self.get_user(user_id)
        seen: set[str] = set()
        queue: deque[str] = deque(user.groups)
        while queue:
            gid = queue.popleft()
            if gid in seen or gid not in self.groups:
                continue
            seen.add(gid)
            queue.extend(self.groups[gid].implies)
        return frozenset({user_token(user_id), EVERYONE, *(group_token(g) for g in seen)})


class UnknownUserError(KeyError):
    """Raised for an identity that is not in the directory."""


def can_access(allowed: frozenset[str] | set[str] | list[str], principals: frozenset[str]) -> bool:
    return bool(set(allowed) & principals)
