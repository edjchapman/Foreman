<!--
PR title MUST follow Conventional Commits — it becomes the permanent squash-merge
commit subject (e.g. feat(worker): …, fix(api): …, build(tooling): …, docs(readme): …).
-->

## What & why

<!-- What changed and the reason. Link any issue or ADR. -->

## Checklist

- [ ] `make preflight` is green (ruff + mypy + tests + audit + docs)
- [ ] New application code is type-annotated (`make typecheck`)
- [ ] Tests added/updated for the change
- [ ] Docs / ADRs updated if behaviour or architecture changed
