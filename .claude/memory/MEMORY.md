# Project memory index: youtube_automanager

Read this index from the top. A line with no link is the full rule.

- A new value of an enumeration column needs a hand-written Alembic revision. The revision changes the CHECK constraint and the length of the column, because the column is as long as its longest value: PostgreSQL refuses a longer value, and `compare_type` reports the old length. On SQLite, `batch_alter_table` with `drop_constraint(..., type_="check")`, `alter_column` and `create_check_constraint` copies the table and keeps its rows. `test_check_constraints_of_models_and_migrations_agree` compares the result with `create_all`. A value longer than the column fails on PostgreSQL with `DataError` before the CHECK constraint, and on SQLite with `IntegrityError` of the CHECK, so a test of such a refusal expects both.
- SQLite gives the id of the newest row again after its delete, unless the table has AUTOINCREMENT. A table whose id another table keeps without a foreign key, such as `rule` (`run_action.rule_id`) and `cookie_version`, has `sqlite_autoincrement`. Revision `c41e7a9d3b58` rebuilt `rule` with `batch_alter_table(..., recreate="always", table_kwargs={"sqlite_autoincrement": True})`. The engine of the migrations leaves the foreign keys of SQLite off, so the drop of the old table touches no row of another table, and the foreign keys of `rule_channel` and `added_video` name `rule` and act on the new table.

- [YouTube access](youtube-access.md) — before you write code that reads or writes YouTube data, or that handles the cookie session, read this file.
- [Scheduler](scheduler.md) — before you change the scheduler, or the way a sync job runs, read this file.
- [Rule conditions](rule-conditions.md) — before you change the rule format, the rule evaluator or the rule editor, read this file.
- [Sync engine](sync-engine.md) — before you change the sync engine, its processing modes, the catch-up of a rule, the preview of a rule, or the state that it keeps in the database, read this file.
- [UI and notifications](ui-and-notifications.md) — before you change the NiceGUI pages, the translations or the notification code, read this file.
