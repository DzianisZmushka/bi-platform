# ADR-004: uv as the only Python tool

- Status: Accepted
- Date: 2026-11-05

## Context

The backend is a multi-package Python monorepo (`formats`, `engine`, `cli`, later `runtime`). Mixing pip, Poetry and virtualenv instructions leads to drift between machines and CI.

## Decision

uv manages the Python version (`.python-version` = 3.12), the virtual environment, dependencies and the lockfile. The repo root is a uv workspace whose members are the packages under `packages/`. `uv.lock` is committed. The repo never contains `requirements*.txt`, `poetry.lock`, `Pipfile` or `setup.py`, and docs never tell anyone to run `pip install`.

## Consequences

- One command sets up a machine: `uv sync`.
- Dependencies are added per package: `uv add --package obi-engine <dep>`.
- CI uses the official uv setup action and runs `uv sync --locked`.
