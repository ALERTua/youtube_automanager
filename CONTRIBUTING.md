# Contributing to YouTube AutoManager

This file tells you how to run the service from the source code, how to run the tests, and how to update the translations. For the use of the service, read the [README](README.md).

## Tools

You need [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just). The recipes run in the folder of the repository.

## Recipes

| Recipe | What it does |
|---|---|
| `just install` | Installs the dependencies of the service and of the development tools. |
| `just serve` | Starts the service on http://localhost:8080, with its data in the folder `data`. It ignores `DATABASE_URL`, `YTDLP_DIR` and `HOME` of `.env`, because these values are for the container. To use another database, set `LOCAL_DATABASE_URL`. |
| `just test` | Runs the tests on each core of the machine. |
| `just test-serial` | Runs the tests in one process, for example `just test-serial --durations=0`. |
| `just cov` | Runs the tests on each core of the machine with the coverage report, and fails when the coverage of the service drops below the limit of the justfile. |
| `just lint` | Formats the code, fixes the lint findings and checks the types with ty. |
| `just pot` | Extracts the texts of the web UI into `youtube_automanager/i18n/messages.pot` again. |
| `just po` | Updates the Ukrainian catalog `youtube_automanager/i18n/locale/uk/LC_MESSAGES/messages.po` from `messages.pot`. Translate each new message and check each fuzzy one in it: the tests fail on both. |
| `just mo` | Compiles the catalog into the `messages.mo` file that the service loads. Run it once before `just serve`, so the pages can show Ukrainian. Git ignores the `.mo` file, and the image build makes it. |
| `just pre` | Runs each pre-commit hook on each file. |
| `just smoke` | Builds the image with wslc, starts a container of it on port 18080 with the settings of `.env`, waits until wslc reports it healthy, and calls `/health`. It removes the container at the end. |

## Tests on PostgreSQL

The tests of the database, the migrations, the sync engine and the runner also run on PostgreSQL when `TEST_POSTGRES_URL` names a server, for example `postgresql://postgres@localhost:5432/postgres`. Each of these tests then runs once on SQLite and once on PostgreSQL. Each worker of a parallel run makes a template database of its own on that server, and each test gets a copy of the template. The tests of the web pages stay on SQLite. The CI workflow `Tests` runs the tests so on each pull request.

On Windows, give `127.0.0.1` instead of `localhost`, because each connection to `localhost` first waits for IPv6.
