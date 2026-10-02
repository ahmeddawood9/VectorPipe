# Documentation Instructions

This directory documents how VectorPipe was built, step by step, and will also hold deployment
guides, architecture runbooks and operational documentation.

## Structure

```
docs/
  README.md                 index: every step, its date and its commits
  steps/NN-short-name.md    one page per step
```

## Adding a step

Every commit on `main` belongs to exactly one step. When you finish a piece of work:

1. Create `steps/NN-short-name.md` with the next number.
2. Use these sections: **Goal**, **What was built**, **Key design decisions** (or "How it got
   here" for problems met along the way), **How to verify**, **Left open**.
3. Head the page with the date and the commit hashes with their subjects.
4. Add a row to the table in [README.md](README.md) and update "Where things stand".
5. Commit the doc with the work, or straight after it.

Write facts that were checked. If something was not verified, say so under **Left open**.
Never put account secrets in docs: use `<account-id>` and `<bucket>` placeholders.
