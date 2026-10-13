---
id: rb-vpn
title: VPN Setup and Troubleshooting
type: runbook
owner: IT
ref: MF-RBK-4331
allow:
- group:everyone
---

# VPN Setup and Troubleshooting

## Install

Install the Meridian VPN client from the Software Centre. Sign in with your corporate account and approve the MFA prompt. The client auto-connects when you are outside the office network.

## Troubleshooting

If the VPN shows 'Authentication failed', check that your system clock is correct, then re-enrol MFA. For error 809, confirm that UDP port 4500 is not blocked by the home router. If the tunnel drops every few minutes, switch the client to TCP mode in Settings.

_Document reference: MF-RBK-4331. Owner: IT._
