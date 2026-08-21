# Capabilities

Capabilities are fine-grained permissions granted to roles or agents.

## Capability Catalog
- `read`: read public/internal resources.
- `write`: create/modify internal resources.
- `shell`: execute shell commands (gated).
- `network`: make outbound network requests.
- `data:pii`: access personally identifiable information.
- `admin:roles`: manage roles and capability grants.

## Grant Rules
1. Capabilities are deny-by-default.
2. Grants include an explicit scope and expiry.
3. Revocation takes effect immediately.
