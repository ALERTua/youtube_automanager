# https://github.com/casey/just
set dotenv-load
set shell := ["powershell", "-c"]
set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

# The folder of this file with forward slashes. A single-quoted string of just has no escapes, so '\\' would be two
# backslashes, and only the double-quoted "\\" is one.
project_dir := replace(justfile_directory(), "\\", "/")
# The local service keeps its database and its yt-dlp releases in the folder data next to this file. The file .env
# also configures the container, where DATABASE_URL, YTDLP_DIR and HOME point into /data, and on Windows /data is a
# folder at the root of the drive. So the local recipes set these three names themselves, and the [env] attribute of a
# recipe wins over .env. LOCAL_DATABASE_URL gives the local service another database, for example PostgreSQL.
data_dir := project_dir + "/data"
database_url := env("LOCAL_DATABASE_URL", "sqlite:///" + data_dir + "/youtube_automanager.sqlite")
ytdlp_dir := data_dir + "/yt-dlp"
image := "youtube_automanager"
# The lowest coverage of the service that "just cov" accepts, in percent, from the measured value rounded down.
coverage_limit := "98"
container := "youtube_automanager"

# Install the dependencies of the service and of the development tools
install:
    uv sync --all-groups

# Upgrade the dependencies, then run the tests before you commit the new uv.lock
upgrade:
    uv sync --all-groups --upgrade

# Start the web service with the scheduler on http://localhost:PORT, with the data in the folder data
[env("DATABASE_URL", database_url)]
[env("YTDLP_DIR", ytdlp_dir)]
[env("HOME", data_dir)]
serve PORT=env("PORT", "8080"):
    New-Item -ItemType Directory -Force "{{data_dir}}" | Out-Null; $env:PORT = "{{PORT}}"; \
    uv run python -m youtube_automanager serve

# Check the stored YouTube cookies of the local service once
[env("DATABASE_URL", database_url)]
[env("YTDLP_DIR", ytdlp_dir)]
[env("HOME", data_dir)]
check-session:
    uv run python -m youtube_automanager check-session

# Copy the subscriptions and the playlists of the account into the local database once
[env("DATABASE_URL", database_url)]
[env("YTDLP_DIR", ytdlp_dir)]
[env("HOME", data_dir)]
refresh-account:
    uv run python -m youtube_automanager refresh-account

# Print a new key for COOKIES_KEY in .env. Keep it: another key cannot read the stored cookies
key:
    uv run python -c "from youtube_automanager.youtube.session import generate_key; print(generate_key())"

# Run the tests on each core of the machine, for example: just test -k sync
test *ARGS:
    uv run pytest -q -n auto {{ARGS}}

# Run the tests in one process, for example to measure the durations: just test-serial --durations=0
test-serial *ARGS:
    uv run pytest -q {{ARGS}}

# Run the tests with the coverage report, and fail when the coverage of the service drops below the limit
cov *ARGS:
    uv run pytest -q -n auto --cov=youtube_automanager --cov-report=term-missing:skip-covered \
    --cov-fail-under={{coverage_limit}} {{ARGS}}

# Format the code, fix the lint findings and check the types
lint:
    uv run ruff format; uv run ruff check --fix; uv run ty check

# Extract the texts of the web UI into youtube_automanager/i18n/messages.pot again
pot:
    uv run pybabel extract -F pyproject.toml --add-location=file --sort-output --add-comments=Translators: \
    -o youtube_automanager/i18n/messages.pot youtube_automanager

# Update the Ukrainian catalog from messages.pot. Then translate each new message, and check each fuzzy one: the test fails on both
po:
    uv run pybabel update --init-missing --ignore-obsolete -i youtube_automanager/i18n/messages.pot \
    -d youtube_automanager/i18n/locale -D messages -l uk
    # pybabel ends the file with an empty line, and the hook end-of-file-fixer removes it. The hook exits 1 when it fixes a file.
    -uv run pre-commit run end-of-file-fixer --files youtube_automanager/i18n/locale/uk/LC_MESSAGES/messages.po

# Compile the catalogs into the .mo files that the service loads. Git ignores them, and the image build makes them too
mo:
    uv run pybabel compile --statistics -d youtube_automanager/i18n/locale -D messages

# Run each pre-commit hook on each file
pre:
    uv run pre-commit run --all-files

# Update the revs of the pre-commit hooks
pre-update:
    uv run pre-commit autoupdate

# Build the image with wslc
build:
    wslc build -t {{image}} "{{justfile_directory()}}"

# Run the image with wslc: the settings come from .env, and the data folder is mounted on /data
run PORT=env("PORT", "8080"):
    New-Item -ItemType Directory -Force "{{data_dir}}" | Out-Null; \
    wslc run -d --name {{container}} -p "{{PORT}}:8080" --env-file "{{project_dir}}/.env" \
    -v "{{data_dir}}:/data" {{image}}

# Build the image, start it on PORT with the settings of .env, wait until wslc reports it healthy, and call /health
smoke PORT="18080":
    wslc build -t {{image}}_smoke "{{justfile_directory()}}"; if ($LASTEXITCODE) { exit $LASTEXITCODE }; \
    try { \
    wslc run -d --name {{container}}_smoke -p "{{PORT}}:8080" --env-file "{{project_dir}}/.env" {{image}}_smoke | Out-Null; \
    if ($LASTEXITCODE) { throw "The container did not start" }; \
    $status = ""; \
    foreach ($try in 1..30) { \
    $status = (wslc inspect --format json {{container}}_smoke | ConvertFrom-Json).State.Health.Status; \
    Write-Host "The health status is $status"; \
    if ($status -eq "healthy") { break }; \
    Start-Sleep -Seconds 3 \
    }; \
    if ($status -ne "healthy") { wslc logs {{container}}_smoke; throw "The container is not healthy after 90 s" }; \
    Write-Host "/health answers:" (Invoke-WebRequest -UseBasicParsing "http://localhost:{{PORT}}/health").Content \
    } finally { wslc rm -f {{container}}_smoke | Out-Null }

# Stop and remove the container
stop:
    wslc rm -f {{container}}

# Show the log of the container
logs:
    wslc logs {{container}}

# Open a shell in the running container
shell:
    wslc exec -it {{container}} bash

# Show the recipes
help:
    @just --list
