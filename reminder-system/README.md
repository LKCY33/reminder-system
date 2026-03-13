# Reminder System Repository

This repository is the development source for the `reminder-system` skill.

It exists under `repositories/` because this is where the skill is developed, tested, and prepared before installation into the active `skills/` layer.

## Development Workflow

The intended workflow is:

1. develop and iterate in `repositories/reminder-system/reminder-system/`
2. run repository-side validation here
3. review install readiness
4. install into `skills/` only after the repository copy is considered ready

This means the repository copy is the development source, while the installed copy under `skills/` is the active deployment target.

## Design Principle: Two Paths, One Runtime Model

The repository path and the installed-skill path are intentionally different parts of the workspace lifecycle:

- `repositories/` -> development source
- `skills/` -> installed active skills

But the skill runtime itself should avoid path-based hidden mode branching.

Preferred model:

- keep one runtime behavior model
- keep paths relative to the skill root by default
- allow explicit overrides when needed
- do not make core runtime behavior depend on whether the code lives under `repositories/` or `skills/`

Current notify handoff path resolution is intended to follow this same rule:

- first honor explicit overrides such as `NOTIFY_EXECUTOR` or `NOTIFY_ROOT`
- then prefer a colocated installed-skill path under `skills/`
- finally fall back to the repository development copy of `notify`

## Repository Responsibilities

This repository should contain:

- the skill implementation
- repository-side validation and test flows
- repository-facing documentation
- readiness improvements before installation

It should not assume that repository-only paths are permanent runtime truth.

## Related Documents

- `SKILL.md` — operational behavior of the skill itself
- `development-system/repositories/reminder-system-install-readiness.md` — install-readiness tracking
- `development-system/repositories/reminder-system-install-validation-plan.md` — install-validation execution plan
- `development-system/repositories/reminder-system-validation-stage-architecture-decision.md` — validation stage architecture and layering rationale
- `development-system/repositories/reminder-system-roadmap.md` — repository/skill evolution roadmap
- `development-system/repositories/notify-vs-reminder-system-boundary.md` — delivery/orchestration boundary

## Validation Entry Point

The repository now includes a fixed install-validation entrypoint:

- `scripts/install_validation.py`

Current intent:

- use `self-check` and `preinstall` as higher-frequency repository-side validation layers
- use `install-copy-check` as an install-shaped artifact validation layer
- use `skills-install-check` as the real `skills/`-layer non-live validation layer
- treat `live-e2e` as a later explicit confidence stage rather than a default validation step

This keeps validation layered instead of collapsing everything into one deployment-like action.

### How To Use The Entry Point

Use stage-local modes when you only need a local or incremental validation step:

- `python3 scripts/install_validation.py self-check`
- `python3 scripts/install_validation.py preinstall`
- `python3 scripts/install_validation.py install-copy-check`
- `python3 scripts/install_validation.py skills-install-check`

Use `full` when you need one complete non-live readiness-review run:

- `python3 scripts/install_validation.py full`

`full` is the canonical non-live readiness-review command.
Stage-local runs are useful for debugging and iteration, but they are not the same thing as a full install-readiness conclusion.

## Current State

Current state:

- working repository-side skill prototype
- closer to install-ready than before
- still undergoing repository-side consistency and readiness tightening
