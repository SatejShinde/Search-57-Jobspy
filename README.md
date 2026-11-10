# JobSpy internship watcher

Runs the existing LinkedIn, Indeed and ZipRecruiter searches on GitHub Actions (`master`).
The existing manual-dispatch endpoint and external schedule are unchanged.

Set repository variable `JOBSPY_DESCRIPTIONS_ENABLED=true` to enable bounded description follow-ups.
Existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` secrets are used. No laptop or new cron job is required.

Run tests with `python -m unittest discover -s tests -v`.
Use `python telegram_export.py index result.json` or `show result.json --key s57_...` for exported descriptions.
