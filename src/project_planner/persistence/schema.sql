-- Schema version 1 (frozen: the DDL v1 databases were created with). Later versions are
-- applied on top by migrations.py (v2: AUTOINCREMENT projects + results generation).
-- All IDs are TEXT, Decimals are exact TEXT, dates are ISO TEXT.

CREATE TABLE projects (
    pk                 INTEGER PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ('saved', 'workspace')),
    name               TEXT NOT NULL,          -- library name (saved) / project name (workspace)
    project_id         TEXT NOT NULL,          -- Project.id
    project_name       TEXT NOT NULL,          -- Project.name
    start              TEXT NOT NULL,
    currency           TEXT NOT NULL,
    cost_report_unit   TEXT NOT NULL,
    revision           INTEGER NOT NULL DEFAULT 1,
    based_on_pk        INTEGER NULL,
    based_on_revision  INTEGER NULL,
    saved_at           TEXT NULL
);
CREATE UNIQUE INDEX ux_projects_saved_name ON projects(name) WHERE kind = 'saved';
CREATE UNIQUE INDEX ux_projects_one_workspace ON projects(kind) WHERE kind = 'workspace';

CREATE TABLE calendars (
    project_pk             INTEGER PRIMARY KEY REFERENCES projects(pk) ON DELETE CASCADE,
    working_weekdays       TEXT NOT NULL,      -- 'Mon,Tue,...' Monday first
    hours_per_day          TEXT NOT NULL,
    working_days_per_year  INTEGER NOT NULL,
    workday_start          TEXT NOT NULL       -- 'HH:MM'
);

CREATE TABLE holidays (
    project_pk  INTEGER NOT NULL REFERENCES projects(pk) ON DELETE CASCADE,
    date        TEXT NOT NULL,
    name        TEXT NOT NULL,
    PRIMARY KEY (project_pk, date)
);

CREATE TABLE calendar_exceptions (
    project_pk  INTEGER NOT NULL REFERENCES projects(pk) ON DELETE CASCADE,
    date        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('working', 'nonworking')),
    name        TEXT NOT NULL,
    PRIMARY KEY (project_pk, date)
);

CREATE TABLE wbs_nodes (
    project_pk    INTEGER NOT NULL REFERENCES projects(pk) ON DELETE CASCADE,
    id            TEXT NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('group', 'task', 'milestone')),
    parent_id     TEXT NULL,                   -- no FK: a Project may hold an invalid graph
    sort_order    INTEGER NOT NULL,
    sizing_mode   TEXT NOT NULL CHECK (sizing_mode IN ('duration', 'effort', 'none')),
    sizing_value  TEXT NULL,
    sizing_unit   TEXT NULL CHECK (sizing_unit IN ('hours', 'days')),
    PRIMARY KEY (project_pk, id)
);

CREATE TABLE resources (
    project_pk   INTEGER NOT NULL REFERENCES projects(pk) ON DELETE CASCADE,
    id           TEXT NOT NULL,
    name         TEXT NOT NULL,
    hourly_rate  TEXT NULL,
    PRIMARY KEY (project_pk, id)
);

CREATE TABLE assignments (
    project_pk   INTEGER NOT NULL,
    task_id      TEXT NOT NULL,
    resource_id  TEXT NOT NULL,
    percent      TEXT NOT NULL,
    PRIMARY KEY (project_pk, task_id, resource_id),
    FOREIGN KEY (project_pk, task_id) REFERENCES wbs_nodes(project_pk, id) ON DELETE CASCADE,
    FOREIGN KEY (project_pk, resource_id) REFERENCES resources(project_pk, id) ON DELETE CASCADE
);

CREATE TABLE dependencies (
    project_pk  INTEGER NOT NULL,
    id          TEXT NOT NULL,
    pred_id     TEXT NOT NULL,
    succ_id     TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('FS', 'SS', 'FF', 'SF')),
    lag_value   TEXT NOT NULL,
    lag_unit    TEXT NOT NULL CHECK (lag_unit IN ('hours', 'days')),
    PRIMARY KEY (project_pk, id),
    FOREIGN KEY (project_pk, pred_id) REFERENCES wbs_nodes(project_pk, id) ON DELETE CASCADE,
    FOREIGN KEY (project_pk, succ_id) REFERENCES wbs_nodes(project_pk, id) ON DELETE CASCADE
);
CREATE INDEX ix_assignments_resource ON assignments(project_pk, resource_id);
CREATE INDEX ix_dependencies_pred ON dependencies(project_pk, pred_id);
CREATE INDEX ix_dependencies_succ ON dependencies(project_pk, succ_id);

CREATE TABLE schedule_runs (
    pk                    INTEGER PRIMARY KEY,
    project_pk            INTEGER NOT NULL REFERENCES projects(pk) ON DELETE CASCADE,
    kind                  TEXT NOT NULL
        CHECK (kind IN ('dependency_only', 'leveling_preview', 'leveled')),
    schedule_fp           TEXT NOT NULL,
    cost_fp               TEXT NOT NULL,
    complete              INTEGER NOT NULL CHECK (complete IN (0, 1)),
    project_finish        TEXT NULL,
    working_span_minutes  INTEGER NULL,
    created_at            TEXT NOT NULL
);
CREATE INDEX ix_schedule_runs_project ON schedule_runs(project_pk);

CREATE TABLE node_results (
    run_pk                  INTEGER NOT NULL REFERENCES schedule_runs(pk) ON DELETE CASCADE,
    node_id                 TEXT NOT NULL,
    start_minute            INTEGER NULL,
    finish_minute           INTEGER NULL,
    duration_minutes        INTEGER NULL,
    effort_person_minutes   TEXT NULL,
    leveling_delay_minutes  INTEGER NOT NULL DEFAULT 0,
    cost                    TEXT NULL,
    cost_complete           INTEGER NOT NULL CHECK (cost_complete IN (0, 1)),
    status                  TEXT NOT NULL,
    PRIMARY KEY (run_pk, node_id)
);

CREATE TABLE assignment_results (
    run_pk              INTEGER NOT NULL REFERENCES schedule_runs(pk) ON DELETE CASCADE,
    task_id             TEXT NOT NULL,
    resource_id         TEXT NOT NULL,
    assignment_minutes  TEXT NOT NULL,
    cost                TEXT NULL,
    cost_complete       INTEGER NOT NULL CHECK (cost_complete IN (0, 1)),
    PRIMARY KEY (run_pk, task_id, resource_id)
);

CREATE TABLE loading_segments (
    run_pk        INTEGER NOT NULL REFERENCES schedule_runs(pk) ON DELETE CASCADE,
    resource_id   TEXT NOT NULL,
    start_minute  INTEGER NOT NULL,
    end_minute    INTEGER NOT NULL,
    percent       TEXT NOT NULL,
    task_ids      TEXT NOT NULL                -- JSON list
);
CREATE INDEX ix_loading_segments_run ON loading_segments(run_pk);

CREATE TABLE issues (
    run_pk       INTEGER NOT NULL REFERENCES schedule_runs(pk) ON DELETE CASCADE,
    severity     TEXT NOT NULL,
    code         TEXT NOT NULL,
    message      TEXT NOT NULL,
    object_type  TEXT NULL,
    object_id    TEXT NULL,
    field        TEXT NULL,
    line         INTEGER NULL
);
CREATE INDEX ix_issues_run ON issues(run_pk);
