# Medik

This repository no longer contains the KuCoin spot-trading toolkit. The
`kucoin/` package, its examples, its tests and the CI workflow that ran
them were removed.

Nothing is lost - the code is still in this repository's history. Commit
`95b04f3` is the last one that contained it:

```bash
# browse the code as it was
git checkout 95b04f3

# or restore it onto a branch
git checkout -b restore-kucoin 95b04f3
```

No API credentials were ever committed here: `.env` was gitignored from
the first commit, and `.env.example` only ever held empty placeholders.
Removing the code therefore has no bearing on the safety of any KuCoin
key. If a key was used on a machine you have doubts about, rotate it at
<https://www.kucoin.com/account/api> - deleting source code does not
revoke a key that has already been issued.
