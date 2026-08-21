# Threat Model

Identifies threats to mind-core and the controls that mitigate them.

## Assets
- Agent identities and credentials
- Stored memory and conversation data
- Tool execution surface
- Event bus integrity

## Threats
| ID | Threat | Vector | Mitigation |
|----|--------|--------|------------|
| T1 | Privilege escalation | Agent self-grants capability | Agent policy rule #1, deny-by-default |
| T2 | Secret exfiltration | Logging of secrets | Data policy rules #4, #1 |
| T3 | Unauthorized tool use | Missing pre-exec check | Tool policy, permissions gate |
| T4 | Event bus tampering | Spoofed events | Signed events, auth on publish |
| T5 | Prompt injection | Untrusted input to planner | Input sanitization, review gate |

## Assumptions
- The host running mind-core is trusted.
- Human operators are authorized by default.
