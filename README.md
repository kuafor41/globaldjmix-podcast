# GlobalDJMix Podcast RSS

A small, test-first builder for the GlobalDJMix episode archive.

- Source list: https://globaldjmix.com/livedjsets
- Feed: https://kuafor41.github.io/globaldjmix-podcast/rss.xml
- Scheduled update: every four hours
- Manual workflow modes: `test` (first 50 episodes), `incremental`, `full`

A test run independently validates audio links and artwork from each episode page. The published feed is not replaced unless all 50 test episodes resolve to audio and their image URLs verify successfully. A failed test is saved to `data/test-report.json` for diagnosis.
