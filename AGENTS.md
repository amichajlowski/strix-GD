# Repository Guidelines

## Project Structure & Module Organisation

`strix/` contains the Python package and CLI entry point. Key areas are `strix/core/` for scan orchestration, `strix/agents/` for agent setup and prompts, `strix/tools/` for integrations, `strix/runtime/` for container/session handling, `strix/interface/` for CLI/TUI code, and `strix/report/` for findings. Built-in security knowledge lives in `strix/skills/`. Tests are in `tests/`, documentation in `docs/`, container assets in `containers/`, and helper scripts in `scripts/`.

## Build, Test, and Development Commands

- `make setup-dev`: install development dependencies with `uv` and enable pre-commit hooks.
- `uv run strix --target https://example.com`: run the CLI locally against a test target.
- `uv run pytest`: run the test suite.
- `make format`: format Python with Ruff.
- `make lint`: run Ruff checks with auto-fixes.
- `make type-check`: run strict mypy and pyright checks.
- `make security`: run Bandit over `strix/`.
- `make check-all`: run formatting, linting, typing, and security checks.
- `./scripts/build.sh`: build a PyInstaller release binary.
- `./scripts/docker.sh dev`: build the local sandbox image.

## Coding Style & Naming Conventions

Use Python 3.12+ with strict typing. Ruff enforces a 100-character line limit, double quotes, space indentation, import ordering, and broad lint coverage. Use `snake_case` for functions, variables, modules, and test files; `PascalCase` for classes; and clear names for security concepts. Keep public functions typed and documented where behaviour is not obvious.

## Testing Guidelines

Tests use `pytest` with `pytest-asyncio` in automatic mode. Add or update tests under `tests/` for behavioural changes, especially execution, hooks, local sources, reporting, and runtime logic. Name files `test_<area>.py` and test functions `test_<expected_behaviour>`. Run `uv run pytest` before opening a PR, then `make check-all` for the full quality gate.

## Commit & Pull Request Guidelines

Recent history uses short imperative summaries, sometimes with prefixes such as `fix:` and PR references, for example `fix: route ollama models through ollama_chat so tool calling works (#562)`. Keep commits focused. PRs should link the issue, describe what changed and why, include test evidence, update docs for user-facing changes, and add screenshots or terminal output for CLI/TUI changes where useful.

## Security & Configuration Tips

Never commit API keys, provider tokens, scan artefacts containing client data, or target-specific secrets. Configure providers with environment variables such as `STRIX_LLM` and `LLM_API_KEY`. Anonymise personal or client identifiers in examples and logs as `XXXX`. Treat this repository as security-sensitive: minimise noisy output and keep Bandit findings fixed or explicitly justified.
