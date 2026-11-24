> # SOURCE-CODE ARCHIVAL BACKUP: NOT A COMPLETE RUNNABLE ATLAS INSTALLATION
>
> This repository is a **sanitized source-code archive** of the Atlas AI Trader project. It is **incomplete**,
> it **will not run as a working installation**, and it **cannot reproduce or verify any research result**.
>
> * Every commit was rewritten. Commit hashes differ from the original project.
> * Removed from every version: provider market data and anything derived from it, strategy results and
>   performance, backtest outputs, benchmark and spread statistics, per-security research tables,
>   screenshots, databases, logs, models, credentials and private settings.
> * Some code and documents are redacted (`REDACTED_FOR_GITHUB`). Hash-pinned files that had to change are stored
>   as `<name>.redacted`.
> * Hashes cited in the files refer to the original files, which are kept offline.
> * See the `backup/manifests` branch for every modification and exclusion.

## This branch

Candidate V3. The default local database password became `CHANGE_ME_LOCAL_ONLY`, and the author's e-mail
became `author@atlas.invalid`.

* `SNAPSHOT-FILES.csv`: every file of `snapshot/2026-11-24-working-tree`: 1021 unchanged, 76 modified,
  6 replaced by SEC-only manifests, 79 excluded, with reasons and the stored files' hashes.
* `HISTORY-ACTIONS.csv`: what was changed or removed in every commit. `MESSAGE-CHANGES.csv`: corrected commit messages.
* `REFS.csv`: every branch. `HELD-LOCALLY.csv`: what is kept offline, by category.
* Hashes of changed or excluded originals, and the original commit ids, are kept offline only.

Exact bytes on checkout: clone with `--no-checkout`, write `* -text -filter -ident -working-tree-encoding` to
`.git/info/attributes`, then check out a branch.
