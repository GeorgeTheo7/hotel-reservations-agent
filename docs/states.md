# States and transitions

Every status an email, a draft, a booking and an outbox message can have, and what is allowed
to move each one from one status to the next. The schema, the approval transaction and the
tests in week 3 follow this file. If the code needs a transition that is not listed here,
stop and add it here first.

Conventions:
- **Final** means no transition leaves that status.
- **Time-based statuses are decided against the injected clock at the moment of an action**
  (same pattern as D22). A read never writes. Something that is past its deadline but not
  yet flipped is treated as already expired by every action.
- "Same transaction" means both changes happen or neither does.

---

## Email

Statuses: `received`, `processing`, `set_aside`, `escalated`, `awaiting_approval`,
`answered`, `expired`, `superseded`, `closed`

```mermaid
stateDiagram-v2
    [*] --> received
    received --> processing
    processing --> set_aside
    processing --> escalated
    processing --> awaiting_approval
    awaiting_approval --> answered
    awaiting_approval --> closed
    awaiting_approval --> escalated
    awaiting_approval --> expired
    awaiting_approval --> superseded
    set_aside --> escalated
    escalated --> closed
    expired --> closed
    answered --> [*]
    superseded --> [*]
    closed --> [*]
```

| From | To | Triggered by | What else happens | Decision |
|---|---|---|---|---|
| (new) | `received` | mailbox adapter stores the email | A second email with the same `message_id` is ignored, not stored twice | D4 |
| `received` | `processing` | a run starts for this email | One run per email (`thread_id = email_id`) | D17 |
| `processing` | `set_aside` | code router: spam or off-topic | No draft, no reply | Graph |
| `processing` | `escalated` | code router, the agent's `escalate` tool, guardrails failing twice, the loop limit, or an unexpected error | Reason code(s), summary, extracted request and tool results are stored; no draft | Escalation rules |
| `processing` | `awaiting_approval` | agent's draft passes the guardrails | Draft created as `pending_approval` (same transaction) | Graph |
| `awaiting_approval` | `answered` | owner approves the draft | Part of the approval transaction | D10 |
| `awaiting_approval` | `closed` | owner rejects the draft | Draft becomes `rejected` (same transaction); nothing sent, nothing held | Q1 |
| `awaiting_approval` | `escalated` | owner approves, but re-validation fails | Draft becomes `invalidated` (same transaction); reason `NO_LONGER_AVAILABLE` | Q5 |
| `awaiting_approval` | `expired` | 72 hours pass with no owner action | Draft becomes `expired` (same transaction); nothing sent, nothing held | D17, Q3 |
| `awaiting_approval` | `superseded` | a newer email from the same guest is stored | Draft becomes `superseded` (same transaction) | D17 |
| `set_aside` | `escalated` | owner: "this was not spam" | Reason `OWNER_REOPENED`; the owner handles it by hand | — |
| `escalated` | `closed` | owner marks it handled | — | Q6 |
| `expired` | `closed` | owner marks it handled | — | Q3 |

Final: `answered`, `superseded`, `closed`.

Not allowed in v1: any status back to `received` or `processing` (no re-run of an email);
`escalated` to `awaiting_approval` (no draft for an escalated email, Q6).

---

## Draft

Statuses: `pending_approval`, `approved`, `rejected`, `expired`, `superseded`, `invalidated`

```mermaid
stateDiagram-v2
    [*] --> pending_approval
    pending_approval --> pending_approval: owner edits
    pending_approval --> approved
    pending_approval --> rejected
    pending_approval --> invalidated
    pending_approval --> expired
    pending_approval --> superseded
    approved --> [*]
    rejected --> [*]
    invalidated --> [*]
    expired --> [*]
    superseded --> [*]
```

| From | To | Triggered by | What else happens | Decision |
|---|---|---|---|---|
| (new) | `pending_approval` | agent finishes a draft that passes the guardrails | Email becomes `awaiting_approval` (same transaction) | Graph |
| `pending_approval` | `pending_approval` | owner edits the text | A new text version is stored and the original is kept; guardrails run again on the edited text; a changed price is a warning the owner must confirm | Guardrails, Q2 |
| `pending_approval` | `approved` | owner approves | One transaction: lock the draft, flip stale holds to `expired`, re-validate, create holds, write the outbox row, write the audit entry, set email `answered` | D10 |
| `pending_approval` | `invalidated` | owner approves, but re-validation fails (a room is no longer free, or a price no longer matches) | No holds, no outbox row; email becomes `escalated` with `NO_LONGER_AVAILABLE`; audit entry; all in one transaction | Q5 |
| `pending_approval` | `rejected` | owner rejects | Email becomes `closed` (same transaction) | Q1 |
| `pending_approval` | `expired` | 72 hours after the draft was created | Email becomes `expired` (same transaction) | D17 |
| `pending_approval` | `superseded` | a newer email from the same guest is stored | Email becomes `superseded` (same transaction as storing the new email) | D17 |

Final: `approved`, `rejected`, `invalidated`, `expired`, `superseded`.

Rules:
- At most **one draft per email** in v1. Owner edits add text versions to that draft; they do not create a new draft.
- Approving a draft that is not `pending_approval` returns **409** and changes nothing (covers double approval, D10).
- Approving a draft that is more than 72 hours old but not yet flipped is treated as `expired`: it is flipped and the approval is refused.
- The text that is sent is the **latest version** at approval, with `{{HOLD_UNTIL}}` filled in during the approval transaction.

---

## Booking

Statuses: `hold`, `confirmed`, `cancelled`, `expired` (already in the database, migration 0002)

| From | To | Triggered by | What else happens | Decision |
|---|---|---|---|---|
| (new) | `hold` | the approval transaction only | `expires_at` = approval time + 48 hours; lowest free room number(s) | D9, D10, D38 |
| `hold` | `confirmed` | owner: "deposit received" (Holds page) | `expires_at` no longer matters | Q4 |
| `hold` | `cancelled` | owner releases the hold (Holds page) | — | Q7 |
| `hold` | `expired` | `expires_at` is in the past; flipped by the approval transaction's sweep or by the tidy-up job | Never flipped by a read | D10, D22 |
| `confirmed` | `cancelled` | owner (Holds page) | — | Q7 |

Final: `cancelled`, `expired`.

Rules:
- **Only the approval transaction creates bookings.** No tool, no model and no other endpoint can.
- A hold is created **only when `hold_allowed` is true** for the draft's quote (D9).
- `hold` to `confirmed` is allowed **as long as the status is still `hold`**, even after `expires_at`. While the row is still `hold`, the database constraint has kept the room from being given to anyone else, so confirming is safe. Once it has been flipped to `expired`, it is final.
- No status ever goes back to `hold` or `confirmed` from `cancelled` or `expired`.
- (new) directly to `confirmed` does not exist in v1 outside test fixtures.

---

## Outbox message

Statuses: `pending`, `sent`

| From | To | Triggered by | What else happens | Decision |
|---|---|---|---|---|
| (new) | `pending` | the approval transaction only | Stores the final text, exactly as it will be sent | D4, D10 |
| `pending` | `sent` | the sender (simulated in v1), after the approval transaction has committed | `sent_at` recorded | D4 |

Final: `sent`.

Rules:
- **Exactly one outbox row per approved draft** (unique on the draft). This is what makes a double send impossible.
- The stored text never changes after it is written.
- If sending fails, the row stays `pending`, with an attempt count and the last error, and is tried again. A `failed` status after repeated attempts is left for later, since v1 sending is simulated.

---

## Rules that hold across all four

These are checked by tests in week 3.

1. An email is `answered` if and only if its draft is `approved`.
2. An outbox row exists if and only if its draft is `approved`, and there is exactly one.
3. Every booking created by the system points to the request of an `approved` draft.
4. A draft whose quote has `hold_allowed` false never produces a booking.
5. The passing of time can only expire things. It never sends a reply, creates a hold, approves a draft or escalates an email (D17).
6. Nothing changes a final status.

---

## Questions and the answers assumed here

Confirm each one, then copy it into the spec as a decision.

| # | Question | Assumed answer | Why |
|---|---|---|---|
| Q1 | What can the owner do after rejecting a draft? | Nothing more in the system: the draft is `rejected`, the email is `closed`, and the owner replies by hand. "Reject with feedback and get a new draft" goes to Later | A redraft loop driven by owner feedback is a feature of its own; v1 keeps one draft per email |
| Q2 | After an edit, which text is sent, and is the original kept? | The latest version is sent. Every version is kept, with who wrote it (agent or owner) | The audit trail must show what the agent proposed and what the owner changed |
| Q3 | What happens to the email when its draft expires? | The email becomes `expired`, shown in its own list. The owner can only close it. It is **not** moved to `escalated` | D17 says nothing is escalated automatically |
| Q4 | How does a hold become `confirmed` without payments? | The owner clicks "deposit received" on the Holds page | Otherwise every hold would simply expire and the `confirmed` status would never be used |
| Q5 | Approval finds the room is no longer free | The draft becomes `invalidated`, the email becomes `escalated` with `NO_LONGER_AVAILABLE`, and nothing is held or sent. The owner replies by hand | It is the result of an owner action, not of time, so it does not break D17. Re-running the agent for a fresh offer goes to Later |
| Q6 | Can an escalated email get a draft later? | No. `escalated` goes only to `closed` | Keeps v1 to one run per email (D17) |
| Q7 | Who can cancel a hold? | Only the owner, on the Holds page. A guest's cancellation email is escalated (`CANCELLATION`) and the owner cancels by hand | Changes and cancellations are escalated in v1 (Scope) |

## Still open

These need a decision before the approval transaction is written.

1. **What does re-validation check for a draft with no hold?** For a held quote it is clear: the price still matches and the rooms are still free. For an offer without a hold (minimum-stay version, alternatives, options), should approval also check that each offered option is still available, or only that the prices still match?
2. **How is "the same guest" recognised for superseding?** By `from_address` only, or also by `in_reply_to` / `thread_id`? And does a newer email supersede only a `pending_approval` draft, or also an email still in `processing`?
3. **New reason codes.** This file uses three that are not in the spec's list: `NO_LONGER_AVAILABLE` (as an escalation reason, not only a tool error), `OWNER_REOPENED`, and one for an unexpected error during processing (for example `SYSTEM_ERROR`).
4. **`set_aside` emails.** Is there a list where the owner can see them, so a wrongly filtered email can be found?