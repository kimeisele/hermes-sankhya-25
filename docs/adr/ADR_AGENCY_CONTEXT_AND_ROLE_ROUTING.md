# ADR: Agency Context and Role Routing Architecture

**Status:** Proposed
**Date:** 2026-07-27
**Decision ID:** ADR-001

## Context

The Hermes Sankhya-25 intelligence node needs to transition from single-operator manual reconnaissance to a structured, repository-local agency capable of bounded autonomous shifts. The system must support multiple logical roles (Scout, Records Clerk, Evidence Analyst, Agency Director, Engagement Lead, Bridge Executor, Auditor, Engineering Planner) without becoming a loose multi-agent chat system.

Two architectural questions must be resolved:

1. **How do roles share state?** Unconstrained peer-to-peer conversations lead to unbounded loops, state corruption, and untraceable decisions.
2. **How are roles routed to models?** Different tasks require different reasoning depth and cost profiles.

## Decision

### 1. Centralized Agency Context (CTX) with append-only events

Roles do not hold direct peer-to-peer conversations. They communicate exclusively through a shared, typed, schema-validated `AgencyContextV1` object.

- The CTX is versioned and schema-validated
- Roles receive **filtered views** of the CTX, not the complete object
- Roles return schema-validated `RoleResult` objects
- The orchestrator applies accepted results and appends an immutable event
- Rejected role output is recorded but not applied
- Every significant decision carries provenance references
- Every run binds to the exact repository base SHA

**Rationale:** This prevents state corruption from concurrent role mutations, enables deterministic replay, and makes every decision traceable to its provenance. The append-only event log provides a complete audit trail.

### 2. Two-tier model routing: DeepSeek Flash / DeepSeek Pro

Roles are assigned to model tiers based on task criticality:

| Tier | Roles | Rationale |
|---|---|---|
| **Flash** | Scout, Records Clerk, Evidence Analyst, Auditor (routine) | High-volume, pattern-oriented, bounded-context tasks |
| **Pro** | Agency Director, Engagement Lead, Auditor (escalation), Engineering Planner | Strategic decisions, engagement drafting, challenge interpretation, synthesis judgment |

**Rationale:** Routine scouting, normalization, and first-pass evaluation are volume tasks well-suited to Flash's cost profile. Director decisions, engagement drafting, and challenge interpretation require deeper reasoning and warrant the Pro tier. This avoids both over-provisioning (Pro for everything) and under-provisioning (Flash for write-critical decisions).

### 3. Hard bounds on role interaction

Each run enforces:
- Maximum role calls
- Maximum delegation rounds
- Maximum token budget
- Maximum estimated cost
- Maximum wall-clock duration

**Rationale:** Without hard bounds, a `DELEGATE` or `NEED_CONTEXT` cycle can become an unbounded internal discussion. The bounds guarantee termination and cost predictability.

### 4. Deterministic Bridge Executor — no model discretion

The Bridge Executor is deterministic code, not a language model. It invokes `scripts/moltbook_write.py` with fixed parameters and returns the result. It cannot choose endpoints, construct payloads, or retry.

**Rationale:** The bridge is the single point of external effect. Model discretion at this layer introduces unverifiable risk. The Engagement Lead (Pro) interprets challenges and produces answers; the Bridge Executor (deterministic) transports them. This separation ensures the model never touches credentials or transport.

## Alternatives Considered

### Peer-to-peer role communication

**Rejected.** Unbounded conversation loops, no global state visibility, no deterministic audit trail. The CTX + event model provides the same expressiveness with hard safety guarantees.

### Single-model approach

**Rejected.** No cost differentiation between routine scouting and strategic decisions. Either over-provisioned (expensive) or under-provisioned (risky).

### Model-directed bridge

**Rejected.** The model should not choose HTTP endpoints, construct API payloads, or handle credentials. This is the hardest security boundary and must be deterministic.

## Consequences

- All role implementations depend on the CTX schema — schema changes are breaking
- Role context views must be maintained alongside CTX evolution
- The orchestrator is the single integration point — its correctness is critical
- Audit trails are only as complete as the events recorded
- Flash/Pro routing is a static mapping in V1 — dynamic routing based on content complexity is a future consideration

## Related

- `docs/specs/MOLTBOOK_AGENCY_V1.md`
- `schemas/agency-context-v1.schema.json`
- `agency/orchestrator.py`
