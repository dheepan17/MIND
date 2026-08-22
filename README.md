# MIND

A scalable monorepo architecture for modular development.

## Structure

- `apps/` — standalone applications
- `packages/` — shared libraries
- `services/` — backend services
- `tests/` — test utilities and configurations
- `docs/` — documentation
- `scripts/` — maintenance and build scripts

## Getting Started

See each package, app, or service directory for specific instructions.

```
MIND
├─ .ruff_cache
│  ├─ 0.16.4
│  └─ CACHEDIR.TAG
├─ AGENTS.md
├─ apps
├─ docs
│  └─ PROJECT_RULES.md
├─ packages
│  ├─ config
│  ├─ logger
│  └─ types
├─ README.md
├─ scripts
├─ security
│  ├─ permissions
│  │  ├─ capabilities.md
│  │  └─ roles.md
│  ├─ policies
│  │  ├─ agent-policy.md
│  │  ├─ data-policy.md
│  │  └─ tool-policy.md
│  └─ threat-model
│     └─ threats.md
├─ services
│  └─ mind-core
│     ├─ .ruff_cache
│     │  ├─ 0.16.4
│     │  │  ├─ 13195282439881666536
│     │  │  └─ 3849493613052413446
│     │  └─ CACHEDIR.TAG
│     ├─ app
│     │  ├─ agents
│     │  │  └─ __init__.py
│     │  ├─ AGENTS.md
│     │  ├─ config.py
│     │  ├─ events
│     │  │  └─ __init__.py
│     │  ├─ main.py
│     │  ├─ memory
│     │  │  └─ __init__.py
│     │  ├─ permissions
│     │  │  └─ __init__.py
│     │  ├─ planner
│     │  │  └─ __init__.py
│     │  ├─ tools
│     │  │  └─ __init__.py
│     │  └─ __init__.py
│     ├─ migrations
│     │  ├─ 0001_init.sql
│     │  ├─ run.py
│     │  └─ __init__.py
│     ├─ mind_core.egg-info
│     │  ├─ dependency_links.txt
│     │  ├─ PKG-INFO
│     │  ├─ requires.txt
│     │  ├─ SOURCES.txt
│     │  └─ top_level.txt
│     ├─ pyproject.toml
│     └─ tests
│        ├─ test_schema.py
│        └─ __init__.py
└─ tests
   ├─ integration
   └─ unit

```