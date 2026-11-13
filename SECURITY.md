# Security policy

This repository is public. **Do not report a vulnerability in a public issue, pull request
or discussion**: a description of how to exploit the API is readable by everyone before a
fix is deployed.

## Reporting a vulnerability

Use GitHub's private reporting: **Security → Report a vulnerability** in this repository
([direct link](https://github.com/jotazet/OtwarteDaneTransportowe-backend/security/advisories/new)).
The report is visible only to the maintainers.

Include what you can:

- the endpoint or request affected and the steps to reproduce it,
- what an attacker gains (data, an account, an action on someone's behalf),
- the date, if it matters.

A problem in the website itself belongs in the
[frontend repository](https://github.com/jotazet/otwarte-dane-transportowe-frontend/security/advisories/new);
if you are not sure which side it is, report it here.

## What to expect

- We read reports and answer in the advisory thread.
- A fix is released first; the advisory is published afterwards, with credit unless you
  prefer otherwise.

## What goes in a public issue

Hardening that describes no exploitable flaw in the running API: a missing response
header, an outdated dependency without a known exploit path here, CI and deployment
improvements.

## Supported versions

Only the version deployed in production (the latest release tag) is supported.
