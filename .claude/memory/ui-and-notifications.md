---
name: ui-and-notifications
description: "Read this file before you change the NiceGUI pages, the translations or the notification code."
metadata:
  type: reference
---

## NiceGUI

- `ui.run(language=...)` changes only the built-in texts of Quasar components. For the strings of the application, use `gettext` catalogs that Babel extracts.
- NiceGUI 3.17 keeps one global application. `ui.run_with(app)` mounts it at the root of `app`, and the routes that `app` already has stay first. NiceGUI starts before the lifespan of `app` and stops after it.
- With NiceGUI at the root, an unknown path, also under `/api/`, gets the HTML 404 page of NiceGUI, not the JSON 404 of FastAPI. So the API router ends with a route for `/api/{path}` that answers the JSON 404.
- In a page function, `request.app` is the NiceGUI application, not the FastAPI application of `create_app`. A page gets the services from the shell, as the argument of its builder.
- After the global application served a request, a second `ui.run_with` in the same process fails with "Cannot add middleware after an application has started". Each test that calls `mount_ui` uses the fixture `nicegui_globals`.
- At its end, `nicegui.testing.general.nicegui_reset_globals` removes the module of each page function and its parent packages from `sys.modules`. After that, a `monkeypatch.setattr` with a dotted path such as `"youtube_automanager.web.app.setup_logging"` fails with `AttributeError`. The fixture `nicegui_globals` empties `Client.page_routes` first, so the modules stay.
- The `user` fixture of NiceGUI runs only the lifespan of NiceGUI and sends each request directly to NiceGUI, so the lifespan of the service does not run and a page gets no services. A page test runs the lifespan of the whole application and gives `nicegui.testing.User` an `httpx.AsyncClient` with `httpx.ASGITransport(app)`. The async tests use the pytest plugin of anyio, not pytest-asyncio.
- In the user simulation, a click goes only to the listeners of the found element, not to its parents. Find a clickable element by its marker.
- `run.io_bound` returns `None` instead of a `CancelledError` when the call is cancelled or the application stops. A `ui.timer` whose callback awaits `run.io_bound` then survives the cancel at the stop of the service: the stop waits 2 s and logs "Could not cancel N tasks within timeout". A page reads the database with `asyncio.to_thread`.
- A `ui.timer` calls its callback at once by default. Give `immediate=False` when the page already read its data.
- NiceGUI writes `app.storage` into the folder `.nicegui` of the current folder, or into `NICEGUI_STORAGE_PATH`, and it writes a file only when a page stores a value. The image runs in `/app`, which the service user cannot write to, so a page that stores a value needs `NICEGUI_STORAGE_PATH` under `/data`. `app.storage.user` and `app.storage.browser` also need a `storage_secret`.

## Translations

- Babel 2.18 reads the extraction mapping from `[tool.babel]` of `pyproject.toml` when you give `-F pyproject.toml`. It writes each location with `/`, also on Windows.
- The template `messages.pot` keeps each location without a line number and sorts the messages, so a moved line does not change it. The test compares the messages and ignores the header, because the header holds the creation date, the year and the version of Babel.
- Babel copies a `# Translators:` comment into the template only when the call of `_` on the next line is not inside another call. A comment above `ui.page_title(_("..."))` does not go into the template, and a comment above `title = _("...")` does.
- `pybabel extract` ends the template with an empty line, and the hook `end-of-file-fixer` removes it. The test ignores this difference.

## Apprise

- Apprise sends one message to many services. A tag on a target routes a message to a subset of the targets. A message has one of four types: info, success, warning and failure.
- Apprise 2.0.1 has no `msteams://` schema. A message to Microsoft Teams goes through the Power Automate schema `workflow://`.
- Youtarr is an example of a notification design on Apprise. It sends one summary for each run, not one message for each item. It sends to each target separately, so one failed target does not stop the others. It removes the target URLs from its logs, because a target URL holds a token.
