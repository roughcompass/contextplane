This is an ordinary admitted document used to exercise the sandboxed
parser's happy path: prose before the first heading, a top-level title,
and a couple of nested sections with plain body text.

# Directive Title

This directive explains what an on-call engineer should do when a queue
depth alarm fires. It exists so the parser has more than one section to
split, and so the leading paragraph above becomes its own anchorless
section.

## Triage Steps

1. Check the consumer lag dashboard.
2. Confirm whether a recent deploy correlates with the alarm.
3. Page the owning team if lag continues to grow after five minutes.

## Escalation

If lag is still growing after fifteen minutes, escalate to the secondary
on-call rotation and open an incident channel.
