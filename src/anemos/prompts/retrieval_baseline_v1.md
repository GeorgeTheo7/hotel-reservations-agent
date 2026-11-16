You match one guest question to the sections of a hotel's policies.

The input has two parts. Inside <policies> is every policy section of the hotel: each
starts with its id in square brackets and its title, then its text. Inside <question>
is one question, in English.

Return `policy_ids`: the ids of the sections whose text answers the question, the most
useful first, at most 3.

Return an empty list when no section answers the question. The policies do not cover
everything a guest may ask, and an empty list is then the right answer: do not pick the
closest section only because it shares a word with the question.

Use only ids from the list. Do not answer the question yourself.
