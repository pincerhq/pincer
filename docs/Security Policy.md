# Security Policy 🔒

## Our Commitment

Pincer handles people's email, calendar, files, and personal data. We take that responsibility seriously. Security vulnerabilities in Pincer can expose users' entire digital lives, and we treat every report as urgent.

---

## Reporting a Vulnerability

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please report responsibly through one of these channels:

- **Email:** [security@pincer.dev](mailto:security@pincer.dev)
- **GitHub Security Advisories:** [Report a vulnerability](https://github.com/pincerhq/pincer/security/advisories/new)

### What to Include

- Description of the vulnerability
- Steps to reproduce (the more specific, the faster we can fix it)
- Affected versions
- Potential impact (what could an attacker do?)
- Suggested fix (if you have one)

### What Happens Next

| Timeline | What We Do |
|----------|-----------|
| **Within 24 hours** | Acknowledge your report |
| **Within 72 hours** | Assess severity and begin working on a fix |
| **Within 7 days** | Release a patch for critical vulnerabilities |
| **Within 30 days** | Release a patch for non-critical vulnerabilities |
| **After patch release** | Publish a security advisory with credit to the reporter |

---

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | ✅ Full support |
| Previous minor | ✅ Security patches only |
| Older versions | ❌ Please upgrade |

---

## Scope

### In Scope

- The Pincer agent codebase (`src/pincer/`)
- Bundled skills (`skills/`)
- Docker images published by the project
- The web dashboard
- The skill security scanner
- Authentication and authorization mechanisms
- Data storage and encryption

### Out of Scope

- Third-party skills not published by the Pincer project
- Vulnerabilities in upstream dependencies (report those to the dependency maintainer, but let us know too so we can update)
- Social engineering attacks against individual users
- Issues requiring physical access to the host machine

---

## Security Design Principles

These are the principles we follow when making security decisions. If you find a place where we're violating these, that's worth reporting:

1. **Deny by default.** Nothing has access unless explicitly granted. Users must be allowlisted. Skills must declare permissions. Tools must be registered.

2. **Least privilege.** Skills get the minimum permissions they need. The agent runs as an unprivileged user. The Docker container has no extra capabilities.

3. **Defense in depth.** Multiple layers — allowlist, sandbox, scanner, signing, audit log. Compromise of one layer shouldn't compromise the whole system.

4. **Fail closed.** If something goes wrong, deny access rather than allow it. If the allowlist can't be loaded, no one gets in. If a skill fails scanning, it doesn't load.

5. **Transparency.** Every action is logged. Users can see exactly what their agent did, when, and why. The audit log is not optional.

6. **No telemetry.** Pincer does not phone home. No analytics, no crash reports, no usage data sent anywhere. Your data stays on your machine.

---

## Recognition

We believe in recognizing the people who make Pincer safer:

- All security researchers who report valid vulnerabilities are credited in the security advisory (unless they prefer to remain anonymous)
- Significant security contributions are highlighted in release notes
- We maintain a Security Hall of Fame in this document (see below)

We don't currently have a paid bug bounty program, but we hope to establish one as the project grows. In the meantime, we offer our sincere gratitude and public recognition.

### Security Hall of Fame

*Be the first name on this list. Report something at security@pincer.dev.*

---

## Contact

- **Security reports:** security@pincer.sh
- **General questions about Pincer's security model:** [Discord #security](https://discord.gg/pincer) or [docs/security.md](docs/security.md)
- **PGP key:** Available upon request for encrypted communication

Thank you for helping keep Pincer and its users safe. 🦀
