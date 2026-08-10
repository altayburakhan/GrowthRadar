# AGENTS.md

## General Principles

- Prefer simple, maintainable solutions over clever ones.
- Write clean, modular, and reusable code.
- Keep functions small and focused on a single responsibility.
- Follow existing project structure and naming conventions.
- Avoid unnecessary dependencies.
- Never hardcode secrets, credentials, or API keys.
- Fail gracefully with meaningful error messages.
- Add logging where it helps debugging, not everywhere.
- Prefer configuration over hardcoded values.
- Optimize for readability first, performance second.

## Engineering Practices

- Think before implementing.
- Avoid duplicated code (DRY).
- Keep changes minimal and isolated.
- Make code deterministic whenever possible.
- Write idempotent operations when applicable.
- Handle edge cases explicitly.
- Validate external inputs.
- Prefer typed code where possible.

## Development Workflow

- Keep commits small and focused.
- Do not leave dead or commented-out code.
- Remove temporary debugging code before finishing.
- Update documentation when behavior changes.
- If a design decision is made, keep it consistent across the project.

## AI Agent Behavior

- Do not make assumptions when information is missing.
- Ask for clarification instead of guessing.
- Explain trade-offs before making major architectural decisions.
- Suggest better alternatives when appropriate.
- Prefer open-source and local-first solutions whenever possible.
- Think like a senior engineer, not just a code generator.