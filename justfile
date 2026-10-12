# https://github.com/casey/just
set dotenv-load
set shell := ["powershell", "-c"]
set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

# The folder of this file with forward slashes. A single-quoted string of just has no escapes, so '\\' would be two
# backslashes, and only the double-quoted "\\" is one.
project_dir := replace(justfile_directory(), "\\", "/")
# The local service keeps its database and its yt-dlp releases in the folder data next to this file.
data_dir := project_dir + "/data"
database_url := env("DATABASE_URL", "sqlite:///" + data_dir + "/youtube_automanager.sqlite")
ytdlp_dir := env("YTDLP_DIR", data_dir + "/yt-dlp")
image := "youtube_automanager"
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
serve PORT=env("PORT", "8080"):
    New-Item -ItemType Directory -Force "{{data_dir}}" | Out-Null; $env:PORT = "{{PORT}}"; \
    uv run python -m youtube_automanager serve

# Check the stored YouTube cookies of the local service once
[env("DATABASE_URL", database_url)]
[env("YTDLP_DIR", ytdlp_dir)]
check-session:
    uv run python -m youtube_automanager check-session

# Copy the subscriptions and the playlists of the account into the local database once
[env("DATABASE_URL", database_url)]
[env("YTDLP_DIR", ytdlp_dir)]
refresh-account:
    uv run python -m youtube_automanager refresh-account

# Print a new key for COOKIES_KEY in .env. Keep it: another key cannot read the stored cookies
key:
    uv run python -c "from youtube_automanager.youtube.session import generate_key; print(generate_key())"

# Run the tests, for example: just test -k sync
test *ARGS:
    uv run pytest -q {{ARGS}}

# Format the code and fix the lint findings
lint:
    uv run ruff format; uv run ruff check --fix

# Extract the texts of the web UI into youtube_automanager/i18n/messages.pot again
pot:
    uv run pybabel extract -F pyproject.toml --add-location=file --sort-output --add-comments=Translators: \
    -o youtube_automanager/i18n/messages.pot youtube_automanager

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
