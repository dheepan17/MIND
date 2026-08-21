# Roles

Roles group capabilities into assignable identities for agents and humans.

## Built-in Roles
- **guest**: read-only access to public resources.
- **operator**: read + limited write within assigned scope.
- **admin**: full control including role and capability management.

## Assignment Rules
1. Roles are assigned by an `admin` only.
2. An agent may hold at most one primary role plus scoped grants.
3. Role changes are logged as security events.
