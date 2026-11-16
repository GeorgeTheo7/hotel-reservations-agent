# Reservations inbox agent
 
An agent that drafts replies to hotel guest emails; the owner approves before anything is sent or held.
Design decisions live in `docs/SPEC.md`. If the spec and the code disagree, stop and ask me.
 
## How to work with me
- I am building this to learn. When you use a concept that may be new to me, explain it in 1–3 lines.
- No code before `docs/SPEC.md` exists. After that, one milestone at a time, in small commits.
- Propose a plan and wait for my OK before changes that touch more than one file.
- Don't add a dependency without asking and giving the reason.
- Don't change the spec silently: suggest the change, I decide.
- Commit messages are short: a title, plus a body only when there is a finding worth recording. When proposing a commit, show the full message, including the Co-Authored-By line.
- While working, run only the tests for the files you changed; run the full suite once, right before proposing a commit.
- Mutation checks: 2–3 per step on what matters most, each run against its target test file only.
- Repeat race tests (--count) only when concurrency code changed.
- Reports: short by default (what changed, test result, mutations caught, decisions needed). Give details only for findings or surprises.
- When I approve a commit, push it too, and report the CI result.
- Start each session with docker compose up -d --wait.
## Rules of the design
- Prices and availability are computed in code from the database, never by the model.
- Tools that change data (hold a room, send a reply) run only after the owner's approval.
- Every draft is checked in code against tool results before it is shown.
- All data is fictional. No real guest data in the repo.
## Tests
- Unit tests make no LLM or network calls.
- Anything that calls a model is an evaluation, lives under `eval/`, and records model, settings, commit and time.
- A build step that calls a model (e.g. policy embeddings) lives in src/, goes through the LLM adapter, and records model, commit, dirty flag and time in its output.