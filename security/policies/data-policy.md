# Data Policy

Rules for how data is stored, accessed, and shared across mind-core.

## Classification
- **Public**: safe to log and share.
- **Internal**: restricted to authorized agents/roles.
- **Secret**: credentials, keys, and PII; never logged.

## Rules
1. Secrets must be sourced from the secret store, never hardcoded.
2. PII requires `data:pii` capability to read or write.
3. Data at rest and in transit must be encrypted.
4. Logs must redact all `Secret` classification fields.
5. Memory stores honor retention and deletion requests.
