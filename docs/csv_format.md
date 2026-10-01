# CSV import/export format, schema version 1

Status: normative. The parser (T20) and exporter implement exactly this document. Fixtures live in `tests/fixtures/csv/` (see its `README.md`).

Source decisions: implementation plan §2 (#2), §2.1 (time units), §2.2 (cost reporting unit), §2.3 (calendar initialisation); functional specification §12.

## 1. File-level rules

| Topic | Rule |
|---|---|
| Scope | One file describes exactly one project. |
| Encoding | UTF-8. On import an optional UTF-8 BOM (`EF BB BF`) is accepted and stripped. Any other encoding, or invalid UTF-8, fails with `CSV_BAD_ENCODING` (line 1). **Export writes UTF-8 without BOM.** |
| Syntax | RFC 4180: comma delimiter, fields containing a comma, double quote, CR or LF are wrapped in double quotes, a literal quote is doubled (`""`). Quoted fields may contain line breaks. |
| Line endings | Import accepts CRLF, LF (and a lone CR). Export writes `\n` (LF), including after the last record. |
| Header | The first line is the header row. Column names are matched case-sensitively; column order in the file is free (the exporter always writes the order in section 2). `record_type` and `schema_version` must be present, else `CSV_MISSING_HEADER`. A column not listed in section 2 is `CSV_UNKNOWN_COLUMN` (reported once, with the header line number). A known column that is absent is treated as empty in every row (hand-written narrow files are legal; the exporter always writes the full header). |
| Blank lines | Lines that are completely empty, and rows whose cells are all empty, are skipped. |
| Row width | Every row must have exactly as many cells as the header, otherwise `CSV_BAD_COLUMN_COUNT` (column `""`). |
| Line numbers | Errors report the 1-based physical line number of the line on which the record starts. The header is line 1. |
| Whitespace | On import, leading and trailing spaces and tabs are trimmed from every cell **except `name` cells, which are kept verbatim** (a required `name` that is empty after trimming counts as missing). Embedded whitespace is never altered. IDs (`id`, `parent_id`, `task_id`, `resource_id`, `pred_id`, `succ_id`) are therefore compared after trimming. Engine/UI code must not create IDs with leading or trailing whitespace or an empty ID, so export/import is lossless. |
| Case | `record_type` values, enumerations and units are matched case-sensitively, exactly as written in this document (`NODE`, `group`, `FS`, `h`). The sole exception is weekday names (section 4.3). |

## 2. Columns (one unified wide header)

**Choice: one wide header shared by all record types; cells not used by a record type are empty.** Justification: spec §12.3 asks for a single CSV with a record-type column; one header keeps the file loadable by any spreadsheet and any CSV library and keeps the parser a simple dispatch on `record_type`. Column names are unique and shared between record types only where the meaning is identical (`id`, `name`, `date`, `start`, `finish`, ...).

The exporter writes exactly these 45 columns, in this order:

```
record_type,schema_version,id,name,start_date,currency,cost_report_unit,
hours_per_day,working_days_per_year,working_weekdays,workday_start,
date,exception_kind,hourly_rate,node_kind,parent_id,sibling_order,
sizing_mode,sizing_value,sizing_unit,task_id,resource_id,percent,
pred_id,succ_id,dep_type,lag_value,lag_unit,
schedule_status,schedule_kind,start,finish,working_span_days,elapsed_span_calendar_days,
duration_days,effort_days,leveling_delay_days,assignment_days,work_qty,work_unit,
rate_per_unit,cost,cost_complete,node_status,issue_codes
```

(Written on a single line in the file; shown wrapped here.)

### 2.1 Column usage per record type

Legend: **R** required (non-empty), **O** optional (empty means "use default" or "absent"), **-** must be empty (a non-empty cell is `CSV_NOT_EMPTY`), **C** conditional (see notes). Result record types (`RESULT_*`) are export-only; their cells are never validated beyond section 6.1 rule 3.

Definition records:

| Column | PROJECT | CALENDAR | HOLIDAY | EXCEPTION | RESOURCE | NODE | ASSIGNMENT | DEPENDENCY |
|---|---|---|---|---|---|---|---|---|
| `record_type` | R | R | R | R | R | R | R | R |
| `schema_version` | R | R | R | R | R | R | R | R |
| `id` | - | - | - | - | R | R | - | R |
| `name` | R | - | O | O | R | R | - | - |
| `start_date` | R | - | - | - | - | - | - | - |
| `currency` | O | - | - | - | - | - | - | - |
| `cost_report_unit` | O | - | - | - | - | - | - | - |
| `hours_per_day` | - | O | - | - | - | - | - | - |
| `working_days_per_year` | - | O | - | - | - | - | - | - |
| `working_weekdays` | - | O | - | - | - | - | - | - |
| `workday_start` | - | O | - | - | - | - | - | - |
| `date` | - | - | R | R | - | - | - | - |
| `exception_kind` | - | - | - | R | - | - | - | - |
| `hourly_rate` | - | - | - | - | O | - | - | - |
| `node_kind` | - | - | - | - | - | R | - | - |
| `parent_id` | - | - | - | - | - | O | - | - |
| `sibling_order` | - | - | - | - | - | R | - | - |
| `sizing_mode` | - | - | - | - | - | C | - | - |
| `sizing_value` | - | - | - | - | - | C | - | - |
| `sizing_unit` | - | - | - | - | - | C | - | - |
| `task_id` | - | - | - | - | - | - | R | - |
| `resource_id` | - | - | - | - | - | - | R | - |
| `percent` | - | - | - | - | - | - | R | - |
| `pred_id` | - | - | - | - | - | - | - | R |
| `succ_id` | - | - | - | - | - | - | - | R |
| `dep_type` | - | - | - | - | - | - | - | R |
| `lag_value` | - | - | - | - | - | - | - | C |
| `lag_unit` | - | - | - | - | - | - | - | C |
| result columns (`schedule_status` ... `issue_codes`) | - | - | - | - | - | - | - | - |

Result records (export-only):

| Column | RESULT_PROJECT | RESULT_NODE | RESULT_ASSIGNMENT |
|---|---|---|---|
| `record_type`, `schema_version` | R | R | R |
| `id` | - | R (node id) | - |
| `task_id`, `resource_id` | - | - | R |
| `schedule_status` | R | - | - |
| `schedule_kind` | R | - | - |
| `start`, `finish` | O (project start/finish) | O (empty if unscheduled) | - |
| `working_span_days`, `elapsed_span_calendar_days` | O | - | - |
| `duration_days` | - | O | - |
| `effort_days` | O | O | - |
| `leveling_delay_days` | - | O | - |
| `assignment_days` | - | - | O |
| `work_qty`, `work_unit` | O | O | O |
| `rate_per_unit` | - | - | O |
| `cost` | O (project total) | O | O |
| `cost_complete` | R | R | R |
| `node_status` | - | R | - |
| `issue_codes` | - | O | - |

All other columns are empty on result records.

## 3. Record types and fields

Exactly one `PROJECT` and exactly one `CALENDAR` are mandatory. All other types may appear zero or more times, except `NODE` which must appear at least once (`CSV_MISSING_NODE`; a project with no WBS node is not importable).

### 3.1 PROJECT

| Column | Rule |
|---|---|
| `name` | Required, non-empty. |
| `start_date` | Required, `YYYY-MM-DD`. |
| `currency` | Optional. Three uppercase ASCII letters (ISO 4217 shape, not checked against a list). Default `USD`. Bad shape: `CSV_BAD_CURRENCY`. |
| `cost_report_unit` | Optional. `person_hours`, `person_days` or `person_years`. Default `person_days`. Otherwise `CSV_BAD_ENUM`. |

### 3.2 CALENDAR

All four fields optional; an omitted (empty or column-absent) field takes its plan §2.3 default and generates an informational `CSV_DEFAULT_APPLIED` note.

| Column | Default | Rule |
|---|---|---|
| `hours_per_day` | `8` | Decimal, `> 0` and `<= 24`, and a whole number of minutes (`value * 60` is an integer: `7.5` ok, `7.333` not). Violation: `CSV_OUT_OF_RANGE`. Non-numeric: `CSV_BAD_NUMBER`. |
| `working_days_per_year` | `220` | Integer, `1..366`. Non-integer text: `CSV_BAD_NUMBER`; out of range: `CSV_OUT_OF_RANGE`. |
| `working_weekdays` | `Mon;Tue;Wed;Thu;Fri` | See 4.3. At least one weekday, else `CSV_BAD_ENUM`. |
| `workday_start` | `09:00` | `HH:MM`, 24-hour (`00:00`..`23:59`), else `CSV_BAD_TIME`. |

Cross-field rule (model error): `workday_start + hours_per_day` must not exceed `24:00`, else `CSV_BAD_CALENDAR` reported on column `workday_start`. The check uses effective values (defaults included).

### 3.3 HOLIDAY

`date` required (`YYYY-MM-DD`); `name` optional (may be empty). Holiday dates must be unique among HOLIDAY records (`CSV_DUPLICATE_DATE`, on the later row). A date may not appear both as HOLIDAY and as EXCEPTION (`CSV_DUPLICATE_DATE`, on the later row in file order).

### 3.4 EXCEPTION

`date` required; `exception_kind` required, `working` or `nonworking` (`CSV_BAD_ENUM`); `name` optional. Dates unique among EXCEPTION records.

### 3.5 RESOURCE

| Column | Rule |
|---|---|
| `id` | Required, unique among RESOURCE records. Text, compared exactly. |
| `name` | Required. |
| `hourly_rate` | Optional. Empty = rate **missing** (valid; resulting costs are reported incomplete). `0` = explicit zero rate (complete cost of 0). Otherwise a non-negative decimal in the project currency per hour, no currency symbol; negative or non-numeric: `CSV_BAD_NUMBER`. |

### 3.6 NODE

| Column | Rule |
|---|---|
| `id` | Required, unique among NODE records. |
| `name` | Required. |
| `node_kind` | Required: `group`, `task` or `milestone` (`CSV_BAD_ENUM`). |
| `parent_id` | Optional; empty = top level. Otherwise the `id` of a NODE that is a `group` (`CSV_DANGLING_REFERENCE` if absent; `CSV_PARENT_NOT_GROUP` if not a group; `CSV_PARENT_CYCLE` if the parent chain loops, which includes a node naming itself). |
| `sibling_order` | Required integer (`CSV_BAD_NUMBER` otherwise; negative allowed but discouraged). Siblings (same `parent_id`) are displayed by ascending `sibling_order`; ties are broken by ascending `id` (code point order). The exporter writes the model's values unchanged. |
| `sizing_mode` | `duration` or `effort` for a `task`. Must be empty for `group` and `milestone`. |
| `sizing_value` | Decimal `>= 0` (fractions allowed: `1.5`). Empty for `group`/`milestone`. |
| `sizing_unit` | `h` (hours) or `d` (days). **Mandatory whenever `sizing_value` is present**; value without unit is `CSV_UNITLESS_TIME`; any other unit text is `CSV_BAD_UNIT`. Must be empty when `sizing_value` is empty. |

Sizing triple rule for a `task`: `sizing_mode`, `sizing_value`, `sizing_unit` are either all empty (an unsized task, which is valid; scheduling will report it as incomplete) or all present. A partial triple reports `CSV_MISSING_REQUIRED` for each missing column (a missing unit alone with a value present is the more specific `CSV_UNITLESS_TIME`). For `group`/`milestone` any non-empty sizing cell is `CSV_NOT_EMPTY`.

The value and unit are stored exactly as entered (plan §2.1); `5 d` and `40 h` stay as they are.

### 3.7 ASSIGNMENT

| Column | Rule |
|---|---|
| `task_id` | Required; must reference a NODE of kind `task` (`CSV_DANGLING_REFERENCE` if absent; `CSV_ASSIGN_NON_TASK` for group or milestone). |
| `resource_id` | Required; must reference a RESOURCE. |
| `percent` | Required plain number, `> 0` and `<= MAX_ASSIGNMENT_PERCENT` (default 100). `80` means 80 %, never `0.8` and never `80%`. Non-numeric: `CSV_BAD_NUMBER`; out of range: `CSV_OUT_OF_RANGE`. |

The pair (`task_id`, `resource_id`) must be unique: the second occurrence is `CSV_DUPLICATE_ASSIGNMENT` (column `resource_id`).

### 3.8 DEPENDENCY

| Column | Rule |
|---|---|
| `id` | Required, unique among DEPENDENCY records. |
| `pred_id`, `succ_id` | Required; each must reference a NODE (`CSV_DANGLING_REFERENCE`). Equal values: `CSV_SELF_DEPENDENCY` (column `succ_id`). Either referencing a `group`: `CSV_GROUP_DEPENDENCY` (plan §2 #5; column of the offending end, `pred_id` first). Milestones are allowed. |
| `dep_type` | `FS`, `SS`, `FF` or `SF` (`CSV_BAD_ENUM`). |
| `lag_value` | Decimal, any sign (`-0.5` is a lead). |
| `lag_unit` | `h` or `d`. |

**Zero-lag / unit rule.** `lag_value` and `lag_unit` form a pair: both present, or both empty. Both empty means a zero lag and is imported as `0 d` with a `CSV_DEFAULT_APPLIED` note. A value without a unit is `CSV_UNITLESS_TIME` (this includes `0`); a unit without a value is `CSV_MISSING_REQUIRED` on `lag_value`. The exporter **always writes both cells**, including `0` + `h` or `0` + `d`, so the as-entered unit round-trips.

Cycle rule (model error): the dependency graph among nodes must be acyclic (`CSV_CYCLE`, section 6.3).

### 3.9 Export-only result records

Result records are written only by export. On import they are **ignored** (section 6.1 rule 3). Amount columns follow plan §2.1/§2.2.

**RESULT_PROJECT** (at most one):

| Column | Content |
|---|---|
| `schedule_status` | `current`, `stale` (definition changed since the last schedule run; values are out of date) or `incomplete` (the last run could not schedule everything). |
| `schedule_kind` | `dependency_only` or `leveled`. |
| `start`, `finish` | Project start / finish datetimes (4.1). |
| `working_span_days` | Working days between project start and finish. |
| `elapsed_span_calendar_days` | Calendar days between them. Never mix with the working span. |
| `effort_days` | Total effort in person-days. |
| `work_qty`, `work_unit` | Total work in the project's `cost_report_unit` (`work_unit` is that unit's code). |
| `cost` | Project cost total, 2 decimals. |
| `cost_complete` | `true` if every assignment had a rate, else `false` (then `cost` covers only costed assignments). |

**RESULT_NODE** (one per NODE): `id` = node id; `start`, `finish`; `duration_days` (working days); `effort_days` (person-days); `leveling_delay_days` (working days; `0` when dependency-only); `work_qty`, `work_unit`, `cost`, `cost_complete`; `node_status` (`scheduled` or `unscheduled`); `issue_codes` (engine issue codes for this node, `;`-separated, empty if none).

**RESULT_ASSIGNMENT** (one per ASSIGNMENT): `task_id`, `resource_id`, `assignment_days` (person-days), `work_qty` + `work_unit` (in `cost_report_unit`), `rate_per_unit` (hourly rate times hours per unit, empty if the rate is missing), `cost` (empty if the rate is missing), `cost_complete`.

## 4. Value formats

### 4.1 Dates, datetimes, times

| Kind | Format | Notes |
|---|---|---|
| Date | `YYYY-MM-DD` | Real calendar date required (`2026-02-30` is `CSV_BAD_DATE`). Four-digit year, zero-padded. |
| Datetime (result columns only) | `YYYY-MM-DDTHH:MM` | Local project time, no timezone, no seconds. A finish at end of working period is written as that time (`2026-03-06T17:00`), per plan §2 #7. |
| Time of day | `HH:MM` | 24-hour. |

### 4.2 Numbers

Decimal point is `.`. No thousands separators, no exponent, no leading `+`, no currency symbols, no `%`. Forms like `.5` or `5.` are rejected; write `0.5` and `5`. Importers parse into exact decimals (never binary floats). The exporter writes plain decimal notation without trailing zeros beyond the entered precision for definition values, so `import(export(p))` reproduces numerically identical values.

Result precision: `*_days` columns 2 decimals (plan `DAYS_DISPLAY_DECIMALS`), `cost` 2 decimals (ROUND_HALF_EVEN, computed from unrounded parts), `work_qty` and `rate_per_unit` up to 6 decimals with trailing zeros stripped (person-years values such as `0.018182` need the extra digits).

Percent: a plain number (`80` = 80 %). Money: a plain decimal string (`3200.00`).

Booleans (result columns): `true` or `false`.

### 4.3 `working_weekdays`

Encoding: weekday abbreviations joined with `;` (semicolon, no spaces), e.g. `Mon;Tue;Wed;Thu;Fri`. Tokens: `Mon Tue Wed Thu Fri Sat Sun`.

- The exporter writes tokens in Monday-first order, capitalised as above, regardless of the week-start used by the UI (a Sunday-Thursday week is written `Mon;Tue;Wed;Thu;Sun`). Round trip depends on the set, not on any order.
- The importer is lenient on case and on spaces around tokens (`mon; TUE`), and does not care about order, but strict on content: only the seven tokens are accepted (no full names, no numbers, no ranges like `Mon-Fri`), a repeated token is an error, and an empty token (`Mon;;Tue`) is an error. All are `CSV_BAD_ENUM` on column `working_weekdays`. A cell that is empty or absent means the default.
- Delimiter `;` avoids quoting; the cell never contains a comma.

### 4.4 Identifiers

IDs are opaque text, compared exactly after whitespace trimming (section 1). `001` and `1` are different IDs; case matters (`T1` is not `t1`). Parsers must never convert IDs to numbers (spreadsheets that do so are outside the guarantee). NODE, RESOURCE and DEPENDENCY IDs are separate namespaces; the same text may be used in each.

## 5. Ordering of records

- **Import:** record order carries no meaning. References may point forward (a NODE may name a `parent_id` defined on a later line, a DEPENDENCY may precede its nodes). The only order-dependent behaviours are error attribution ("later row" in duplicate checks) and the tie rule for `sibling_order`.
- **Export:** deterministic, in this order:
  1. `PROJECT`
  2. `CALENDAR`
  3. `HOLIDAY` ascending by `date`
  4. `EXCEPTION` ascending by `date`
  5. `RESOURCE` ascending by `id` (code point order)
  6. `NODE` in WBS pre-order: depth first, parents before children, siblings by (`sibling_order`, `id`)
  7. `ASSIGNMENT` ordered by the position of `task_id` in the NODE order, then `resource_id` ascending
  8. `DEPENDENCY` ascending by `id`
  9. `RESULT_PROJECT`
  10. `RESULT_NODE` in NODE order
  11. `RESULT_ASSIGNMENT` in ASSIGNMENT order

  Exporting the same project twice yields byte-identical files (no timestamps are written).

## 6. Import behaviour and validation

### 6.1 Rules

1. The whole file is read and validated in memory before anything is replaced. If any **error** exists, nothing is replaced (spec A09) and `ImportFailed(issues)` carries every error. On success the active project definition is replaced completely: no merging, no value is ever taken from the previously open project, and all prior schedule results are dropped.
2. **Schema version.** Every non-blank row must have `schema_version` = `1`. Empty is `CSV_MISSING_REQUIRED`. On the first row (in file order) carrying any other value, the importer reports exactly one `CSV_UNKNOWN_SCHEMA_VERSION` error on that row and stops: further content cannot be interpreted. Files mixing version `1` rows with other versions are rejected the same way.
3. **Result records.** `RESULT_PROJECT`, `RESULT_NODE`, `RESULT_ASSIGNMENT` rows are not validated beyond `schema_version` and row width, and are never imported. Calculated dates in a file never override scheduling (spec §12.1). The import summary contains one info note `CSV_RESULTS_IGNORED` with the count per result record type.
4. **Omitted information.** `PROJECT` and `CALENDAR` are mandatory (`CSV_MISSING_PROJECT`, `CSV_MISSING_CALENDAR`). Optional settings that are omitted take the defaults in sections 3.1/3.2/3.8 and each application yields one info note `CSV_DEFAULT_APPLIED` (record type, line, column, applied value). The import summary lists all of them.
5. Two phases, all errors collected, none thrown early (except rule 2):
   - **Phase 1, format errors** (section 6.2): detectable from one row alone (and header).
   - **Phase 2, model errors** (section 6.3): cross-record checks. They run on all rows that passed phase 1; a check that needs a row which failed phase 1 is skipped for that row (so one bad row does not cascade). Phase 2 runs even when phase 1 reported errors, so the user sees as much as possible in one pass. The checks are implemented by (or shared with) engine validation, but are mapped to `CSV_*` codes and attributed to file lines.
6. Errors are sorted by (line, column order of section 2). A model error spanning several rows (`CSV_CYCLE`) is reported once, on the first participating row (lowest line number).

### 6.2 Error object

Each error carries: `severity` (`error`), `code`, `record_type` (of the offending row; for file-level errors the type that is missing or `""`), `line` (1-based; file-level errors use line 1, or 0 for an empty file), `column` (name, or `""` for whole-row/file errors), `message` (human text naming the offending value). Info notes use the same shape with severity `info`. Mapping to the engine `Issue` type: `object_type` = record type, `object_id` = the row's ID where it has one, `field` = column; the line number is kept in the message and in a `line` attribute.

### 6.3 Error codes

Format errors (phase 1, plus the file-level checks):

| Code | Condition |
|---|---|
| `CSV_BAD_ENCODING` | Not valid UTF-8. |
| `CSV_MISSING_HEADER` | No header row, or `record_type` / `schema_version` column missing. |
| `CSV_UNKNOWN_COLUMN` | Header names a column outside section 2. |
| `CSV_BAD_COLUMN_COUNT` | Row cell count differs from header. |
| `CSV_UNKNOWN_SCHEMA_VERSION` | `schema_version` is not `1` (rule 2). |
| `CSV_UNKNOWN_RECORD_TYPE` | `record_type` not one of the 11 defined types. |
| `CSV_MISSING_REQUIRED` | A required cell is empty (or a conditional partner is missing). |
| `CSV_NOT_EMPTY` | A cell that must be empty for this record type is not. |
| `CSV_BAD_DATE` | Not `YYYY-MM-DD` or not a real date. |
| `CSV_BAD_TIME` | Not `HH:MM` within 00:00..23:59. |
| `CSV_BAD_NUMBER` | Not a plain decimal / integer, or negative where non-negative is required (rate, sizing value), or non-integer `sibling_order`. |
| `CSV_OUT_OF_RANGE` | Numeric but outside the allowed range (`hours_per_day`, `working_days_per_year`, `percent`). |
| `CSV_BAD_UNIT` | `sizing_unit` / `lag_unit` not `h` or `d`. |
| `CSV_UNITLESS_TIME` | A `sizing_value` or `lag_value` is present without its unit. |
| `CSV_BAD_ENUM` | Value not in the allowed set (`node_kind`, `sizing_mode`, `dep_type`, `exception_kind`, `cost_report_unit`, weekdays). |
| `CSV_BAD_CURRENCY` | `currency` is not three uppercase letters. |
| `CSV_DUPLICATE_PROJECT` | Second `PROJECT` row (reported on the second, column `record_type`). |
| `CSV_DUPLICATE_CALENDAR` | Second `CALENDAR` row (same shape). |

Model errors (phase 2):

| Code | Condition | Column |
|---|---|---|
| `CSV_MISSING_PROJECT` | No `PROJECT` row (line 1). | `record_type` |
| `CSV_MISSING_CALENDAR` | No `CALENDAR` row (line 1). | `record_type` |
| `CSV_MISSING_NODE` | No `NODE` row (line 1). | `record_type` |
| `CSV_DUPLICATE_ID` | Repeated `id` within NODE, within RESOURCE or within DEPENDENCY (on the later row; message gives the first line). | `id` |
| `CSV_DUPLICATE_DATE` | Repeated date among HOLIDAY, among EXCEPTION, or one date in both. | `date` |
| `CSV_DANGLING_REFERENCE` | An ID reference matches no record of the right type. | the referencing column |
| `CSV_PARENT_NOT_GROUP` | `parent_id` names a task or milestone. | `parent_id` |
| `CSV_PARENT_CYCLE` | Parent chain loops. | `parent_id` |
| `CSV_ASSIGN_NON_TASK` | ASSIGNMENT targets a group or milestone. | `task_id` |
| `CSV_DUPLICATE_ASSIGNMENT` | Repeated (`task_id`, `resource_id`). | `resource_id` |
| `CSV_SELF_DEPENDENCY` | `pred_id` = `succ_id`. | `succ_id` |
| `CSV_GROUP_DEPENDENCY` | Dependency endpoint is a group. | `pred_id` or `succ_id` |
| `CSV_CYCLE` | Dependencies form a cycle. Message lists all node IDs on the cycle in order and the lines of its dependencies, e.g. `n2 -> n3 -> n4 -> n2 (lines 10, 11, 12)`. One error per cycle, on the first dependency line, column `pred_id`. | `pred_id` |
| `CSV_BAD_CALENDAR` | `workday_start + hours_per_day > 24:00`. | `workday_start` |

Info notes (severity `info`, never block import): `CSV_DEFAULT_APPLIED`, `CSV_RESULTS_IGNORED`.

Self-dependency and group-endpoint rows are excluded from the cycle search, so a self-dependency produces `CSV_SELF_DEPENDENCY` only.

## 7. Export semantics

1. **Complete.** Every PROJECT/CALENDAR/HOLIDAY/EXCEPTION/RESOURCE/NODE/ASSIGNMENT/DEPENDENCY is exported, whatever the UI state (collapsed groups, filters, sorting). Nodes are never skipped (spec A12).
2. **Definition fields exactly as entered:** sizing and lag values with their `h`/`d` units, calendar parameters, `currency`, `cost_report_unit`. Absent optional values are written empty, except that defaults which were applied on import are written explicitly (the exporter always writes all four calendar fields, `currency` and `cost_report_unit`).
3. **Results.**
   - If the project has never been scheduled (no result object), **no `RESULT_*` record is written**.
   - If results exist, `RESULT_PROJECT`, one `RESULT_NODE` per node and one `RESULT_ASSIGNMENT` per assignment are written.
   - `RESULT_PROJECT.schedule_status` is `current` only when the results match the present definition; `stale` when the definition changed after the last schedule run (the values written are the old ones); `incomplete` when the engine reported the schedule as incomplete. Stale takes precedence over incomplete.
   - Nodes without results (unscheduled) get a `RESULT_NODE` with `node_status` = `unscheduled`, empty start/finish.
4. **Costs** use the project's `cost_report_unit`: `work_qty`, `work_unit` and `rate_per_unit` are in that unit; `cost` is unit independent (plan §2.2). A missing rate leaves `rate_per_unit` and `cost` empty on that assignment and `cost_complete` = `false` on it, on its node and on the project.
5. **Leveling.** `schedule_kind` = `leveled` and `leveling_delay_days` carry the applied delays; a dependency-only schedule writes `0`.

## 8. Round-trip guarantee

For any valid project `p`, `import(export(p))` yields a project whose definition is identical to `p`: same project name, start date, currency, `cost_report_unit`; same calendar parameters (including `working_days_per_year`, weekday set, `workday_start`); same holidays and exceptions; same resources (including missing vs zero rates); same nodes with IDs, hierarchy, `sibling_order`, kind, sizing mode, value and **entered unit**; same assignments (with percent); same dependencies with type, lag value and **entered lag unit**. Decimal values compare numerically equal. Result records are not part of the definition; after import the project is unscheduled (stale/never scheduled) and results must be recomputed.

Consequently `export(import(export(p)))` equals `export(p)` byte for byte when `p` had no results.

## 9. Worked example

The following file is `tests/fixtures/csv/valid_with_results.csv` (shown verbatim). It has two resources, a group with an effort task of 40 h split 80 %/20 %, a milestone, one dependency, and result records for a current dependency-only schedule on an 8 h calendar with `person_days` reporting. Importing it ignores the last six records (RESULT_PROJECT 1, RESULT_NODE 3, RESULT_ASSIGNMENT 2) and reports them in one `CSV_RESULTS_IGNORED` note.

```csv
record_type,schema_version,id,name,start_date,currency,cost_report_unit,hours_per_day,working_days_per_year,working_weekdays,workday_start,date,exception_kind,hourly_rate,node_kind,parent_id,sibling_order,sizing_mode,sizing_value,sizing_unit,task_id,resource_id,percent,pred_id,succ_id,dep_type,lag_value,lag_unit,schedule_status,schedule_kind,start,finish,working_span_days,elapsed_span_calendar_days,duration_days,effort_days,leveling_delay_days,assignment_days,work_qty,work_unit,rate_per_unit,cost,cost_complete,node_status,issue_codes
PROJECT,1,,With results,2026-03-02,USD,person_days,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
CALENDAR,1,,,,,,8,220,Mon;Tue;Wed;Thu;Fri,09:00,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
RESOURCE,1,R1,Alice,,,,,,,,,,100,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
RESOURCE,1,R2,Bob,,,,,,,,,,50,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
NODE,1,G1,Phase,,,,,,,,,,,group,,1,,,,,,,,,,,,,,,,,,,,,,,,,,,,
NODE,1,T1,Implement,,,,,,,,,,,task,G1,1,effort,40,h,,,,,,,,,,,,,,,,,,,,,,,,,
NODE,1,M1,Done,,,,,,,,,,,milestone,G1,2,,,,,,,,,,,,,,,,,,,,,,,,,,,,
ASSIGNMENT,1,,,,,,,,,,,,,,,,,,,T1,R1,80,,,,,,,,,,,,,,,,,,,,,,
ASSIGNMENT,1,,,,,,,,,,,,,,,,,,,T1,R2,20,,,,,,,,,,,,,,,,,,,,,,
DEPENDENCY,1,D1,,,,,,,,,,,,,,,,,,,,,T1,M1,FS,0,d,,,,,,,,,,,,,,,,,
RESULT_PROJECT,1,,,,,,,,,,,,,,,,,,,,,,,,,,,current,dependency_only,2026-03-02T09:00,2026-03-06T17:00,5,5,,5,,,5,person_days,,3600.00,true,,
RESULT_NODE,1,G1,,,,,,,,,,,,,,,,,,,,,,,,,,,,2026-03-02T09:00,2026-03-06T17:00,,,5,5,0,,5,person_days,,3600.00,true,scheduled,
RESULT_NODE,1,T1,,,,,,,,,,,,,,,,,,,,,,,,,,,,2026-03-02T09:00,2026-03-06T17:00,,,5,5,0,,5,person_days,,3600.00,true,scheduled,
RESULT_NODE,1,M1,,,,,,,,,,,,,,,,,,,,,,,,,,,,2026-03-06T17:00,2026-03-06T17:00,,,0,0,0,,0,person_days,,0.00,true,scheduled,
RESULT_ASSIGNMENT,1,,,,,,,,,,,,,,,,,,,T1,R1,,,,,,,,,,,,,,,,4,4,person_days,800,3200.00,true,,
RESULT_ASSIGNMENT,1,,,,,,,,,,,,,,,,,,,T1,R2,,,,,,,,,,,,,,,,1,1,person_days,400,400.00,true,,
```

Reading the example: Alice at 80 % of the 5-day duration is 4 person-days (`assignment_days` 4, 32 h), rate per day = 100 x 8 = 800, cost 3200.00; Bob 1 person-day at 400, cost 400.00; project cost 3600.00.

See `tests/fixtures/csv/valid_full.csv` for every definition record type, and `tests/fixtures/csv/README.md` for the full fixture index and the expected error for each invalid file.
