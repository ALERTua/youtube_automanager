---
name: ui-and-notifications
description: "Read this file before you change the NiceGUI pages, the translations or the notification code."
metadata:
  type: reference
---

## NiceGUI

- `ui.run(language=...)` changes only the built-in texts of Quasar components. For the strings of the application, use `gettext` catalogs that Babel extracts.
- `run.io_bound` runs a blocking call off the event loop.

## Apprise

- Apprise sends one message to many services. A tag on a target routes a message to a subset of the targets. A message has one of four types: info, success, warning and failure.
- Apprise 2.0.1 has no `msteams://` schema. A message to Microsoft Teams goes through the Power Automate schema `workflow://`.
- Youtarr is an example of a notification design on Apprise. It sends one summary for each run, not one message for each item. It sends to each target separately, so one failed target does not stop the others. It removes the target URLs from its logs, because a target URL holds a token.
