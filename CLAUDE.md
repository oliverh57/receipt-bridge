# Receipt Bridge

## Releases happen only when I say so

Installed copies update when `VERSION` in `app/updates.py` goes up on
`main`, so every push of a new version reaches users. To avoid lots of tiny
updates, changes are batched:

- Commit each change locally as usual, but **don't push and don't raise
  `VERSION`** until I say I'm ready to release.
- When I ask for a release:
  1. Raise `VERSION` in `app/updates.py` by one patch number (`1.2.10` →
     `1.2.11`). Nothing else in that commit.
  2. Commit it with the message `Receipt Bridge <version>`, then a blank
     line, then one `- ` line per change since the last release, in plain
     words for the user. The app shows this message as "What's new".
  3. Push all the unpushed commits together.

A commit that changes nothing installed copies run (this file, the tests
alone) never needs a release.
