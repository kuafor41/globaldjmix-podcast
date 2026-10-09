# UniversalMixDJ Podcast RSS

An automated RSS builder for the GlobalDJMix DJ mix and live-set archive.

- **Scheduled updates:** hourly, at minute 15, in the `Europe/Istanbul` timezone
- **Manual workflow modes:** `test`, `incremental`, `since2025`, `full`
- **Retention:** episodes from 1 January 2025 onward
- **Publication date:** `Post Date` first, then `Rec Date`, then an ISO date fallback
- **Audio:** candidate links are resolved and probed before an episode is included

## How the modes work

- **`test`** checks up to 50 recent archive entries and writes diagnostics to `data/test-report.json`. It does **not** change `rss.xml`, `data/items.json`, or `data/retry-queue.json`, so a test cannot replace the production feed with a 50-item feed.
- **`incremental`** is the normal hourly mode. It scans at most the first three archive pages and processes newly discovered episodes, plus up to 30 URLs from the retry queue.
- **`since2025`** is the manual backfill/refresh mode. It discovers the archive back to the 2025 boundary and reparses those episodes so stored publication dates can be refreshed.
- **`full`** is a manual full-archive scan. It is not used by the hourly schedule.

## Retry queue and diagnostics

Episodes whose audio cannot be resolved are placed in `data/retry-queue.json`. A failed audio resolution is retried once immediately; queued URLs are then retried on later incremental runs. The queue is capped at 1,000 URLs, with up to 30 attempted per hourly run. Successfully resolved URLs leave the queue.

The latest run report is saved in `data/test-report.json`. It includes archive-page errors, per-episode audio/image diagnostics, retry-queue counts, and RSS validation results. It also reports `new_episodes_added` and `new_episode_titles` for the current run, plus `daily_added_count` and `daily_episode_titles` for the current date in Turkey. The rolling `data/daily-additions.json` file records episodes actually added each day and keeps the last 365 days. Test mode does not change this daily history.

## Automatic validation

Every run validates the generated RSS XML before publishing. Checks include:

- valid RSS 2.0 XML and the expected item count;
- non-empty titles and GUIDs, with no duplicate GUIDs;
- valid HTTP(S) enclosure URLs and `audio/mpeg` enclosure types;
- valid publication dates, descending publication-date order, and the 2025 retention boundary;
- live probes of up to three newest enclosure URLs.

Structural validation errors block replacement of the published RSS and saved episode list, preserving the last known-good feed. Sampled live-audio probe failures are recorded as warnings because remote servers can fail temporarily; they do not by themselves block publication.

## Notes

GitHub Actions runs the scheduled workflow on the default branch. GitHub notes that scheduled workflows can occasionally be delayed during periods of high load, so an hourly schedule is a target cadence rather than a guarantee of exact start time. [GitHub Actions schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).
