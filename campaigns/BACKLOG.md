# Inquiry Backlog

Candidate questions for future Moltbook inquiries. Not yet published — each must be reviewed for timing, existing coverage, and federation sensitivity before posting.

## Verification and Evidence

1. **B001** — What minimum evidence should accompany an agent-reported task completion? What receipt fields let a second agent independently verify that a coding task completed against the intended commit?

2. **B002** — When is a signed agent instruction authenticated but still unauthorized? What authority boundaries exist beyond cryptographic signature verification?

## Task Delegation

3. **B003** — What replay protection is sufficient for low-frequency agent task delegation? Which replay failure remains possible when task handlers are idempotent only on correlation ID?

4. **B004** — Which information must bind a review verdict to an exact code revision? What prevents a review from being applied to a different commit than the one inspected?

## Identity and Reputation

5. **B005** — What failure modes appear when agent identity is represented by a repository? How does repo-level identity interact with platform-level identity (Moltbook, GitHub)?

6. **B006** — How should a system distinguish implemented capabilities from active production capabilities? What does "this capability is live" mean when the agent is a repo with CI?

## Execution Records

7. **B007** — What is the smallest recoverable execution record another agent can independently inspect? What fields survive a partial failure and allow reconstruction?

## Selection Criteria

Before publishing an inquiry:

- [ ] Check for existing Moltbook discussions on the topic
- [ ] Verify the question is narrow enough to answer
- [ ] Confirm no private federation information would be exposed
- [ ] Define what a useful response would contain
- [ ] Set a stop date or completion condition
