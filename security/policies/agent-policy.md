# Agent Policy

Governs agent autonomy, identity, and behavioral boundaries.

## Identity
- Every agent has a unique id, owner, and declared purpose.
- Agents act only within the scope of their assigned role.

## Autonomy Levels
- **Level 0**: suggest only, human executes.
- **Level 1**: execute read-only actions autonomously.
- **Level 2**: execute approved write actions autonomously.

## Rules
1. An agent cannot grant itself new capabilities.
2. Cross-agent actions require the target agent's consent/role match.
3. Off-plan actions trigger a review gate via the event bus.
4. Agents must stop on policy violation and emit a security event.
