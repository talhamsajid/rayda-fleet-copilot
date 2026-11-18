---
name: fleet-health-review
description: Producing a fleet health summary or top-risks overview - which tools to run, how to rank risks, how to report them.
applies_to: fleet_overview, detect_insights, compliance_summary
---
# Fleet health review

1. Call fleet_overview, detect_insights(kind="all") and compliance_summary once each.
2. Rank the findings:
   1. Data exposure: disk_encryption failures; unapproved remote-access or file-sharing software.
   2. Imminent failure: disk under 14 days to full; battery condition not Normal.
   3. Performance: sustained RAM pressure.
   4. Hygiene: compliance drift, outdated OS.
   5. Telemetry gaps: report as a caveat, not as a risk.
3. One finding per risk group, with its device ids and evidence ids, the count and the worst value.
4. End the summary with two or three recommended next steps in plain text. Do not create proposals unless the user asked for actions.
