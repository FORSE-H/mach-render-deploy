# Security Policy

MACH is an open catalog of open-source healthcare software. This repository
contains the harvester and the public read-only REST + MCP API that power
[machcatalog.org](https://machcatalog.org). It stores **no patient or clinical
data** — only public metadata about open-source projects (names, repositories,
licences, and signals harvested from public APIs).

## Reporting a vulnerability

If you find a security issue, please report it privately rather than opening a
public issue:

- Use GitHub's **private vulnerability reporting** on this repository
  (Security → Report a vulnerability), or
- Email **mach@machcatalog.org**.

Please include a description, steps to reproduce, and the affected version or
commit. We aim to acknowledge reports within a few business days.

## Scope

In scope: the harvester (`ingest.py`, `core/`, `adapters/`), the API/MCP edges
(`edges/`), and the GitHub Actions workflows in this repository.

Out of scope: the third-party public services we harvest from (GitHub, GitLab,
PyPI, conda-forge, npm, CRAN, Hugging Face, OpenAlex, Software Heritage) — report
issues in those to their respective maintainers.

## Supported versions

This project is deployed continuously from the `main` branch; security fixes are
applied to `main`. There is no separate long-term support branch.
