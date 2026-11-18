---
name: compliance-remediation
description: Writing remediation tickets for failing compliance checks (disk_encryption, screen_lock, os_up_to_date) and deciding which to open first.
applies_to: open_remediation_ticket
---
# Compliance remediation

1. Open tickets only for checks that fail on the latest snapshot. Get them in one or two calls, not one call per device: detect_insights(kind="compliance_drift") returns every device whose check started failing in the window, with failing_since and evidence ids; find_devices(failing_check=...) adds devices that failed for the whole window. Do not call get_device_history per device. One ticket per device and check, all proposals in one turn.
2. Order: high severity first (disk_encryption), then drift (a check that started failing recently usually means a configuration change), then long-standing failures.
3. Ticket note, at most 500 characters:
   "<check> failing on <device_id> (<model>, <os>) since <failing_since, or 'the whole 30-day window'>. Fix: <fix>."
4. Fixes:
   - screen_lock: re-apply the screen-lock policy (MDM profile on macOS, Group Policy or Intune on Windows) and confirm the idle lock.
   - os_up_to_date: schedule the pending OS update and confirm it installs.
   - disk_encryption: enable FileVault or BitLocker and escrow the recovery key.
5. When several devices started failing the same check on nearby dates, say in the summary that one central policy change may explain it, so the administrator can fix it once.
