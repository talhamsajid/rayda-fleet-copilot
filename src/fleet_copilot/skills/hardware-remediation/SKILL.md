---
name: hardware-remediation
description: Choosing between an upgrade order and a replacement for storage, RAM or battery problems, and what spec to order.
applies_to: create_upgrade_order, flag_device_for_replacement
---
# Hardware remediation

Use after detect_insights has returned storage_low, ram_constrained or battery_eol findings.

1. Start from the finding, not the request. If no detector fires for the device, do not propose anything; explain what the telemetry shows instead.
2. Pick the action the policy will accept for that model:

   | model | RAM | storage | battery |
   |---|---|---|---|
   | Dell XPS 15 | upgrade order | upgrade order | upgrade order (component battery) |
   | ThinkPad X1 Carbon | soldered: replacement | upgrade order | upgrade order (component battery) |
   | MacBook Pro / MacBook Air | soldered: replacement | soldered: replacement, or a clean-up notice | upgrade order (component battery) |
   | Mac mini | soldered: replacement | soldered: replacement, or a clean-up notice | none |

3. Prefer the cheaper fix. A battery-only problem on an otherwise healthy laptop is a battery upgrade order, not a new device. A disk with more than 14 days until full gets a clean-up notice (load employee-notification) before any hardware. Below 5% free or under 14 days to full justifies an upgrade or replacement.
4. If the administrator explicitly asks for a replacement and the policy accepts it, propose it, and mention the cheaper option in the summary.
5. Specs: RAM doubles the current size (16 GB to 32 GB). Storage goes one size up (512 GB to 1 TB, 1 TB to 2 TB). Battery is the OEM replacement for the model.
6. One device with several problems gets one replacement flag, not a flag plus part orders.
7. Put the finding's numbers in the reason or spec (free %, days to full, RAM pressure share, cycles, capacity drop).
