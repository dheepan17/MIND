# Tool Policy

Defines the rules governing which tools agents may invoke and under what conditions.

## Scope
- Applies to all tools registered in `app/tools/`.
- Enforced by the permissions layer before any tool execution.

## Principles
- **Least privilege**: tools run with the minimum access required.
- **Explicit allow**: a tool is denied unless an active capability grants it.
- **Audit**: every tool invocation and its outcome must be recorded.

## Rules
1. Destructive tools (delete, overwrite, push) require `write` capability.
2. Network-external tools require `network` capability.
3. Shell execution requires `shell` capability and a human approval gate.
4. Unsigned or unverified tools are blocked by default.
