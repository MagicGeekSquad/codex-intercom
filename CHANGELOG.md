# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-11-19

### Added

- `new` starts the first turn of a Codex session in a detached exec and claims its thread for `listen`.
- `status` and `listen` report the model, reasoning effort, and token usage from the Codex rollout when available.
- `send` preserves per-session model and reasoning effort on exec resume, with `--model` and `--effort` overrides and JSON reporting of their sources.
- Skill guidance: long tasks go in a file with a one-line pointer message, Codex keeps its final reply short, and waiting never uses polling or scheduled wake-ups.

### Fixed

- Exec resume no longer runs on the `config.toml` default model and reasoning effort instead of the session's own
  (`codex exec resume` ignores the saved settings; [openai/codex#32061](https://github.com/openai/codex/issues/32061)).
- `listen` preserves real stops and their last replies across queued sends, start timeouts, and exec exits.
- `new` and exec resume write hooks to the same data directory, including when `--dir` is relative.
- Cached rollout settings and turn usage survive appends larger than the initial lookup window.

## [1.0.0] - 2026-11-13

First public release.

### Added

- `send`: deliver a message to any Codex session (App or CLI). A working session gets it steered into
  the running turn; a stopped one starts a new turn via `codex queue`, falling back to
  `codex exec resume` in the session's own directory when the Codex App doesn't have it open.
- `listen`: one background process per Claude Code session watches every Codex session it manages and
  exits — waking Claude Code — exactly once when any of them needs attention, with the reason:
  `done`, `asking`, `approval`, `error`, `interrupted`, `ended`, `stalled`, `not_started`.
- Progress check-ins for long Codex tasks (default every 30 min, per session via `--checkin`), with a
  snapshot of what Codex has been doing; replaces hand-made scheduled tasks.
- Multi-session ownership: a Codex session belongs to one Claude Code session; others are refused
  unless they pass `--take-over`. Ownership lapses after 24 h without activity.
- Nothing is lost while nobody listens: attention is derived from Codex hook events each time, and an
  unreported stop is reported by the next `listen` (latest stop only, plus `earlier_stops`).
- Popup gate: while a session is managed, Codex asks its questions in its reply instead of App popups
  that nobody would answer.
- `list`, `status`, `claim`, `release`, `doctor`, `install`, `uninstall`, `gc`.
- `install.sh` / `uninstall.sh`, Claude Code plugin marketplace manifest, CI on Linux and macOS.

[1.1.0]: https://github.com/MagicGeekSquad/codex-intercom/releases/tag/v1.1.0
[1.0.0]: https://github.com/MagicGeekSquad/codex-intercom/releases/tag/v1.0.0
