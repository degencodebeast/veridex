# Demo Video Script — Veridex (≤5:00)

*The brief's three mandated beats: **the problem → live app walkthrough → how TxLINE powers the backend.** "Submissions will be evaluated heavily based on the demo video… make sure your demo clearly showcases the product experience, user flow, and core functionality." Target runtime 4:45; speak ~140 wpm; every [SAY] line below is timed to its window.*

---

## Pre-recording setup (do all of this BEFORE hitting record)

```bash
# Terminal 1 — the judge demo (real sealed runs + verify URLs)
cd veridex-arena && source .venv/bin/activate
python scripts/demo_phase2d.py --serve          # leave running; note the printed verify URLs

# Terminal 2 — the web app
cd veridex-arena/apps/web && pnpm dev           # http://localhost:3000
```

**Browser tabs, in order (rehearse the tab-switches):**
1. `localhost:3000/studio` (Agent Studio)
2. `localhost:3000` Cockpit (live arena view)
3. Decision Inspector (reachable from a Cockpit action — know which row you'll click)
4. Proof Card (for the demo run)
5. The `POST /runs/{id}/verify` response (pre-run it once in a REST tab or keep `curl` ready in Terminal 3)
6. Solana explorer — the two devnet txs from the README (subscription + Memo anchor)

**Ground rules while recording (honesty on-camera):**
- Mode chips (REPLAY / Backtest / Dry-run) must be **visible** — never crop them out, never call replay "live."
- Say "synthetic demo data" out loud when the demo pack is on screen — the manifest/console already label it; the narration should match.
- Don't promise anything the repo can't show. Every sentence below survives a hostile "prove it."

---

## The script

### BEAT 1 — The problem (0:00 – 0:30)

**[SCREEN: black slide or the README hero. Just the tagline.]**

**[SAY]** "Agents can trade. They can't grade themselves. Every 'my AI bot made forty percent' claim you've ever seen is unverifiable — the agent reporting the performance is the same agent being graded. Screenshots, dashboards, leaderboards: trust-me numbers. And trust-me doesn't scale to money. Veridex fixes the grading."

### BEAT 2 — What Veridex is (0:30 – 1:00)

**[SCREEN: README "What Veridex is" — the six-verb chain diagram.]**

**[SAY]** "Veridex is the proof-and-deployment layer for autonomous sports-trading agents. One chain, and no link trusts the previous one: the agent proposes — its claimed edge is untrusted metadata. A deterministic law recomputes every number from sealed evidence. Policy gates whether acting is safe. A real venue executes — and its receipts can never become proof. Anyone can re-verify the run. And a leaderboard ranks agents on recomputed closing-line value only."

### BEAT 3 — Walkthrough: configure → deploy (1:00 – 2:05)

**[SCREEN: Agent Studio. Click through: pick the Sharp Momentum template → show the config knobs → pin → Deploy (replay mode).]**

**[SAY]** "Here's the loop a user actually lives in. In Agent Studio I configure an agent from a strategy template — this is Sharp Momentum v2, a false-positive-controlled line-movement detector. Every knob you see — thresholds, warmup, market universe, risk caps — is typed, bounded, and folded into a config hash. Template plus config plus policy envelope equals a pinned agent instance."

**[ACTION: hit Deploy. Point at the returned run id.]**

**[SAY]** "Deploy runs a fail-closed preflight — invalid config, unhealthy feed, unresolvable market: each fails with a *named* reason, and no agent launches. When it passes, the instance is persisted durably with its preflight audit attached, and I get a run id back immediately — the seal happens asynchronously. This is running in replay mode, on recorded data, and the UI says so — Veridex never dresses a replay up as live."

### BEAT 4 — Walkthrough: observe — the untrusted-LLM fence (2:05 – 2:50)

**[SCREEN: Cockpit — the streaming decision trail. Click an AGENT_ACTION → Decision Inspector.]**

**[SAY]** "The Cockpit streams the full decision trail. Now the part most agent demos hide: the Decision Inspector. On one side, what the model *claimed* — fenced off, labeled 'untrusted, not an input to score.' On the other, what the deterministic law *recomputed* from sealed evidence. The agent's opinion of itself is never allowed to touch its grade — that separation is import-audited: zero LLM SDK code anywhere in the trust path."

### BEAT 5 — Walkthrough: proof card → verify (2:50 – 3:35)

**[SCREEN: Proof Card → click Verify. Show the per-check verdicts going green. Then flash the Solana explorer tab.]**

**[SAY]** "Every run seals into a proof card: an evidence hash over the sealed event prefix, seven structural checks, and a manifest anchored on Solana — here's the actual devnet transaction. And this Verify button is not a checkmark image. It re-runs the law over the sealed bytes, right now, and returns a per-check verdict. Tamper with one sealed byte, the hash breaks. Doctor a score row instead, the metrics-recomputed check catches it — we tested exactly that. You don't trust this leaderboard; you can falsify it."

### BEAT 6 — How TxLINE powers it (3:35 – 4:15)

**[SCREEN: Terminal 1 — the demo_phase2d output. Then a quick flash of the endpoints table in docs/submission.md.]**

**[SAY]** "All of it runs on TxLINE. StablePrice odds are de-margined consensus — the probabilities sum to one — which is exactly the clean fair-value input closing-line scoring needs. Live, agents ingest the SSE odds stream. For replay and backtesting, one call — odds updates by fixture — returned sixty-five thousand real updates for a single World Cup match, which we seal into content-hashed replay packs. That's why this demo is deterministic: the World Cup ends before judging, but you can re-run this exact demo, offline, and re-verify every proof yourself."

### BEAT 7 — Real results: the candidate signal + the trust moat (4:15 – 4:45)

**[SCREEN: README real-data section ("We ran it on real World Cup data. Here's the truth.") — the Run-001 / Run-002 story.]**

**[SAY]** "And here's what happened on real data — eighteen finished World Cup fixtures. Our drift agent averaged **plus sixty-one basis points of closing-line value**, beating all three deterministic baselines — a *candidate* signal, recomputed by the law, not self-reported. Then we tested whether it survived at a real venue. The Polymarket lane priced ninety-five percent of its decisions — and the estimated edge came out as a clean longshot ramp: six hundred basis points on the longshots, thirty on the favorites. That's not tradeable edge, that's a structural divergence — so Veridex called it exactly that. Estimated mids, not fills; no executable-edge claim. Most demos ask you to trust the bot. A trustworthy **'no executable edge yet'** is the product. **You can't fake a win on Veridex.**"

**[SCREEN: end card — repo URL + access link + "Agents can trade. They can't grade themselves."]**

---

## Timing summary

| Beat | Window | Content |
|---|---|---|
| 1 | 0:00–0:30 | The problem (tagline cold open) |
| 2 | 0:30–1:00 | The chain / what Veridex is |
| 3 | 1:00–2:05 | Studio: configure → preflight → deploy (replay, honestly labeled) |
| 4 | 2:05–2:50 | Cockpit + Inspector: the untrusted-LLM fence |
| 5 | 2:50–3:35 | Proof Card → Verify recompute → Solana anchor → tamper story |
| 6 | 3:35–4:15 | TxLINE: StablePrice fair value, SSE live, 65k-update history → ReplayPacks, judges-can-rerun |
| 7 | 4:15–4:45 | Run-001 candidate CLV signal (+61 bps, hook) → Run-002 longshot-ramp = no executable edge (trust moat) + kicker |

## If something breaks on camera
Don't hide it — Veridex's states are honest by design. A failed preflight shows a *named* reason; an unverified proof shows "⚠ NOT verified." Narrate it ("that's the fail-closed behavior") and retake if needed. Never edit the footage to fake a green state.
