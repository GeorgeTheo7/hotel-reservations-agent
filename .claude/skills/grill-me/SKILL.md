---
name: grill-me
description: Interview me about a design, one open-ended question at a time, until every decision is explicit. Use when I say "grill me".
---

# Grill me

Goal: reach a complete, shared understanding of the design before any code is written.
Start by reading `docs/BRIEF.md`. Treat it as my starting point, not as settled decisions.

## Rules
- Don't use the AskUserQuestion tool. Plain text only.
- ONE open-ended question per message, numbered (Q1, Q2, ...).
- After each question, give a **Suggested answer** with a one-line reason.
- If I say "I don't know", don't push the suggestion: present 2–3 options with pros, cons and when each fits, then let me choose.
- If a concept may be new to me, explain it in 1–3 lines before asking.
- Dig deeper on vague answers; point out conflicts with earlier answers or with the brief.
- When a topic is settled, write one line: "Decision: ... because ...".
- If I say "out of scope", record it under "Later" and move on.
- Never write or change code during grilling.
- Keep me honest about time: I have about 10 hours a week for 10 weeks. Say so when an answer does not fit.

## Order of topics
1. Goal and users: who uses it, what a good day looks like for them.
2. Scope: what is NOT in v1 (ask this early and return to it at the end).
3. Inputs and outputs: what an email looks like on the way in, what the owner sees, what the guest receives.
4. Domain data: the fictional hotel, room types, seasons, rates, minimum stays, policies.
5. Database schema: tables, keys, how availability and holds are represented, how overlaps are prevented.
6. Deterministic logic: availability and pricing in code; rounding, taxes, children, extra beds, edge cases.
7. Extraction: the structured request, required vs optional fields, what happens when a field is missing or ambiguous.
8. Tools: the list, each tool's inputs, outputs and errors; which are read-only and which change data.
9. Agent design: the graph, the loop limit, what the model is allowed to decide and what code decides.
10. State and approval: what is stored between steps, where the run pauses, what approve / edit / reject each do, what happens if nobody responds.
11. Escalation: the exact rules that send an email to the owner without a draft.
12. Guardrails: the checks in code on every draft; behaviour on prompt injection and off-topic emails.
13. Audit and monitoring: what is logged per step, what the monitoring page shows.
14. Evaluation: the test set, labels per email, metrics, dev/held-out split, how many runs per setting.
15. Retrieval for policies: chunking, search method, how it is measured.
16. MCP: what the server exposes and why it is a separate server.
17. API and inbox page.
18. Testing: what is tested without LLM or network calls.
19. Cost and latency limits; models.
20. Deployment and milestones.

## Commands
- "summary": list all decisions so far, open questions, and "Later" items.
- "write the spec": create `docs/SPEC.md`, max ~3 pages: Goal, Users, Scope (in / out), Decisions (one line each + reason), Data model (tables), Tools (table: name, reads/writes, needs approval), Graph design (a short mermaid diagram), Escalation rules, Guardrails, Evaluation plan (metrics + targets), Milestones (week by week, v1 first, small), Later.