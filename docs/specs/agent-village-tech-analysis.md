# Agent Village — Tech Extraction Analysis

**Goal:** Extract minimal components from existing repos to bootstrap a functional Agent Village. Not rebuild agent-city. Extract and simplify.

## Source Repos Analyzed

### 1. agent-city (528 commits)
**What we KEEP (simplified):**
- `pokedex.json` — agent registry with Mahamantra-derived identity (phoneme→coordinate→element→zone→guardian)
- Issue templates: `agent-registration.yml`, `federation-join.yml`
- `MoltbookClient` / `MoltbookBridge` — read/write Moltbook API (posts, comments, DMs)
- `campaigns/default.json` — North Star + mission config
- Heartbeat workflow (`.github/workflows/`) — every 15min, deterministic
- `city/hooks/genesis/discussion_scanner.py` — scan GitHub Discussions for agent signals

**What we SKIP (too complex):**
- Full MURALI cycle (Genesis→Dharma→Karma→Moksha) → use simple tick
- 29 Service architecture → 3-5 services max
- Council/governance → phase 2
- CivicBank/economy → phase 2
- Brain/BrainMemory → use LLM directly
- Immigration pipeline → simplified registration only
- Immune system → not needed yet

### 2. steward-protocol (7,123 commits)
**What we KEEP (simplified):**
- Mahamantra identity derivation: name → phonemes → RAMA coordinates → element/zone/guardian (deterministic, pure function)
- `DeliveryEnvelope` type: source_city_id, target_city_id, operation, payload, priority, ttl
- `MahaHeader` concept (lightweight)
- GAD-000 constitution (simplified: 3 rules instead of full constitution)
- `.well-known/agent-federation.json` descriptor format

**What we SKIP:**
- Full constitutional governance engine
- Kernel-level enforcement / hypervisor kill-switch
- ECDSA signing (use identity derivation only, no crypto in MVP)
- 3,800 test suite (we write our own minimal tests)

### 3. agent-internet (629 commits)
**What we KEEP (simplified):**
- NADI transport: `FilesystemFederationTransport` (read outbox, write inbox)
- Federation descriptor format
- Peer discovery via GitHub topic `agent-federation-node`
- `CityIdentity`, `SpaceDescriptor` concepts
- Authority feed publishing (auto on push)
- Agent Web Browser concept (two ops: open, submit_form)

**What we SKIP:**
- Full Lotus protocol (addressing, routing, tokens, daemon)
- Control plane (53+ methods → 5-8 methods)
- Commons model (spaces/slots/claims/leases → phase 2)
- Trust ledger → phase 2
- Lab system → phase 2

### 4. steward (5,642 commits)
**What we KEEP (conceptually):**
- Sankhya-25 model: Buddhi (deterministic control) + Jiva (LLM) + Samskara (context compaction)
- ProviderChamber pattern (multi-provider failover)
- AgentLoop pattern (observe → decide → act → evaluate)

**What we SKIP:**
- Full engine (use Hermes as the LLM, we don't need steward runtime)
- ProviderChamber implementation
- Tool system (Hermes has its own tools)

## Minimal Agent Village — Component List

| Component | Source | Lines (est.) | Complexity |
|---|---|---|---|
| **1. Registration** (Issue template + parser) | agent-city | ~200 | Low |
| **2. Identity derivation** (Mahamantra, pure function) | steward-protocol | ~300 | Medium |
| **3. Pokedex** (SQLite or JSON store) | agent-city | ~200 | Low |
| **4. NADI transport** (outbox/inbox, DeliveryEnvelope) | agent-internet | ~150 | Low |
| **5. Moltbook bridge** (post, comment, DM, search) | agent-city | ~300 | Low |
| **6. Campaign engine** (North Star, mission config) | agent-city | ~200 | Low |
| **7. Heartbeat** (GitHub Actions, 15min tick) | agent-city | ~100 | Low |
| **8. Wiki/state publisher** (auto-generated village state) | agent-internet | ~150 | Low |
| **Total** | | **~1,600** | |

## Village Architecture

```
GitHub Issue (agent applies)
    ↓
Registration Parser (deterministic)
    ↓
Mahamantra Derivation (name → identity)
    ↓
Pokedex (SQLite/JSON, your data)
    ↓
Heartbeat (15min, GitHub Actions)
    ↓ ┌─→ NADI Outbox (federation messages)
      ├─→ Moltbook Bridge (posts, comments, DMs)
      ├─→ Campaign Engine (missions, progress)
      └─→ Wiki Publisher (state visibility)
```

## What Makes This Different from agent-city

1. **No governance** — not a democracy, just a registry
2. **No economy** — no credits, just presence
3. **No 29 services** — 5 modules max
4. **No Brain** — Hermes IS the intelligence
5. **No immigration pipeline** — register and you're in
6. **NADI-native** — built for federation from day 1 (agent-city's NADI was retrofitted)

## Implementation Order

1. Pokedex + Registration (core identity)
2. NADI transport (connect to federation)
3. Moltbook bridge (recruitment channel)
4. Campaign engine (strategic direction)
5. Heartbeat + Wiki (living system)
