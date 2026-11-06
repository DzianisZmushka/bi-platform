# runtime

`obi_runtime`: what a served project needs beyond rendering (ADR-014).

- `users.py`: the user store (`users.json` in the data directory, never in Git) and signed
  session cookies. Roles and attributes have the shape access.yaml declares.
- `deploy.py`: a served site, either a local project directory or a Git branch. A deploy runs
  fetch, validate, migrate, swap; a failed validation keeps the previous release live.

The HTTP server is `obi_cli/server.py` (ADR-007). The scheduler comes in Phase 3.
