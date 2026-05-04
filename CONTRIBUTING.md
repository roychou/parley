# Contributing

This is primarily a personal portfolio project, but the practices below are documented for transparency and to hold the author (Roy) to them.

## The AI-free rule

Modern AI tooling makes it possible to ship working code without internalizing the patterns underneath. For a project whose explicit purpose is to demonstrate engineering depth — to a reader who is a senior engineer or hiring manager evaluating that depth — that's a problem. So this project runs under a deliberate AI-free rule on skill-building components.

### Ramp period: Days 13–14

AI assistance (Claude Code, Cursor tab-completion, Copilot, etc.) is permitted across the whole codebase during the initial scaffolding phase. The goal is to get the project structure, dependencies, data pipeline, and first SDK call working end-to-end without friction.

### From Day 15 onwards: AI-free on skill-building components

The following components must be written without AI completion or AI generation. Reading reference code (open-source projects, Anthropic docs, MCP spec) is fine. Asking AI for explanations of concepts is fine. Pasting AI-generated code into these components is not.

- Agent decision logic
- Orchestration layer (supervisor dispatch, synthesis)
- Specialist agent implementations
- MCP server core handlers
- Eval harness core
- Financial data pipeline core (anything that does real computation, not glue)

### Where AI assistance stays on

- Tests (writing assertions, mock setup, fixtures)
- Configuration files (`pyproject.toml`, GitHub Actions, etc.)
- Glue code (CLI argument parsing, file I/O boilerplate, logging setup)
- Documentation (READMEs, ARCHITECTURE.md, this file)
- Repo housekeeping (`.gitignore`, dependency bumps)
- One-off shell commands

### Why this rule exists

Two reasons. First, this project is portfolio evidence — a reader should be able to assume that the engineering choices visible in the orchestration layer are the author's own, made deliberately, not borrowed from a model that happened to spit out a reasonable-looking pattern. Second, technical interviews for PM roles at AI companies frequently include live coding without AI assistance. The bar is "hold up coding on the fly without help." Building the load-bearing components AI-free is the deliberate practice that gets to that bar.

### Honest expectation

Following this rule adds roughly 20–30% to build time on covered components. The release schedule factors that in.

### Day 15 morning check

If on Day 15 there's an urge to extend the AI-assisted ramp another day or two, examine why before agreeing. Usually the urge means the next component is genuinely new (first specialist, first MCP server, first orchestration layer). The right move there is to read reference code, not to ask AI to generate one.

## Style

`ruff` is configured in `pyproject.toml`. Run `uv run ruff check` and `uv run ruff format` before committing.

## Tests

Tests live in `tests/`. Run with `uv run pytest`. AI assistance is on for tests — the point of the AI-free rule is the production code, not the test code.