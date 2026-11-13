# Pricing examples (hand-calculated)

These examples are the expected values for the pricing and rules tests.
They were calculated by hand from `docs/SPEC.md`, not by the code.
If the code disagrees with an example, stop and find out which one is wrong.

All amounts are in euros. All dates are in 2027. A stay "12–16 July" means check-in 12 July,
check-out 16 July, nights of 12, 13, 14 and 15 July.

## Rules used

| Room type | Low | Mid | High | Standard / max people |
|---|---|---|---|---|
| Standard Double | 70 | 95 | 140 | 2 / 2 |
| Sea View Double | 95 | 130 | 190 | 2 / 3 |
| Family Suite | 140 | 180 | 260 | 2 / 4 |
| Accessible Double | 70 | 95 | 140 | 2 / 2 |

- Seasons: Low 15 Apr–31 May and 1–31 Oct. Mid 1 Jun–14 Jul and 1–30 Sep. High 15 Jul–31 Aug.
- Each night is priced at its own season.
- Extra bed per night: 20 for a child aged 2–11, 35 for anyone aged 12+. Cot for an infant under 2: free; the infant does not count as a person.
- City tax per room per night: 1.50 in Low, 4.00 in Mid and High. Paid on arrival, not part of the deposit.
- Minimum stay by arrival date: Low 1, Mid 2, High 3, arrivals 10–20 Aug 5.
- Deposit: 30% of the accommodation subtotal, rounded up to a whole euro.
- Free cancellation until 14 days before arrival (30 days in High season).

## Decisions these examples use (now spec D24–D27)

Q1–Q4 were confirmed and copied into `docs/SPEC.md` as D24 (Q1), D25 (Q2), D26 (Q3) and D27 (Q4).
The spec is the authority; this table is kept as the record of what the examples were calculated with.

| # | Question | Answer | Why |
|---|---|---|---|
| Q1 | Deposit base | Room nights **plus** extra beds; city tax excluded | "Accommodation subtotal" is everything the hotel charges for the stay itself; city tax is collected on arrival (D14) |
| Q2 | Who pays the extra bed | Sort guests by age, oldest first. The first `standard_occupancy` guests fill the standard places. Every other guest pays the extra-bed rate for their own age. Infants under 2 are left out before sorting | One deterministic rule; the youngest guests are the "extra" ones, which is the cheaper result for the guest |
| Q3 | 14 or 30 days when a stay touches High season | The **arrival date's** season decides | Same principle as minimum stay, which also goes by arrival date |
| Q4 | Over capacity: one bigger room or two smaller ones | First try a single room of the smallest type that fits everyone. Only if no single type fits, split across 2 rooms | One room keeps a party together and is one booking |

---

## Example 1: simplest case

Request: Standard Double, 20–21 May, 2 adults.

| Night | Season | Room rate | Extra bed | City tax |
|---|---|---|---|---|
| 20 May | Low | 70 | 0 | 1.50 |

- Rooms: 70. Extra beds: 0. Accommodation subtotal: **70**.
- City tax (on arrival): **1.50**.
- Deposit: 30% of 70 = **21**.
- Minimum stay: arrival in Low, minimum 1, stay is 1: OK.
- Free cancellation until: **6 May** (14 days before arrival).
- Expected result: quote; hold allowed.

## Example 2: season boundary with a child

Request: Sea View Double, 12–16 July, 2 adults + 1 child aged 8.

| Night | Season | Room rate | Extra bed | City tax |
|---|---|---|---|---|
| 12 Jul | Mid | 130 | 20 | 4.00 |
| 13 Jul | Mid | 130 | 20 | 4.00 |
| 14 Jul | Mid | 130 | 20 | 4.00 |
| 15 Jul | High | 190 | 20 | 4.00 |

- Rooms: 580. Extra beds: 80. Accommodation subtotal: **660**.
- City tax: **16.00**.
- Deposit: 30% of 660 = **198**.
- Minimum stay: arrival in Mid, minimum 2, stay is 4: OK.
- Free cancellation until: **28 June** (arrival is in Mid: 14 days, Q3).
- Expected result: quote; hold allowed.

## Example 3: Low into Mid, city tax changes mid-stay

Request: Standard Double, 29 May–3 June, 2 adults.

| Night | Season | Room rate | Extra bed | City tax |
|---|---|---|---|---|
| 29 May | Low | 70 | 0 | 1.50 |
| 30 May | Low | 70 | 0 | 1.50 |
| 31 May | Low | 70 | 0 | 1.50 |
| 1 Jun | Mid | 95 | 0 | 4.00 |
| 2 Jun | Mid | 95 | 0 | 4.00 |

- Rooms: 210 + 190 = 400. Accommodation subtotal: **400**.
- City tax: 4.50 + 8.00 = **12.50**.
- Deposit: 30% of 400 = **120**.
- Minimum stay: arrival in Low, minimum 1: OK.
- Free cancellation until: **15 May**.
- Expected result: quote; hold allowed.

## Example 4: minimum stay not met (10–20 August rule)

Request: Sea View Double, 10–13 August, 2 adults (3 nights).

- Minimum stay for arrival on 10 Aug is 5. The request fails with `MIN_STAY(5)`.
- The agent quotes the minimum-stay version instead: **10–15 August**, 5 nights.

| Night | Season | Room rate | Extra bed | City tax |
|---|---|---|---|---|
| 10 Aug | High | 190 | 0 | 4.00 |
| 11 Aug | High | 190 | 0 | 4.00 |
| 12 Aug | High | 190 | 0 | 4.00 |
| 13 Aug | High | 190 | 0 | 4.00 |
| 14 Aug | High | 190 | 0 | 4.00 |

- Rooms: 950. Accommodation subtotal: **950**.
- City tax: **20.00**.
- Deposit: 30% of 950 = **285**.
- Free cancellation until: **11 July** (arrival in High: 30 days).
- Expected result: quote for the 5-night version; **no hold**, because it is not what the guest asked for (D9).

## Example 5: infant in a cot

Request: Standard Double, 1–4 September, 2 adults + 1 infant aged 1.

| Night | Season | Room rate | Extra bed | City tax |
|---|---|---|---|---|
| 1 Sep | Mid | 95 | 0 | 4.00 |
| 2 Sep | Mid | 95 | 0 | 4.00 |
| 3 Sep | Mid | 95 | 0 | 4.00 |

- People counted: 2 (the infant is not counted, D13). Fits the Standard Double (max 2).
- Rooms: 285. Extra beds: 0 (cot is free). Accommodation subtotal: **285**.
- City tax: **12.00**.
- Deposit: 30% of 285 = 85.5, rounded up = **86**.
- Minimum stay: arrival in Mid, minimum 2, stay is 3: OK.
- Free cancellation until: **18 August**.
- Expected result: quote; hold allowed.

## Example 6: Family Suite with two extra beds at different prices

Request: Family Suite, 20–24 July, 2 adults + children aged 13 and 6.

| Night | Season | Room rate | Extra bed (13) | Extra bed (6) | City tax |
|---|---|---|---|---|---|
| 20 Jul | High | 260 | 35 | 20 | 4.00 |
| 21 Jul | High | 260 | 35 | 20 | 4.00 |
| 22 Jul | High | 260 | 35 | 20 | 4.00 |
| 23 Jul | High | 260 | 35 | 20 | 4.00 |

- The two adults fill the standard places; both children are extra (Q2).
- Rooms: 1040. Extra beds: 4 × 55 = 220. Accommodation subtotal: **1260**.
- City tax: **16.00**.
- Deposit: 30% of 1260 = **378**.
- Minimum stay: arrival in High (not 10–20 Aug), minimum 3, stay is 4: OK.
- Free cancellation until: **20 June** (30 days).
- Expected result: quote; hold allowed.

## Example 7: stay runs past closing

Request: Standard Double, 30 October–2 November, 2 adults.

- The last open night is 31 October. The stay's nights are 30 Oct, 31 Oct and 1 Nov (check-out 2 Nov is not a night); only the night of 1 November is closed.
- The agent offers the open part: **30 October–1 November**, 2 nights.

| Night | Season | Room rate | Extra bed | City tax |
|---|---|---|---|---|
| 30 Oct | Low | 70 | 0 | 1.50 |
| 31 Oct | Low | 70 | 0 | 1.50 |

- Rooms: 140. Accommodation subtotal: **140**.
- City tax: **3.00**.
- Deposit: 30% of 140 = **42**.
- Free cancellation until: **16 October**.
- Expected result: quote for the open part; **no hold** (D9).

## Example 8: over capacity

Request: Sea View Double, 5–8 June, 4 adults.

- Sea View holds at most 3 people. The request fails with `OVER_CAPACITY`.
- Smallest single room type that fits 4 people: **Family Suite** (Q4).

| Night | Season | Room rate | Extra beds (2 adults) | City tax |
|---|---|---|---|---|
| 5 Jun | Mid | 180 | 70 | 4.00 |
| 6 Jun | Mid | 180 | 70 | 4.00 |
| 7 Jun | Mid | 180 | 70 | 4.00 |

- Rooms: 540. Extra beds: 3 × 70 = 210. Accommodation subtotal: **750**.
- City tax: **12.00**.
- Deposit: 30% of 750 = **225**.
- Minimum stay: arrival in Mid, minimum 2, stay is 3: OK.
- Free cancellation until: **22 May**.
- Expected result: quote for the Family Suite as an alternative; **no hold** (D9).

## Example 9: who pays the extra bed

Request: Sea View Double, 3–5 October, 1 adult + children aged 13 and 8.

| Night | Season | Room rate | Extra bed (8) | City tax |
|---|---|---|---|---|
| 3 Oct | Low | 95 | 20 | 1.50 |
| 4 Oct | Low | 95 | 20 | 1.50 |

- Sorted by age: adult, 13, 8. The adult and the 13-year-old fill the standard places; the 8-year-old is extra (Q2).
- Rooms: 190. Extra beds: 40. Accommodation subtotal: **230**.
- City tax: **3.00**.
- Deposit: 30% of 230 = **69**.
- Minimum stay: arrival in Low, minimum 1: OK.
- Free cancellation until: **19 September**.
- Expected result: quote; hold allowed.

## Example 10: arrival in Mid, stay runs into High

Request: Standard Double, 13–17 July, 2 adults.

| Night | Season | Room rate | Extra bed | City tax |
|---|---|---|---|---|
| 13 Jul | Mid | 95 | 0 | 4.00 |
| 14 Jul | Mid | 95 | 0 | 4.00 |
| 15 Jul | High | 140 | 0 | 4.00 |
| 16 Jul | High | 140 | 0 | 4.00 |

- Rooms: 190 + 280 = 470. Accommodation subtotal: **470**.
- City tax: **16.00**.
- Deposit: 30% of 470 = **141**.
- Minimum stay: arrival in Mid, minimum 2, stay is 4: OK.
- Free cancellation until: **29 June** (arrival in Mid: 14 days, even though two nights are in High, Q3).
- Expected result: quote; hold allowed.

## Example 11: two rooms

Request: 2 × Standard Double, 24–27 August, 4 adults (2 per room).

| Night | Season | Room rate (per room) | Rooms | City tax (per room) |
|---|---|---|---|---|
| 24 Aug | High | 140 | 2 | 4.00 |
| 25 Aug | High | 140 | 2 | 4.00 |
| 26 Aug | High | 140 | 2 | 4.00 |

- Rooms: 3 × 140 × 2 = 840. Accommodation subtotal: **840**.
- City tax: 3 × 4.00 × 2 = **24.00**.
- Deposit: 30% of 840 = **252**.
- Minimum stay: arrival 24 Aug is High, outside 10–20 Aug, minimum 3, stay is 3: OK.
- Free cancellation until: **25 July** (30 days).
- Expected result: quote; hold allowed for both rooms.

## Example 12: extra bed across two rooms

Request: 2 × Sea View Double, 4–7 October, 2 adults + children aged 16, 12 and 4.

- Standard places: 2 rooms × 2 = 4 (D25, several rooms). Sorted over the whole party by age:
  adult, adult, 16, 12, 4. The first 4 fill the standard places; the 4-year-old is extra.
- Why "whole party": ordering room by room (e.g. adult + 16 + 12 in one room) would make the
  12-year-old pay 35 instead of the 4-year-old paying 20 (subtotal 675, deposit 203: wrong).

| Night | Season | Room rate (per room) | Rooms | Extra bed (4) | City tax (per room) |
|---|---|---|---|---|---|
| 4 Oct | Low | 95 | 2 | 20 | 1.50 |
| 5 Oct | Low | 95 | 2 | 20 | 1.50 |
| 6 Oct | Low | 95 | 2 | 20 | 1.50 |

- Rooms: 3 × 95 × 2 = 570. Extra beds: 3 × 20 = 60. Accommodation subtotal: **630**.
- City tax: 3 × 1.50 × 2 = **9.00**.
- Deposit: 30% of 630 = **189**.
- Minimum stay: arrival in Low, minimum 1, stay is 3: OK.
- Free cancellation until: **20 September** (14 days).
- Expected result: quote; hold allowed for both rooms.

---

## Summary

| # | Stay | Subtotal | City tax | Deposit | Free cancellation until | Result |
|---|---|---|---|---|---|---|
| 1 | Standard, 20–21 May, 2A | 70 | 1.50 | 21 | 6 May | quote |
| 2 | Sea View, 12–16 Jul, 2A + child 8 | 660 | 16.00 | 198 | 28 Jun | quote |
| 3 | Standard, 29 May–3 Jun, 2A | 400 | 12.50 | 120 | 15 May | quote |
| 4 | Sea View, 10–13 Aug, 2A → 10–15 Aug | 950 | 20.00 | 285 | 11 Jul | min-stay version, no hold |
| 5 | Standard, 1–4 Sep, 2A + infant | 285 | 12.00 | 86 | 18 Aug | quote |
| 6 | Family Suite, 20–24 Jul, 2A + 13 + 6 | 1260 | 16.00 | 378 | 20 Jun | quote |
| 7 | Standard, 30 Oct–2 Nov → 30 Oct–1 Nov | 140 | 3.00 | 42 | 16 Oct | open part, no hold |
| 8 | Sea View, 5–8 Jun, 4A → Family Suite | 750 | 12.00 | 225 | 22 May | alternative, no hold |
| 9 | Sea View, 3–5 Oct, 1A + 13 + 8 | 230 | 3.00 | 69 | 19 Sep | quote |
| 10 | Standard, 13–17 Jul, 2A | 470 | 16.00 | 141 | 29 Jun | quote |
| 11 | 2 × Standard, 24–27 Aug, 4A | 840 | 24.00 | 252 | 25 Jul | quote |
| 12 | 2 × Sea View, 4–7 Oct, 2A + 16 + 12 + 4 | 630 | 9.00 | 189 | 20 Sep | quote |