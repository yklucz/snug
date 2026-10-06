# Security Policy

## Supported versions

Security fixes target the current maintained release. Shared CLI/archive fixes originate on `main` and are intentionally ported to `windows`; Windows runtime and installer fixes are maintained on `windows`. This checkout identifies itself as Snug `1.8.0`; older releases may need an update before a fix can be applied.

## Reporting a vulnerability

Please report unpatched vulnerabilities privately using GitHub's **Report a vulnerability** control on this repository's [Security page](https://github.com/yklucz/snug/security), if available. Private reporting is a repository setting and may not be enabled. If the control is unavailable, open an issue requesting a private reporting channel without including vulnerability details or an exploit.

Include the affected version, platform, backend, and a minimal reproduction privately. Avoid sharing personal files or credentials. Do not disclose an unpatched vulnerability in a public issue or pull request.

See the [security model](docs/security.md) for extraction checks, runtime trust boundaries, and known limitations.
