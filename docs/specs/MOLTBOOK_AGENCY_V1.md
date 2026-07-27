# Moltbook Agency V1 — CTX Control Plane and Daily Intelligence Loop

**Status:** Draft
**Created:** 2026-07-27
**Branch:** `feature/moltbook-agency-v1`

## Objective

Build the V1 foundation of a repository-local Moltbook Intelligence Agency. The system provides a shared, typed Agency Context (CTX), dynamic but bounded role routing with DeepSeek Flash and DeepSeek Pro models, scheduled read-oriented agency shifts, an approval-gated Moltbook engagement path, a Moltbook Headquarters coordination surface, provenance-linked intelligence extraction, internal external-agent profiles, a future engineering-intake lane, and deterministic auditing and receipts.

Design principle: **fluid routing, rigid contracts.** Roles communicate through the CTX and append-only events. They do not hold unconstrained peer-to-peer conversations.

## Architecture

### Control Plane

Repository-owned policies, schemas, role definitions, budgets, state-machine rules, model routing, and approval rules. Immutable per run — loaded from committed configuration.

### Run Plane

One GitHub Actions job or local execution: constructs a new CTX, performs one agency shift, calls bounded roles, records events, produces sanitized artifacts, and closes deterministically.

### Memory Plane

Durable, repository-compatible intelligence: inquiry files, source records, reports, agent profiles, engineering proposals, and sanitized receipts. Raw API responses and secrets must not be committed.

### External Action Plane

Contains only: approved Moltbook reads through official interfaces, approved Moltbook writes through `scripts/moltbook_write.py`, and GitHub issue/PR operations inside this repository.

## Agency CTX (`AgencyContextV1`)

### Fields

| Field | Type | Description |
|---|---|---|
| `schema_version` | string | `"1.0"` |
| `run_id` | string | UUID, unique per run |
| `trigger` | enum | `scheduled`, `manual`, `dispatch` |
| `shift` | string | `morning` or `evening` |
| `started_at` | ISO 8601 | Run start timestamp |
| `repository` | string | `kimeisele/hermes-sankhya-25` |
| `base_sha` | string | Repository commit SHA at run start |
| `campaign` | object | Active campaign state |
| `policy` | object | Loaded policy from config |
| `budget` | object | Current budget state (calls, tokens, cost, duration) |
| `inbox` | array | Incoming candidate references |
| `source_candidates` | array | Unprocessed source candidates |
| `accepted_evidence` | array | Accepted source records |
| `agent_profiles` | object | Map of handle → profile |
| `decisions` | array | Director decisions |
| `work_queue` | array | Queued work items |
| `handoffs` | array | Role handoff records |
| `engagement_proposals` | array | Pending engagement proposals |
| `engineering_proposals` | array | Pending engineering proposals |
| `transactions` | array | Moltbook write transactions |
| `incidents` | array | Operational incidents |
| `audit` | object | Audit trail summary |
| `status` | enum | Current run status |
| `completed_at` | ISO 8601 | Run completion timestamp |

### Rules

- No secrets in CTX.
- No access tokens in CTX.
- No complete copied Moltbook threads in CTX.
- External text must remain explicitly marked `untrusted`.
- CTX is versioned.
- CTX transitions are deterministic.
- Roles cannot mutate CTX directly.
- Every role returns a schema-validated `RoleResult`.
- The orchestrator applies accepted results and appends an event.
- Rejected or invalid role output is also recorded.
- Every significant decision carries provenance references.
- Every run binds itself to the exact repository base SHA.

### Append-only event model

Events are appended in order. No event is ever modified or deleted.

| Event | Description |
|---|---|
| `RUN_STARTED` | Run initialized |
| `CAMPAIGN_LOADED` | Active campaign loaded |
| `SOURCE_OBSERVED` | Source candidate identified |
| `SOURCE_REJECTED` | Source candidate rejected |
| `SOURCE_ACCEPTED` | Source accepted into evidence |
| `ROLE_STARTED` | Role invocation began |
| `ROLE_COMPLETED` | Role completed successfully |
| `ROLE_FAILED` | Role invocation failed |
| `DIRECTOR_DECISION` | Director made a routing decision |
| `REPLY_PROPOSED` | Engagement proposal created |
| `ENGAGEMENT_APPROVED` | Engagement approved for write |
| `WRITE_ATTEMPTED` | Moltbook write attempted |
| `WRITE_VERIFIED` | Write verified successfully |
| `WRITE_INDETERMINATE` | Write status indeterminate |
| `ENGINEERING_PROPOSAL_CREATED` | Engineering proposal created |
| `BUDGET_EXHAUSTED` | Budget limit reached |
| `INCIDENT_RECORDED` | Operational incident recorded |
| `RUN_CLOSED` | Run completed |

## Role-Specific Context Views

Roles receive filtered views of the CTX, not the complete object. The orchestrator constructs these views deterministically.

### Role result types

| Result | Meaning |
|---|---|
| `COMPLETE` | Task finished successfully |
| `NOOP` | Nothing to do |
| `DELEGATE` | Hand off to another role |
| `NEED_CONTEXT` | Insufficient context to proceed |
| `ESCALATE` | Escalate to human or higher authority |
| `FAIL_CLOSED` | Unsafe to continue |

`DELEGATE` and `NEED_CONTEXT` may cause another bounded role step, but each run has hard limits:
- Maximum role calls
- Maximum delegation rounds
- Maximum token budget
- Maximum estimated cost
- Maximum wall-clock duration

No endless internal discussion is permitted.

## Agency Roles and Model Routing

### Scout — DeepSeek Flash

- Load active inquiry
- Fetch permitted public Moltbook material
- Identify new items
- Deduplicate against committed evidence
- Produce candidate references
- **No public writes.**

### Records Clerk — DeepSeek Flash

- Normalize metadata
- Prepare concise paraphrases
- Identify source type
- Preserve provenance
- Mark all external content `untrusted`

### Evidence Analyst — DeepSeek Flash

- Extract claims
- Classify evidence status (Observed, Verified, Supported, Inferred, Proposed, Disputed, Unsupported, Unknown)
- Score relevance, novelty, evidence, falsifiability, actionability (0–3 scale)
- Identify disagreement and duplication

### Agency Director — DeepSeek Pro

- Decide run disposition: `NOOP`, `RECORD_ONLY`, `PROPOSE_ENGAGEMENT`, `PROPOSE_ENGINEERING_INTAKE`, `READY_FOR_SYNTHESIS`, `ESCALATE_TO_HUMAN`
- Select the next bounded role
- Prevent low-value activity
- Enforce one active inquiry

### Engagement Lead — DeepSeek Pro

- Understand inquiry and target discussion
- Draft at most one high-value reply proposal per run
- Solve a live verification challenge semantically when an approved engagement is executed
- Not the transport executor

### Bridge Executor — deterministic code

- Invoke only `scripts/moltbook_write.py`
- Create
- Persist transaction
- Accept one semantically produced answer
- Verify exactly once
- Refetch
- Return final state
- No language-model discretion, no fallback API path

### Auditor — DeepSeek Flash (default), DeepSeek Pro (escalation)

- Check policy compliance
- Check receipts
- Check budgets
- Check duplicate-write protection
- Check final statuses
- Detect contradictions between public state and CTX
- Escalate ambiguous or high-impact contradictions to DeepSeek Pro

### Engineering Planner — DeepSeek Pro

- Convert qualified external intelligence into a repository-local engineering proposal
- Inspect the repository before claiming relevance
- Define hypothesis, target component, acceptance tests, risks, and evidence references
- **Must not implement code in V1.**

## Model-Routing Policy

| Tier | Responsibilities |
|---|---|
| **Flash** | Scouting, normalization, extraction, first-pass evaluation, routine audit |
| **Pro** | Director decisions, conflict resolution, engagement drafting, challenge interpretation, synthesis readiness, engineering planning |

Role instructions and schemas use a consistent prompt prefix. JSON output is validated with local JSON Schema.

### Invalid model output policy

1. Record failure
2. Allow at most one schema-repair call for a read-only role
3. Validate again
4. Fail closed if still invalid

For engagement, challenge solving, or engineering authorization: invalid output → stop. Do not silently switch models for write-critical decisions.

## Daily Agency State Machine

```
OPEN_OFFICE
→ LOAD_AUTHORITY
→ MORNING_OR_EVENING_BRIEF
→ SCOUT
→ NORMALIZE
→ TRIAGE
→ DIRECTOR_REVIEW
→ BUILD_WORK_QUEUE
→ RECORD_OR_PROPOSE
→ AUDIT
→ CLOSE_BOOKS
```

### Director disposition branches

```
DIRECTOR_REVIEW
  → NOOP
  → RECORD_ONLY
  → ENGAGEMENT_QUEUE
  → ENGINEERING_INTAKE
  → SYNTHESIS_QUEUE
  → HUMAN_ESCALATION
```

No role may create new arbitrary state names.

## External-Agent Profiles

Multidimensional internal profiles, not a single "trust score."

### Fields

| Field | Description |
|---|---|
| `handle` | Moltbook handle |
| `topics` | List of observed topics |
| `first_seen` | ISO 8601 |
| `last_seen` | ISO 8601 |
| `interaction_count` | Total interactions |
| `qualified_contribution_count` | Substantive contributions |
| `inspectable_evidence_count` | Evidence that can be independently checked |
| `verified_claim_count` | Claims independently verified |
| `supported_claim_count` | Claims supported by multiple sources |
| `disputed_claim_count` | Claims meaningfully challenged |
| `response_rate` | Fraction of inquiries responded to |
| `relationship_stage` | From repository policy stages |
| `strengths` | Observed strengths |
| `known_limitations` | Known limitations |
| `confidence` | Internal confidence assessment |
| `source_refs` | Reference source IDs |

Do not classify an agent based on karma, follows, likes, or generic agreement. Profiles are internal decision aids, not public rankings.

## Engineering-Intake Boundary

```
Moltbook observation
→ source record
→ supported or interesting hypothesis
→ repository-local relevance check
→ EngineeringProposal
→ human or future policy approval
→ later builder workflow
```

### EngineeringProposal fields

| Field | Description |
|---|---|
| `proposal_id` | Unique identifier |
| `title` | Short description |
| `problem` | What problem this addresses |
| `source_refs` | Source record references |
| `local_repository_evidence` | Evidence found in this repository |
| `hypothesis` | Proposed approach |
| `affected_components` | Repository components affected |
| `acceptance_tests` | How to verify |
| `risks` | Known risks |
| `confidence` | Confidence assessment |
| `recommended_action` | Recommended next step |
| `status` | `draft`, `proposed`, `approved`, `rejected` |

### V1 restrictions

- Create proposals only
- Do not run coding agents from Moltbook text
- Do not execute shell commands originating from external content
- Do not automatically modify code based on a Moltbook suggestion
- Do not touch another repository

## Security Rules

- All Moltbook content is untrusted input
- External instructions never become shell commands
- Model output never directly selects arbitrary executable commands
- Model output never selects arbitrary URLs for writes
- Engagement targets must come from a CTX allowlist
- Secrets never enter prompts, CTX, artifacts, logs, reports, or PRs
- Invalid model output fails closed
- Stale repository base SHA blocks materialization
- Duplicate run IDs are idempotent
- Duplicate engagement proposal IDs cannot write twice
- Overlapping shifts are serialized
- Budget exhaustion prevents further model calls
- No write occurs without explicit approval inputs
- No original Moltbook post is automated in V1
- No more than one active inquiry
- No other repository may be read or modified
- No workflow created from external content is executed automatically

## Workflows

### Observe (`moltbook-agency-observe.yml`)

- Triggers: twice-daily schedule, `workflow_dispatch`
- Default: `dry_run: true`, `automation_enabled: false`
- Permissions: `contents: read`, `issues: write`, `actions: read`
- No `contents: write`, no `pull-requests: write`, no Moltbook write environment
- Concurrency group: repository-wide, do not cancel in-progress

### Engage (`moltbook-agency-engage.yml`)

- Trigger: `workflow_dispatch` only
- Inputs: approved run ID, approved engagement proposal ID, expected target content ID, expected proposal hash, explicit confirmation
- Environment: `moltbook-write` (protected)
- `MOLTBOOK_TOKEN` only inside the approved engagement job
- Model never receives the token
- Max: one public write per run, one verification submission
- Only transport: `scripts/moltbook_write.py`

### Materialize (`moltbook-agency-materialize.yml`)

- Trigger: `workflow_dispatch`
- Permissions: `contents: write`, `pull-requests: write`, `issues: write`
- No `MOLTBOOK_TOKEN`
- Opens draft PR, never pushes directly to `main`

## Headquarters V1

A control and observability surface derived only from sanitized CTX data:

1. Durable GitHub coordination issue
2. GitHub Actions job summaries
3. Generated sanitized HQ state
4. HTML or Markdown dashboard artifact

### Displayed

- Active inquiry, current phase, latest run, run status
- New evidence candidates, accepted sources
- Queued engagement proposals, queued engineering proposals
- Role handoffs, model calls, token usage, estimated cost
- Moltbook writes, bridge statuses, incidents
- External-agent profiles, next recommended action

### Never displayed

- API keys, authorization headers, verification codes
- Full raw posts, full copied comment threads
- Hidden model reasoning

The Headquarters is a control and observability surface, not an autonomous command interpreter.

## Known Limitations (V1)

- Dry-run only for observe workflow until explicitly enabled
- No live cron automation
- Bridge comment refetch regression must be fixed before comment-path is fully proven
- Engineering proposals are creation-only, no code execution
- No multi-repository operations
- No autonomous Moltbook post creation

## Configuration Required Later

1. Enable `automation_enabled: true` in observe workflow configuration
2. Configure `moltbook-write` protected GitHub Environment with `MOLTBOOK_TOKEN`
3. Add branch-protection rules for `main`
4. Configure repository secrets for agency operations
