# Reservations inbox agent
 
An agent that drafts replies to hotel guest emails; the owner approves before anything is sent or held.
Design decisions live in `docs/SPEC.md`. If the spec and the code disagree, stop and ask me.
 
## How to work with me
- I am building this to learn. When you use a concept that may be new to me, explain it in 1–3 lines.
- No code before `docs/SPEC.md` exists. After that, one milestone at a time, in small commits.
- Propose a plan and wait for my OK before changes that touch more than one file.
- Don't add a dependency without asking and giving the reason.
- Don't change the spec silently: suggest the change, I decide.
## Rules of the design
- Prices and availability are computed in code from the database, never by the model.
- Tools that change data (hold a room, send a reply) run only after the owner's approval.
- Every draft is checked in code against tool results before it is shown.
- All data is fictional. No real guest data in the repo.
## Tests
- Unit tests make no LLM or network calls.
- Anything that calls a model is an evaluation, lives under `eval/`, and records model, settings, commit and time.