# Codex Project Rules

## Git workflow

- `/Users/masaru/Projects/Trading` is the production checkout. Keep it on `main` and do not edit files there.
- Perform every code or documentation change in a dedicated Git worktree and branch.
- Create pull requests directly from the work branch to `main`. Do not use `develop` for new work.
- The main thread reviews the pull request, merges it, updates the production checkout, restarts affected services, and verifies the result.
- Pull request titles and descriptions must be written in Japanese.
- Keep one pull request focused on one coherent change. Report only the PR URL, changed files, verification results, unresolved items, and deployment steps.

## Platform separation

- `kabu_station_server/` contains Windows-side proxy code only.
- Do not place Mac-side or shared Python files in `kabu_station_server/`, even when logic could be shared.

## Model allocation

- Main thread: GPT-5.5 low.
- Investment hypotheses and important design decisions: GPT-5.5 medium.
- Technical lead: GPT-5.4 medium.
- Normal implementation: GPT-5.4-mini high.
- Git operations, investigation, and documentation: GPT-5.4-mini low or medium.
- Real-order logic review: GPT-5.5 medium or GPT-5.4 high.

## Context efficiency

- Read only relevant file sections and limit command output.
- Do not create a subagent for a deterministic one-step operation.
- Do not duplicate detailed implementation notes across chat, handover documents, and pull request descriptions.
- When work completes or becomes blocked, notify the next responsible person with a concise result or explicit question.
