# Branch: `<branch-name>`

## Objective

- Goal: `<why this branch exists>`
- Included: `<bounded scope>`
- Excluded: `<important non-goals>`
- Acceptance criteria: `<observable completion conditions>`

## Constraints and decisions

- Base/PR target: `<branch>`
- Role: `<feature/fix/chore/integration>`
- Worktree: `<sibling path; one branch only>`
- Owned paths: `<paths this branch may edit; no-go paths if useful>`
- History: [<filename>.history.md](<filename>.history.md)
- Decisions: `<durable owner or implementation decisions>`

## Risks or blockers

`<Unresolved issue affecting continuation, or none>`

## Next action

`<One immediate safe action>`

## Validation checkpoint

`<Latest meaningful commands/results, explicit deferrals, or not yet required>`

## Merge and cleanup

- Primary checkout: `<path used for merge/cleanup>`
- After merge: verify reachability from the remote default branch, archive this handoff/history, remove the sibling worktree, then delete local/remote branches only with authorization.

Do not routinely copy tip, clean/dirty state, upstream tracking, ahead/behind counts, or complete stash inventory from Git. Record identifiers only when essential to recovery or a durable milestone.
