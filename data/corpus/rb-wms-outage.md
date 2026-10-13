---
id: rb-wms-outage
title: Warehouse System Outage Runbook
type: runbook
owner: IT
ref: MF-RBK-3768
allow:
- group:it
- group:ops
---

# Warehouse System Outage Runbook

## Immediate actions

If the warehouse management system is unreachable, dispatchers switch depots to offline manifests printed at 05:00 each day. Notify the IT on-call engineer through the paging line.

## Recovery

Once service is restored, depots reconcile paper manifests within 4 hours and flag any scans that need reprocessing.

_Document reference: MF-RBK-3768. Owner: IT._
