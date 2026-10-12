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

- `refresh` of a `ui.refreshable` gives each argument that it gets to the refreshable function as a new argument. An `on_change` handler gets the event, so pass `lambda: view.refresh()` and not `view.refresh`.
- The service cleans the old history when it starts, and it deletes a run that is older than the retention period. A test that seeds a run with a fixed date and then starts the service would depend on the clock. So the autouse fixture `history_cleanup_off` in `tests/conftest.py` replaces `clean_history` in `youtube_automanager.web.app`, and a test of the cleanup puts the real function back with `monkeypatch.setattr`.
- The fixtures of the page tests (`user`, `settings`, `sessions`, `logging_setups`, `no_logged_errors`) are in `tests/conftest.py`, and their helpers (`running_service`, `store`, `text_of`, `set_value`, `eventually`) are in `tests/ui_support.py`. A page test module has `pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]`.
- In the user simulation, a `ui.refreshable` rebuilds a moment after the change of a value. A test polls for the new content and does not read it at once.
- In the user simulation, `user.find` sees only visible elements, so an element that `bind_visibility_from` hides is not found. `ElementFilter(marker=..., local_scope=False)` inside `with user.client:` also finds the hidden ones.
- In the user simulation, `click()` does nothing on a disabled element, `type()` adds to the value, and `clear()` sets the value to `None`. `should_not_see` returns at the first check where the element is absent, so it does not wait for a handler that runs in the background. A test that proves that a click changed nothing waits a short time first.
- In the user simulation, `set_value` with the value that the element already has calls no `on_change`. A test that waits for a rebuild after such a call waits until its timeout.
- In the user simulation, a click on a `ui.select` opens its list, and a second `user.find("<option label>").click()` chooses the option. A `ui.radio` takes `value` of any hashable key, for example a `StrEnum` member, and `element.value` returns that member.
- `ui.table` rows and columns are props, not elements. `user.find` does not see the text of a cell, and a cell slot from `add_slot("body-cell-<name>", ...)` does not render in the simulation. A test reads `table.rows` and `table.columns`, so give each cell the ready display text.
- `table.on("rowClick", handler, js_handler="(event, row) => emit(row.id)")` sends only the id of the clicked row, and `e.args` in the handler is that id and not a list. In the simulation, `user.find(marker=...).trigger("rowClick", args=<id>)` calls the handler. A real browser run confirmed it.
- `ui.table(pagination=None)` hides the paging of Quasar and shows each row, and `pagination=50` gives `table.pagination == {"rowsPerPage": 50}`. For many rows, read one page from the database and use `ui.pagination(1, pages, value=page, on_change=...)`, where the handler reads `event.value`.
- A cell of a Quasar table stays on one line with an ellipsis only when the inner `div` has the class `ellipsis` and its own `max-width`. A `max-width` on the `q-td` does not truncate the text.
- `ui.input(...).props("type=date")` gives a text `YYYY-MM-DD`, and `clearable` gives `None` after the clear. A page that parses such a text moves the date into a range that `datetime` can convert to a time zone, because a date near year 1 or 9999 fails at the conversion.
- `ui.number` gives a `float` or `None`, also with `precision=0`. A page that needs whole numbers checks `float(value).is_integer()` and converts, because the `min` and `max` props check only in the browser.
- `ui.select(options, with_input=True)` searches by typing and works with the 598 names of `zoneinfo.available_timezones()`. The stored value must be one of the options, or NiceGUI raises a `ValueError`.
- A page that reads the database with `asyncio.to_thread` after each change of a filter drops a read that ends after a newer read started. Without a counter of the reads, a slow old read replaces the newer table.

## Translations

- Babel 2.18 reads the extraction mapping from `[tool.babel]` of `pyproject.toml` when you give `-F pyproject.toml`. It writes each location with `/`, also on Windows.
- The template `messages.pot` keeps each location without a line number and sorts the messages, so a moved line does not change it. The test compares the messages and ignores the header, because the header holds the creation date, the year and the version of Babel.
- Babel copies a `# Translators:` comment into the template only when the call of `_` on the next line is not inside another call. A comment above `ui.page_title(_("..."))` does not go into the template, and a comment above `title = _("...")` does. A comment of two lines above `text = _("...").format(...)` also goes into the template.
- `pybabel extract` ends the template with an empty line, and the hook `end-of-file-fixer` removes it. The test ignores this difference.

## Apprise

- Apprise sends one message to many services. A tag on a target routes a message to a subset of the targets. A message has one of four types: info, success, warning and failure.
- Apprise 2.0.1 has no `msteams://` schema. A message to Microsoft Teams goes through the Power Automate schema `workflow://`.
- Youtarr is an example of a notification design on Apprise. It sends one summary for each run, not one message for each item. It sends to each target separately, so one failed target does not stop the others. It removes the target URLs from its logs, because a target URL holds a token.
