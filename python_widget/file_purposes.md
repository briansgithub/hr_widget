2026-07-17-uncommitted-summary

No application code is modified. All 14 changes implement the portable handoff framework.

### Modified files

1. `.cursor/rules/handoff-maintenance.mdc`  
   Makes Cursor read repository policy and run the handoff validator.

2. `GEMINI.md`  
   Adds the same handoff lifecycle instructions for Gemini/Google agents.

3. `docs/handoffs/MASTER_HANDOFF.md`  
   Documents templates, repository configuration, validation requirements, and CI.

4. `docs/handoffs/README.md`  
   Adds the current chore branch to the active index and links templates/configuration.

5. `docs/handoffs/main.md`  
   Corrects `main`’s exact tip and notes current development is on the chore branch.

### New files

6. `AGENTS.md`  
   Vendor-neutral entry point for agents and IDEs.

7. `.github/workflows/handoff-validation.yml`  
   Runs handoff validation on PRs and pushes to `main`.

8. `docs/handoffs/REPOSITORY.md`  
   Repository-specific branch, merge, permission, and test policy.

9. `docs/handoffs/chore/portable-handoff-kit.md`  
   Current operating handoff for this branch.

10. `docs/handoffs/chore/portable-handoff-kit.history.md`  
    Append-only milestone history for this branch.

11. `docs/handoffs/templates/BRANCH_HANDOFF.md`  
    Template for new active branch handoffs.

12. `docs/handoffs/templates/BRANCH_HISTORY.md`  
    Template for append-only branch histories.

13. `docs/handoffs/templates/ARCHIVED_HANDOFF.md`  
    Template for frozen post-merge handoffs.

14. `scripts/validate_handoffs.py`  
    Checks required files, active/archive pairs, links, size limits, and append-only history.

The external reusable copy at `H:\Desktop\ai_agent_guides\git-handoff-system` is outside this Git repository and therefore not part of these uncommitted changes. Both validators currently pass.