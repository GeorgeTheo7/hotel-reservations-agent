# Brief: Reservations inbox agent

## Goal
An agent for a small (fictional) Greek island hotel. It reads guest emails, works out what the guest wants,
checks rooms and prices, looks up hotel policies and drafts a reply in the guest's language.
The owner approves, edits or rejects each draft. Only after approval is the reply sent and the room held.

## User
The hotel owner or receptionist, who answers dozens of emails a day in several languages.

## Design rules
1. The model never calculates a price or decides availability. Code does, from the database.
2. Nothing that changes data (send reply, hold room) happens without the owner's approval.
3. Every draft is checked in code before the owner sees it: prices and dates must match tool results.
4. The agent escalates instead of answering: complaints, discount requests, groups, missing information,
   a policy it cannot find.

## Planned pieces
- Postgres: room types, rooms, seasonal rates, minimum stays, reservations, holds, emails, drafts, audit log.
- 6–8 short policy documents (cancellation, pets, children and extra beds, check-in, deposit, breakfast, transfers).
- Read tools: check availability, quote price, search policies, find reservation, suggest alternatives.
- Write tools (approval needed): hold a room, send the reply.
- An MCP server exposing the reservation system (availability, quote, hold).
- LangGraph agent with state stored in Postgres, pausing for approval.
- Inbox page: emails, draft, steps taken, approve / edit / reject.
- Monitoring page: cost and latency per email, escalation rate, approval and edit rate.
- Test set: about 60 labelled emails in English, Greek and German, with traps.

## Not in v1 (first guess)
Real mailbox connection, payments, more than one hotel, user accounts, channel managers (Booking.com etc.).

## Metrics
Extraction accuracy per field; price in reply matches the correct price (100%, enforced in code);
correct action (reply / ask for information / escalate); right tools called; unsafe actions = 0;
cost and time per email.

## Time
About 10 hours a week for 10 weeks. Stack carried over from my first project: Python, FastAPI, LangGraph,
React, Docker, GitHub Actions, Azure.

## Open questions
Hotel name and details; languages in v1; which model; where Postgres is hosted; how emails enter the system in v1.