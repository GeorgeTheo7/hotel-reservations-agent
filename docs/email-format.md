# Test-email format (schema version 1)

One YAML file per email, named `<id>.yaml`. The same format and loader
(`anemos.email_cases`) serve the evaluation set (`eval/emails/`), the demo inbox
(`src/anemos/demo_emails/`), the fake mailbox and the tests. All data is fictional.

Files are read with `yaml.safe_load` and validated strictly: an unknown key, a wrong
type or a broken rule refuses the whole file, and the error names the file
(`tests/unit/test_email_cases.py` checks every file and every rule).

Each file is one email on its own. Superseding sequences (a second email from the same
guest) are out of scope for the eval files; the integration tests cover them.

```yaml
schema_version: 1                       # required; the only version
id: en-quote-sea-view-june              # lowercase words joined by "-"; equals the file name;
                                        # the Message-ID is <id@cases.example.test>
split: dev                              # dev | held_out | demo (demo: never in the evaluation)
tags: [exact_dates]                     # free labels for stratifying the set (traps etc.)

email:
  from: "Anna Fictional <anna@example.test>"   # an @example.test address (fictional data)
  subject: "Sea view room in June"             # may be ""
  received_at: "2026-11-20T09:15:00+00:00"     # with a timezone; pinned for date traps
  in_reply_to: null                            # optional; set = thread reply
  body: |
    Hello, do you have a sea view room for two adults from 1 to 4 June 2027?

world:                                  # optional: the database state the case starts from;
  bookings:                             # left out = the seeded hotel with no bookings
    - {room: 101, check_in: "2027-07-10", check_out: "2027-07-14", status: confirmed}
    - {room: 201, check_in: "2027-06-01", check_out: "2027-06-03", status: hold,
       expires_at: "2026-11-21T09:00:00+00:00"}   # expires_at exactly for holds

labels:
  language: en                          # two-letter code: en, el, or another ("other language")
  script: latin                         # greek | latin (D5); el + latin = Greeklish;
                                        # en needs latin, greek needs el
  extraction:                           # gold extraction; null for spam or a pure question
    date_spec:                          # null if the guest gave no dates
      kind: exact
      check_in: {day: 1, month: 6, year: 2027, ambiguous_order: false}   # D37
      check_out: {day: 4, month: 6, year: 2027}
      nights: null
    adults: 2
    children: 0
    child_ages: []                      # may be shorter than children: ages missing
    rooms: 1
    room_type: sea_view                 # a seeded room type id, or null if none named
    flags: []                           # legal_threat | billing_request | medical_need |
                                        # accessibility_need | injection_suspected
  route: quote                          # quote | ask_info | policy | decline | escalate | set_aside
  reason_codes: []                      # spec reason codes; non-empty exactly when route is escalate
  required_tools: [check_availability, quote_price]   # also suggest_alternatives, search_policies
  policy_ids: []                        # policies the answer must use; allowed with quote
                                        # (a request plus a policy question)
  holds: 1                              # rooms held at approval (0-2)
  forbidden_phrases: []                 # must never appear in the draft
```

The other `date_spec` kinds mirror `anemos.reservations.datespec` field for field:

```yaml
{kind: relative, phrase: next_weekend, nights: null}             # or this_weekend
{kind: holiday, holiday: easter, tradition: unspecified, year: null, nights: 3}
{kind: approximate, part: late, month: 6, year: null, nights: 7} # early | mid | late | whole_month
```

Optional keys: `tags`, `in_reply_to`, `world`, `nights`, `year`, `ambiguous_order`,
`flags`, `reason_codes`, `required_tools`, `policy_ids`, `holds`, `forbidden_phrases`
(all default to empty, null, false or 0).

An email with no request but with a flag (e.g. a complaint with a legal threat) sets
`extraction` to just the flags, `extraction: {flags: [legal_threat]}`; `extraction: null`
is for an email with neither (spam, a pure question).

Not labels: expected prices (computed by the unit-tested pricing code from the gold
extraction) and summaries (week 6).

YAML traps the strict types catch: an unquoted `no` is the boolean false (quote a
Norwegian `"no"`), and a `received_at` without a timezone is refused.
