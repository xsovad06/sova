# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

Use GitHub's built-in [private vulnerability reporting](https://github.com/xsovad06/sova/security/advisories/new) to submit a report. Include:

- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

### What to Expect

- **Acknowledgement** within 48 hours
- **Assessment** within 1 week
- **Fix or mitigation** timeline communicated after assessment

## Scope

### In Scope

- Authentication or authorization bypass in SOVA's adapter layer
- Secrets leaking through logs, error messages, or handoff files
- Command injection via task titles, issue bodies, or configuration values
- Unsafe file operations (path traversal, symlink attacks) in worktree or output handling

### Out of Scope

- Vulnerabilities in upstream dependencies (report to those projects directly)
- Issues requiring physical access to the host machine
- Social engineering attacks
- Denial of service against the local dashboard (single-user tool)

## Security Design

SOVA handles credentials exclusively through environment variables and the `gh` CLI's credential store. No secrets are stored in configuration files or the database. See the [README](README.md) for architecture details.
