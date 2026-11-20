# Production Telegram and archive delivery

Job alerts are one plain-text message:

```text
Company — Job Title
Location
Direct job posting URL
```

Unknown locations use `Location not specified`. Titles/company/location are flattened to one line; markup is literal plain text, URLs are validated and unsafe URL characters encoded. Link previews are disabled. JobSpy prefers a valid employer-direct URL and otherwise keeps the source posting URL. Search57 uses known Oracle tenant/site/native-ID information to replace generic careers links with specific posting links when available. Oversized messages remain pending rather than splitting or silently shortening the title.

No job descriptions, envelopes, qualification summaries, scores, source metadata, hashes or multipart completion messages go to Telegram. Historical wire-format/parser fixtures remain for old exports only; the old live description canary is retired. Search57 startup job alerts use the same compact layout; non-job funding-prospect behavior is unchanged.

## Independent private archive

`SEARCH57_DESCRIPTIONS_ENABLED` / `JOBSPY_DESCRIPTIONS_ENABLED` continue to control extraction and remain enabled. They no longer enable Telegram description delivery. Targeted retrieval and existing listing reuse remain active; no catalog-wide detail fetch is introduced.

Already-returned descriptions are encrypted before the human notification when available. Extracted bodies are staged in the durable archive outbox using authenticated AES-256-GCM and the existing archive-only `JOB_ARCHIVE_SSH_KEY`. Only opaque ciphertext is added to the public pending metadata; plaintext stays in ignored runner files and the private archive. Keep that key available until pending ciphertext has been drained. Startup extraction persists the same encrypted handoff in its existing separate state branch.

A successful alert does not acknowledge an archive write. Extraction work retires only after a durable encrypted handoff, and the archive outbox retires only after the private commit succeeds (or an identical existing record is verified). A failed private write retains the exact encrypted body across runners, even if the upstream posting later closes. Failed staging retains extraction work. Telegram failure does not prevent description staging; the unsent compact alert remains recoverable. Existing fair bounded extraction, private push retries and the private recovery consumer remain in place.

The extraction worker can recover a normal unsent alert, but cannot emit description text. Startup's Python extraction worker has no Telegram credentials and never sends job alerts; its Node watcher owns those acknowledgements. The bridge and historical backfill are archive-only and have no notification path. Reading/backfilling archive records never requeues notifications or changes application/review status.

Existing search queries, inventories, eligibility/location filters, schedules, recipients and per-watcher seen identities remain unchanged. Cross-source alert deduplication is not introduced: the archive has opportunity deduplication but no atomic shared Telegram-send claim. Source alerts are retained to avoid unsafe suppression.

## Verification

Full regression suites cover exact three-line output, arbitrary markup/newlines, unknown locations, clickable URLs, no multipart sends even with legacy acknowledgement state, no historical replay, independent archive staging during Telegram failure, failed private pushes, and complete encrypted-body recovery after a fresh runner.

Manual `archive-write-check.yml` replays one existing real archived description through encryption and the canonical private writer and records a private receipt. It injects no job, does not scan, changes no watcher seen state, and has no Telegram credentials. It verifies full-description hashes and a real scoped-key push without historical alerts.
