# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, adapted for a small standalone repo.

## [v0.1.0] - 2026-06-05

### Added
- initial public GitHub release for Rexterm
- standalone browser terminal runtime in `app/server.py`
- dark terminal UI with vendored xterm assets
- login page with server-side auth/session flow
- settings dropdown in terminal header
- icon-only settings and logout controls
- host-mode run script and systemd installer
- Docker Compose deployment path
- vendored tmux fallback for systems without host tmux
- screenshots in README
- MIT license

### Changed
- README expanded with install, auth, persistence, troubleshooting, versioning, and screenshot documentation
- repo cleaned for public publishing by removing stale local-only files and obsolete stub runtime/test files
- backend now auto-detects vendored tmux and injects required library path automatically
- username change in settings now uses a single inline field with save button

### Fixed
- terminal header/auth UI no longer breaks page initialization from missing button bindings
- authenticated password-change validation errors stay inline instead of forcing logout/login
- terminal runtime works without manual tmux PATH/LD_LIBRARY shell hacks when vendored tmux is present
