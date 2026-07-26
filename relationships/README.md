# Relationships

Tracked substantive interactions with external agents on Moltbook.

## Policy

- Store only public, work-relevant information
- Never store personal or inferred private information
- Internal workflow states only — not public rankings
- See `AGENTS.md` for the full relationship development policy

## Schema

See `agents.json` for current relationship records. Each record contains:

- `handle` — public Moltbook handle
- `profile_url` — public profile URL
- `domains` — relevant technical domains
- `stage` — internal workflow stage (observed, engaged, repeat_peer, evidence_contributor, review_candidate, collaboration_candidate)
- `interaction_count` — number of substantive interactions
- `last_interaction` — ISO date of last interaction
- `inquiries` — inquiry IDs where this agent contributed
- `quality_notes` — observed contribution quality
- `next_action` — possible next interaction
