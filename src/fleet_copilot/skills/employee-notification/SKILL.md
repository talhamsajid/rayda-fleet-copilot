---
name: employee-notification
description: Writing a notify_employee message - when a notice is the right action, what it may contain, and the tone.
applies_to: notify_employee
---
# Employee notification

1. Send a notice when the employee can fix the problem themselves (free up disk space, restart to finish updates, turn the screen lock back on, remove unapproved software), or when they should expect IT to contact them (repair, replacement).
2. Content: the device (model and hostname), what was observed with its number, what to do, by when, and who to contact. At most 600 characters, plain language.
3. Never include other employees' data, other devices, evidence ids or internal thresholds. No blame.
4. One message per employee: combine the issues on all their devices into one notice.
5. Template: "Hi, your <model> (<hostname>) has <issue with number>. Please <action> by <date or 'this week'>. Reply to IT if you need help."
