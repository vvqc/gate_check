# Repository Guidelines

## Project Structure & Module Organization

`gate_check` is currently a minimal repository: the only tracked project file is `README.md`. There are no source modules, tests, or assets yet. Keep the README as the entry point for project purpose, setup, and usage. When adding implementation, introduce clearly named directories such as `src/`, `tests/`, and `assets/` only as needed, and document their roles.

## Build, Test, and Development Commands

No build system, dependency manifest, or local runtime command is configured. Use these repository checks when preparing changes:

- `git status --short`: inspect modified and untracked files.
- `git diff --check`: detect whitespace errors in tracked changes.
- `git diff`: review unstaged edits; use `git diff --cached` for staged changes.

The first implementation change should document exact installation, development, build, and test commands in `README.md`. Do not assume that commands such as `npm test` or `make build` are available.

## Coding Style & Naming Conventions

No language-specific style, formatter, or linter is established. For Markdown, use descriptive headings, concise paragraphs, fenced code blocks with language identifiers, and relative links to repository files. Indent nested list items consistently with spaces. Choose descriptive filenames and follow the selected language's standard naming and indentation conventions when introducing code. Add formatter or linter configuration alongside that code.

## Testing Guidelines

No testing framework, test naming convention, or coverage threshold exists. Documentation changes should be checked for accurate paths, commands, and rendered Markdown. When introducing executable behavior, add appropriate tests, document how to run them, and adopt the chosen framework's standard test filenames. Cover expected behavior and relevant failure cases.

## Commit & Pull Request Guidelines

The two existing commits both use `first commit`, so no meaningful commit convention is established. Use short, imperative summaries, such as `Document local setup`, and keep commits focused. Pull requests should explain the change, its purpose, and validation performed. Link relevant issues and include screenshots for visible interface changes when applicable.
