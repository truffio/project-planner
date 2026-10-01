# Project Planner — Functional Specification

Version: 1.4 draft  
Date: 2026-10-01  
Status: Functional scope established. All open decisions resolved on 2026-10-01 (Section 14). No implementation included.

## 1. Purpose

Provide a locally hosted, desktop project planning tool analogous to the planning functions of Microsoft Project. Users define a work breakdown structure (WBS), task durations or effort, dependencies, resources, assignments, and a shared work calendar. The application calculates the project completion date and resource loading, identifies overloads, and offers user-requested resource leveling.

The application supports planning only. Progress tracking, actual work, and baseline comparisons are outside this version.

## 2. Agreed technology and operating environment

| Layer | Technology | Responsibility |
|---|---|---|
| Desktop interface | Python Tkinter/ttk | Editing, Gantt display, resource charts, project actions |
| Application services | Python modules | Validate requests and coordinate project operations |
| Scheduling engine | Independent Python modules | Calendar calculations, dependency scheduling, resource loading, leveling |
| Persistence | SQLite | Store complete project data and calculated results |

The application shall run locally as a desktop application with a Tkinter user interface. It shall require only a standard Python installation or a packaged executable, and no web browser, web server, Node.js, npm, or frontend build step. The Gantt and resource charts shall be implemented with native Tkinter widgets (Canvas). The user interface shall call scheduling and persistence functions through an application-service layer and shall not contain scheduling rules. Scheduling rules shall reside in independent Python modules that do not depend on the user interface. Long-running operations shall not block the interface. Public hosting, authentication, and simultaneous multi-user editing are outside the initial scope.

## 3. Project lifecycle

### 3.1 Project definition

A project shall contain a name, a project start date, one shared work calendar, a WBS, resource definitions with hourly rates, assignments, dependencies, calculated schedule information, and estimated labor costs.

The user shall be able to create a project, edit its definition, calculate its schedule, request leveling, save work, and load saved work.

### 3.2 Save and load

Saving shall preserve the complete project definition, resource hourly rates, calculated costs, calculated dates, schedule status, and any applied leveling delays. Loading shall restore these values without silently rebuilding a different schedule.

The interface shall indicate whether there are unsaved changes. Loading another project, creating a new project, or importing a replacement CSV shall provide an opportunity to save or discard unsaved work. Failed save or load operations shall leave the current working project intact and show an actionable error.

**Storage workflow (agreed):**
- Multiple named projects shall be stored in SQLite, with New, Save, Save As, Load, and Delete actions and a project picker.
- Save As shall create a new named project from the current working project. The new project then becomes the active one.

## 4. Work breakdown structure

### 4.1 Node types

| Type | Meaning | Scheduling behavior |
|---|---|---|
| Group / summary | Organizes child groups, tasks, and milestones | Dates derived from scheduled descendants |
| Task | Work with a user-defined duration or effort | Scheduled using calendar and dependencies |
| Milestone | A zero-duration project event | Has one scheduled instant and consumes no resource capacity |

The WBS shall support nested groups. Users shall be able to add, rename, edit, move, reorder, and delete nodes through the interface. Deleting nodes with descendants or dependency references shall identify the affected items before the deletion is committed.

Each node shall have a stable unique ID, a name, a node type, a parent reference, and a sibling order. IDs shall remain stable when names or positions change. The display WBS number may change when the hierarchy changes.

There shall be no imposed task-count limit. This means that the application shall not enforce an arbitrary fixed maximum; it does not imply unlimited memory or instantaneous calculation. Large WBS views shall avoid requiring every row to be rendered at once.

Performance targets are measured on representative projects of 1,000, 10,000, and 50,000 tasks:
- Scheduling a 10,000-task project shall take under 2 seconds.
- Leveling a 10,000-task project shall take under 30 seconds.
- Results for the 1,000- and 50,000-task sizes shall be measured and reported.

### 4.2 Summary information

Group start shall be the earliest scheduled start of its descendants; group finish shall be their latest scheduled finish. Group elapsed span shall not be calculated by adding child durations, because children can overlap. Group effort shall aggregate leaf task effort without counting nested summaries twice.

Dependencies between tasks and milestones are required. In this version, dependencies shall not attach to summary groups. Attempting to create one shall produce a validation error identifying the group.

## 5. Task sizing and resource assignments

### 5.1 Task properties

Each task shall provide a name, stable ID, WBS parent, sizing mode, sizing value and unit, zero or more assignments, and zero or more dependencies. Calculated fields shall include start, finish, working duration, total effort, and relevant validation or overload status.

**Time units.**
- Task duration, task effort, and dependency offsets shall be entered as a value with a unit of either hours or days. Days are converted to working time using the shared calendar's working hours per day.
- The entered value and unit are the user's choice. They shall be preserved as entered and shall not be changed by calculation. Effort expressed in days means person-days.
- Planning calculation outputs (working durations, effort, assignment work, offsets, leveling delays, and project working span) shall be reported in working days, with fractional days permitted. The elapsed calendar span shall be reported in calendar days and labelled distinctly.
- Cost reporting uses a separate, user-selectable work unit (Section 5.5).

### 5.2 Duration-based tasks

The user defines the working duration. Changing assignments shall not change that duration. Assignments determine calculated effort and loading.

For a task with working duration D hours and assignment fractions a1 through an:

`total_effort_hours = D × sum(assignment_fractions)`

An unassigned duration-based task can still be scheduled and contributes no resource loading.

### 5.3 Effort-based tasks

The user defines total person-hours of effort. Duration shall be calculated from the sum of the user-entered allocation fractions:

`working_duration_hours = total_effort_hours / sum(assignment_fractions)`

Example: 40 person-hours, one person at 80%, another at 20%, and an eight-hour working day result in five working days. Their contributions are 32 and 8 person-hours respectively.

An effort-based task without positive assigned capacity shall be marked unschedulable with an explanation. The application shall not invent assignments or assume one full-time person.

### 5.4 Assignment rules

A task may have multiple assigned people. Each assignment shall have its own user-entered percentage, which shall remain constant throughout the task. Different people do not need equal percentages. All assignments are active over the same scheduled task interval during working time.

The scheduler shall not redistribute effort, alter percentages, or assume interchangeable people. The same person shall not be represented twice within one task; editing shall update that person's existing assignment.

An individual assignment percentage shall be greater than 0% and no greater than a configurable maximum, which defaults to 100%. Total resource loading across tasks shall be allowed to exceed 100% and shall remain visible.

### 5.5 Resource rates and project cost

Each resource shall have a user-editable hourly rate. Estimated labor cost shall be calculated from that resource's assigned working hours, including its allocation percentage:

`assignment_hours = task_working_duration_hours × assignment_fraction`

`assignment_cost = assignment_hours × resource_hourly_rate`

`task_cost = sum(assignment_cost for all task assignments)`

For a task with working duration D, each resource r contributes D × its own allocation fraction hours, at its own hourly rate. The task cost shall therefore be:

`task_cost = D × sum(allocation_fraction_r × hourly_rate_r)`

The application shall calculate each contribution individually before summing. It shall not divide hours equally among resources or apply an unweighted average rate. Task details shall show each resource’s percentage, assigned work, rate, and calculated cost so the total can be checked.

Costs shall be calculated in the rate's native unit, from assigned hours and hourly rates. Cost reports shall present assigned work, and the corresponding rate, in a reporting unit chosen by the user: person-hours (man-hours), person-days (man-days), or person-years (man-years).
- Person-days use the calendar's working hours per day.
- Person-years use the calendar's working days per year, which the user configures at calendar initialisation (Section 6).
- The default reporting unit is person-days, set per project. Each report may select a different unit.
- The reporting unit shall change only how work and rates are presented. It shall not change calculated cost amounts, dates, or the user's entered task units.

Group cost shall sum the costs of descendant leaf tasks without counting summary groups twice. Project cost shall sum all leaf task costs. Milestones shall have zero labor cost.

For an effort-based task, assignment hours shall use the calculated duration and the user-entered percentages. Example: a 40-hour task with Alice at 80% and $100/hour and Bob at 20% and $50/hour takes 40 working hours, assigns 32 hours to Alice and 8 to Bob, and costs $3,600. For a duration-based task, changing allocation shall change its effort and cost while preserving duration.

Delaying a task through leveling shall not change its labor cost when duration, assignments, and rates remain unchanged. Nonworking days and dependency lag shall not themselves incur labor cost. Overlapping assignments shall each contribute their own assigned hours and cost, even when the person is overloaded.

The interface shall display resource rates, assigned work and costs in task details, task and group costs in the WBS, and total project cost. Rate edits shall invalidate or recalculate affected costs without changing schedule dates. Missing rates shall be flagged; an incomplete estimate shall not be presented as a complete project total. An explicit zero rate shall be valid.

**Currency and rounding (agreed):**
- Each project shall have one currency, with a default code of USD, and no exchange-rate conversion. Rates shall be nonnegative.
- Monetary calculations shall use exact decimal arithmetic at full precision.
- Displayed and exported amounts shall be rounded to 2 decimals using round-half-to-even. Totals shall be computed from unrounded contributions, not by summing rounded values.
- Rates apply uniformly across each resource's assignments. Overtime premiums, date-dependent rates, and task-specific rate overrides are outside this version.

## 6. Shared work calendar

One calendar shall apply to every resource and task. Users shall define recurring working weekdays, working hours per day, holidays, and date exceptions such as an exceptional working day or nonworking day.

**Calendar initialisation.** At the start of a project, the user shall configure the calendar parameters through a single calendar initialisation function. These parameters are project data, not fixed constants. They shall be saved with the project and included in CSV import and export.

| Parameter | Default | Rule |
|---|---|---|
| Working hours per day | 8 | Greater than 0 and no more than 24 |
| Working days per year | 220 | Whole number greater than 0 and no more than 366; used for person-year cost reporting |
| Working weekdays | Monday–Friday | At least one weekday |
| Workday start time | 09:00 | Start time plus working hours per day shall not pass midnight. Used to display clock times; the working day is one continuous period. |
| Holidays | None | Unique dates |
| Date exceptions | None | Unique dates, each marked working or nonworking |

Initialisation rules:
- Any parameter the user omits takes its default.
- All parameters shall be validated together. If any is invalid, none shall be applied, and the error shall identify each invalid parameter.

Calendar parameters may be changed later through calendar editing:
- A change to working hours per day, weekdays, start time, holidays, or exceptions shall mark the schedule stale. Under Section 5.1, tasks keep their entered value and unit.
- A change to working days per year affects only person-year cost reporting and shall not mark the schedule stale.

Task work and dependency offsets shall count working time only. Nonworking dates shall contribute no task progress or resource capacity. A continuous task may span a weekend or holiday; these calendar pauses do not constitute task splitting.

The calendar does not require separate daily working intervals, individual resource calendars, or individual leave calendars in this version.

The scheduling engine shall support fractional working days rather than rounding every task to whole days. Under an eight-hour calendar, 12 working hours equal 1.5 working days. Calendar calculations shall use a consistent start/finish boundary convention so a finish-to-start successor with zero lag can begin immediately when its predecessor finishes.

**Precision and display (agreed):**
- Working time shall be calculated at one-minute precision.
- A duration derived from effort shall be rounded up to the next whole working minute.
- Task intervals run from their start up to, but not including, their finish.
- Day values shall be calculated exactly and displayed to 2 decimal places.
- A finish that falls on a day boundary shall be displayed as the end of the last working period (for example, "Monday 17:00"), not as the start of the next working day.

If the selected project start falls on a nonworking day, scheduling shall begin at the next working boundary and the user shall be informed.

## 7. Dependencies

A task or milestone shall support multiple predecessors and successors. Each dependency shall reference stable endpoint IDs, one relationship type, and a signed working-time offset. Users shall add, edit, and remove dependencies through the user interface.

| Relationship | Zero-offset constraint |
|---|---|
| Finish-to-start (FS) | Successor start is no earlier than predecessor finish |
| Start-to-start (SS) | Successor start is no earlier than predecessor start |
| Finish-to-finish (FF) | Successor finish is no earlier than predecessor finish |
| Start-to-finish (SF) | Successor finish is no earlier than predecessor start |

Positive offsets postpone the applicable successor boundary. Negative offsets allow that boundary to occur earlier relative to the predecessor boundary. Offsets are evaluated using the shared work calendar.

All incoming constraints shall be satisfied together. The application shall not treat dependency relationships as mandatory equality or start a successor before the project start merely because it has negative lag.

Invalid endpoint references, self-dependencies, and dependency cycles shall produce explicit errors. All dependency cycles shall be rejected in this version, even if a particular mathematical cycle might admit a solution. The error shall identify every node in each cycle.

## 8. Schedule calculation

The application shall calculate a forward schedule from the project start date. It shall derive each task's working duration, apply the calendar, and find dates satisfying all dependencies. Before leveling, dates shall be calculated without restricting resource use to 100%.

The result shall include:

- Start and finish for each schedulable task and milestone.
- Derived dates and effort for summary groups.
- Project completion date: latest finish among scheduled tasks and milestones.
- Project working span and elapsed calendar span, clearly distinguished.
- Resource loading and overload intervals.
- Estimated assignment, task, group, and total project labor cost, with estimate completeness status.
- Errors for items that cannot be scheduled.

The engine shall not report a project completion date as valid if required tasks remain unscheduled. A partial result may be shown, but shall be identified as incomplete.

Changes to calendar, task sizing, dependencies, assignments, or project start shall invalidate affected calculated results. Stale results shall be marked clearly and shall not be represented as current in exports.

Schedule recalculation shall happen only when the user explicitly requests it, through a Calculate action. Until then, edits mark the affected results stale. Rate edits are the exception: they change costs only, so affected costs shall be recalculated immediately, without changing dates.

Fixed dates, deadlines, backward scheduling, and additional date constraints are not established requirements for this version.

## 9. Resource loading

For a resource at a particular working instant:

`loading_percent = sum(allocation_percent of that resource's active tasks)`

Resource loading shall reflect the actual overlapping assignments. It shall not be capped at 100% or averaged in a way that conceals brief overloads.

Example: two overlapping tasks assigning the same person at 80% and 50% produce 130% loading. The displayed value remains 130%, and the affected interval is flagged.

Users shall be able to inspect each person's loading over time and identify the tasks contributing to an overload. The display shall include a 100% capacity reference. Daily or weekly summaries may be offered, but must distinguish average loading, peak loading, and assigned work (in person-days). Nonworking periods shall not be treated as available capacity.

## 10. User-requested resource leveling

Leveling shall run only when the user requests it. It shall resolve overloads by delaying whole tasks while preserving:

- User-entered effort or duration.
- Assigned people and their allocation percentages.
- Task continuity across working time.
- All dependency constraints and the project start bound.

Leveling shall not split tasks, reduce percentages, remove assignments, or automatically substitute people. Calendar weekends and holidays remain ordinary nonworking pauses.

Delaying a task may require delaying related tasks to preserve FS, SS, FF, or SF constraints. The resulting project finish may move later; it shall be recalculated and displayed.

The operation shall report delayed tasks, the added delay, the resulting project finish, and unresolved overloads. If a task's own assignment exceeds a person's permitted capacity, shifting that task cannot resolve the overload; the application shall explain this condition rather than continue indefinitely.

**Interaction (agreed):**
- Leveling shall first produce a preview, which the user may Apply or Discard.
- The user shall also be able to return to the dependency-only schedule.

**Ordering (agreed):**
- Tasks shall be placed one at a time in dependency order.
- When several tasks are ready, priority goes to the earliest dependency-only start, then WBS display order, then node ID.
- Each task is placed at the earliest start that respects its dependencies and keeps every assigned person within capacity.

No optimality or minimum-project-duration guarantee is implied.

Edits that invalidate a leveled schedule shall mark it stale. Leveling shall not silently rerun after edits.

## 11. User interface

### 11.1 Main planning view

The main view shall contain an editable, collapsible WBS table on the left, an aligned read-only Gantt on the right, and a resource loading view below or in an adjacent tab.

The WBS table shall expose task name, sizing mode and value, calculated duration, effort, start, finish, assignments, estimated cost, and status. Task selection shall open a properties editor for assignments and dependencies. Table and Gantt shall share visible row ordering, hierarchy expansion state, and vertical scrolling.

### 11.2 Gantt

The Gantt shall show task bars, milestones, summary spans, and nonworking dates. It shall support horizontal scrolling, timeline zoom, and selecting an item to inspect it. Dependency connections shall be available for inspection, with a way to reduce visual clutter for large projects.

Gantt bars shall not be draggable or resizable. Dates are schedule outputs rather than values edited through the Gantt.

### 11.3 Additional editors and actions

The interface shall provide project settings, calendar editing, resource management, task properties, dependency editing, save/load, CSV import/export, scheduling, and leveling. Validation errors shall identify the affected field or object. Long operations shall display progress or a busy state without freezing the interface. Closing the application window shall offer the same save/discard opportunity as loading another project.

## 12. CSV import and export

### 12.1 Replacement import

Import shall reset the entire active project definition and replace it with the definition represented by the CSV. It shall not merge with the existing WBS or retain previous resources, assignments, dependencies, calendar exceptions, or schedule results.

The full file shall be parsed and validated before replacement is committed. Failed import shall preserve the existing project. The user shall see a replacement summary and the unsaved-work handling described in Section 3.

Dependencies may be supplied by the import format or entered afterward through the user interface. Calculated dates in a CSV shall not override scheduling rules when imported; saved-project loading is the mechanism for restoring an exact working state.

### 12.2 Export

Export shall include every WBS node, including collapsed or filtered-out items, and the available schedule information. It shall include stable IDs, hierarchy, sizing, assignments, dependencies and offsets, calculated start/finish, duration, effort, resource hourly rates, assignment costs, task/group/project costs, cost completeness status, leveling delay, and schedule status. If scheduling is incomplete or stale, the export shall identify that condition explicitly.

### 12.3 Format definition

The format shall be a single CSV file with a record-type column, plus a schema version.
- **Record types:** project, calendar, holiday, calendar exception, resource, WBS node, assignment, and dependency. Export adds result records.
- **Multiple values per task:** each assignment and each dependency is a separate record, so a task can have several of each without ambiguity.

Policy for information omitted from the CSV:
- The project and calendar records are mandatory. Holidays, exceptions, resources, assignments, and dependencies are optional.
- The calendar record carries the Section 6 initialisation parameters. Omitted calendar parameters, and the optional project settings (currency and cost reporting unit), take their documented defaults. The import summary shall list every default it applied.
- Missing information shall never be taken silently from the previous project.

The following shall be documented in an import template, which is a design deliverable of implementation:
- UTF-8 encoding; ISO 8601 dates; a decimal point for decimal values.
- Time units: each time value has its own unit column, `h` or `d`.
- Nested-field representation, and the schema version.

Identifiers shall be preserved as text so values such as `001` retain their identity.

## 13. Acceptance scenarios

| ID | Scenario | Expected behavior |
|---|---|---|
| A01 | 40-hour effort task, allocations 80% and 20%, eight-hour days | Five working days; individual efforts 4 and 1 person-days (32 and 8 hours) |
| A02 | Five-day duration task; allocations change from 50% to 100% | Duration remains five days; effort and loading change |
| A03 | Same person assigned 80% and 50% to overlapping tasks | 130% shown and overlap flagged |
| A04 | Working duration crosses a holiday | No work or capacity counted on the holiday; finish moves accordingly |
| A05 | Each dependency type with positive and negative lag | Applicable start/finish constraint honored in working time |
| A06 | Task has several predecessors | All constraints honored simultaneously |
| A07 | Leveling requested for resolvable overload | Whole tasks delayed; assignments preserved; dependencies still valid |
| A08 | Weekend lies inside a leveled task | Calendar pause allowed; no additional work interruption introduced |
| A09 | Invalid replacement CSV | Existing project remains intact; row/field errors shown |
| A10 | Valid replacement CSV | Previous project contents removed; imported definition becomes active |
| A11 | Save, close, and load | Definition and saved schedule restored, including leveling delays |
| A12 | Export while a WBS group is collapsed | All nodes exported, including hidden descendants |
| A13 | Effort task has no positive allocation | Task unschedulable; project result marked incomplete |
| A14 | Dependency cycle or dangling reference | Clear validation error; no valid schedule claimed |
| A15 | Edit an input after scheduling | Old results marked stale or replaced by a new calculation |
| A16 | 40-hour effort task; Alice 80% at $100/hour, Bob 20% at $50/hour | Assignment costs $3,200 and $400; task cost $3,600 |
| A17 | Nested WBS with several costed tasks | Group and project costs sum leaf tasks once |
| A18 | Resource hourly rate changes | Affected costs update or become stale; dates unchanged |
| A19 | Leveling delays tasks without changing work or assignments | Project labor cost unchanged |
| A20 | Assigned resource has no hourly rate | Missing rate identified; total estimate marked incomplete |
| A21 | 16-hour duration task; Alice 75% at $100/hour, Bob 25% at $60/hour | Alice contributes 12 hours/$1,200; Bob 4 hours/$240; task total $1,440 |
| A22 | Launch on a Windows laptop with a standard Python installation, or from the packaged executable | Application window opens without installing Node.js, npm, a browser, or any frontend build step |
| A23 | Leveling or scheduling a large project | Interface stays responsive, shows progress, and permits cancellation; a cancelled run applies no partial result |
| A24 | Same task entered as 40 hours and as 5 days, eight-hour days | Identical schedule results, reported in days; each task keeps its entered value and unit |
| A25 | Cost report unit switched between person-hours, person-days, and person-years | Work and rate shown in the chosen unit; cost amounts and dates unchanged |
| A26 | Calendar initialised with 7.5 hours per day and 250 working days per year | A 5-day task lasts 37.5 working hours; person-year reporting uses 250 days; settings survive save/load and CSV round-trip |
| A27 | Calendar initialisation with an invalid parameter combination | Nothing applied; each invalid parameter identified |

## 14. Open decisions and exclusions

### Decisions resolved on 2026-10-01

| # | Decision | Resolution | Section |
|---|---|---|---|
| 1 | Leveling priority, tie-breaking, preview/apply, reset | Placement in dependency order; ties broken by earliest dependency-only start, then WBS order, then ID. Preview with Apply/Discard; reset to the dependency-only schedule. | 10 |
| 2 | CSV schema and omitted settings | Single record-type CSV. Project and calendar records mandatory; documented defaults for optional settings. The detailed template is an implementation deliverable. | 12.3 |
| 3 | Named project storage and Save As | Multiple named projects; New, Save, Save As, Load, Delete; project picker. | 3.2 |
| 4 | Individual allocation range | Greater than 0%, configurable maximum defaulting to 100%. | 5.4 |
| 5 | Dependencies on summary groups | Not permitted in this version. | 4.2 |
| 6 | Immediate vs. explicit recalculation | Explicit Calculate action; rate-only edits recalculate costs immediately. | 8 |
| 7 | Precision, boundary display, rounding, time units | One-minute precision; effort-derived durations rounded up; days displayed to 2 decimals; finish shown as end of working period. Inputs in hours or days, preserved as entered; outputs in days. | 5.1, 6 |
| 8 | Performance targets and test sizes | 1k / 10k / 50k tasks; schedule 10k under 2 s; level 10k under 30 s. | 4.1 |
| 9 | Currency and monetary rounding | One currency per project (default USD); exact decimal arithmetic; round-half-to-even to 2 decimals for display and export. | 5.5 |
| 10 | Cost reporting unit | User-selectable person-hours, person-days, or person-years; default person-days; amounts unchanged by unit. | 5.5 |
| 11 | Working days per year, hours per day, and similar calendar parameters | User-configurable at project start through calendar initialisation; defaults 220 days/year, 8 hours/day, Mon–Fri, 09:00 start; editable later. | 6 |

### Outside the initial scope

Critical path display, actual-work and progress tracking, baselines, budget targets and nonlabor costs, per-resource calendars, task splitting, automatic leveling after edits, editable Gantt bars, public deployment, simultaneous multi-user collaboration, and a browser-based interface (a later version may add one over the same application-service layer) are outside the initial scope. Additional date constraints and backward scheduling require a separate scope decision.

This specification defines intended functionality. It does not select a scheduling package or imply that any third-party package already implements the agreed rules.
