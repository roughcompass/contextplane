# Outcome shapes the pilot expects

One file per shape an orchestrator submits when a piece of delivery work
concludes. They exist so the outcome contract is pinned by examples a reader can
check against, rather than by assertions that restate the code they test.

Each file is the *submission*, not the stored row: the projection, the
references, and the two times. What the ledger derives — ingestion time,
authority, content digest — is deliberately absent, because an adapter that
could supply any of the three would be an adapter that could launder it.

`unjoinable_no_references.json` and `misspelled_kind.json` are the two shapes
that must be refused. They are kept here rather than inlined in the test for the
same reason the accepted shapes are: the point is that a reader can see what a
broken submission looks like, and both of these look entirely reasonable.
