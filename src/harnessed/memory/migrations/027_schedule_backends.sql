-- 027_schedule_backends: a schedule install may be recorded by launchd and systemd
-- (ticket 319, FR-178, decision 92).
--
-- Committed. Never edited.
--
-- `schedule_installs."where"` (020) carried a CHECK admitting only `task_scheduler` and `crontab`.
-- SQLite cannot widen a CHECK in place, so the table is rebuilt as 024 was and its rows copied
-- across unchanged, `install_id` included.

CREATE TABLE schedule_installs_new (
    install_id INTEGER PRIMARY KEY AUTOINCREMENT,
    "where" TEXT NOT NULL CHECK ("where" IN ('task_scheduler', 'crontab', 'launchd', 'systemd')),
    entry_name TEXT NOT NULL,
    command TEXT NOT NULL,
    arguments TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    entry_sha256 TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    uninstalled_at TEXT
);

INSERT INTO schedule_installs_new (
    install_id, "where", entry_name, command, arguments, working_dir, entry_sha256, installed_at,
    uninstalled_at
)
SELECT
    install_id, "where", entry_name, command, arguments, working_dir, entry_sha256, installed_at,
    uninstalled_at
FROM schedule_installs;

DROP TABLE schedule_installs;

ALTER TABLE schedule_installs_new RENAME TO schedule_installs;
