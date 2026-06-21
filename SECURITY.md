# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security vulnerabilities.

Instead, report them privately through GitHub's
[private vulnerability reporting](https://github.com/lancedb/geneva-examples/security/advisories/new)
("Report a vulnerability" under the repository's **Security** tab). If that is
unavailable, contact the LanceDB maintainers at security@lancedb.com.

Please include enough detail to reproduce the issue (affected file/CLI, steps, and
impact). We will acknowledge your report and keep you updated on remediation.

## Scope

This repository contains **example** Geneva UDFs and submission tooling. The most
relevant concerns are:

- **Credentials.** All secrets live in `config.yaml`, which is gitignored; never
  commit it. `config-example.yaml` is the only tracked template. A
  `detect-private-key` pre-commit hook and a CI secret scan guard against
  accidental leaks.
- **Dependencies.** CI runs an advisory `pip-audit` scan and Dependabot proposes
  updates; the `geneva`/`lancedb`/`pylance` betas are pinned to match the deployed
  cluster.

## Supported versions

This is an examples repository tracking the current Geneva/LanceDB beta stack; only
the latest `main` is supported.
