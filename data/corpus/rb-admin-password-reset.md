---
id: rb-admin-password-reset
title: Privileged Account Password Reset Procedure
type: runbook
owner: IT Security
ref: MF-RBK-6821
allow:
- group:it-admins
---

# Privileged Account Password Reset Procedure

## Scope

This procedure covers resetting passwords for domain administrators, service accounts and break-glass accounts. It is restricted to IT administrators.

## Procedure

Two administrators must be present. Retrieve the break-glass vault key from the sealed envelope in Safe 2, which uses the combination held by the IT Security Manager. Reset the credential, rotate dependent service secrets within 1 hour, and record the change ticket.

## Audit

Every privileged reset is reviewed by IT Security within 24 hours. Unreviewed resets trigger a Sev-2 incident.

_Document reference: MF-RBK-6821. Owner: IT Security._
