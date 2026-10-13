---
id: rb-backup-restore
title: Backup and Restore Runbook
type: runbook
owner: IT
ref: MF-RBK-2258
allow:
- group:it-admins
---

# Backup and Restore Runbook

## Schedule

Databases are backed up nightly at 01:00 UTC with 35-day retention. Weekly full snapshots are kept for 12 months in a separate region.

## Restore

Restore requests need a change ticket approved by the system owner. Test restores run quarterly. The target recovery time for the Transport Management System is 4 hours and the recovery point objective is 15 minutes.

_Document reference: MF-RBK-2258. Owner: IT._
