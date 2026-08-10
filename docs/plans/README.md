# Implementation plans

Records of *how* a non-trivial change was going to be built — the scope, the
decisions settled before implementation, the intended interface, and how the
result would be verified — plus, once the work lands, the **outcome**: what
shipped, what deviated from the plan, and why.

They complement the [ADRs](../adr/README.md): an ADR records a design *choice*
and its consequences; a plan records the *execution* of a piece of work —
usually referencing the ADRs and reviews that motivated it. Write one when a
change is big enough to plan before coding (multi-file refactors, new
subsystems, milestone slices); skip it for small fixes, where the PR
description is enough.

Conventions, mirroring `docs/adr/`:

- One file per plan, `NNNN-slug.md`, numbered in the order plans are approved.
- Each record states its **Status** (Approved → Implemented / Abandoned) and
  links the PR(s) that executed it.
- The plan text is frozen at approval; implementation learnings go in the
  **Outcome** section rather than by editing the plan — the gap between the
  two is the interesting part.

| Plan | Title | Status |
|------|-------|--------|
| [0001](0001-job-lifecycle-module.md) | `jobs/lifecycle.py` deep module (design review rec. A) | Implemented ([#150](https://github.com/edjchapman/Foreman/pull/150)) |
