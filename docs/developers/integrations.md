# External services

## The Clash API

Every request goes through the one
`elbow_helper.infrastructure.clash.ClashClient` that the app creates. It
handles the HTTP session, the API key, timeouts, retries, and the request
limits all features share. It reuses a response for as long as the API's
Cache-Control header allows, and requests for the same data made at the same
time share one fetch.

Family collectors fetch profiles, wars, league groups, ranked history, and
raids. Features read that data from the family database and ask the
collectors' profile service when they need a fresh profile. Agent lookups that
ask the Clash API directly, and `/api`, use the same client. Clean up tags and
encode them for URLs with `elbow_helper.domain.player_tags`.

## AI text generation

`elbow_helper.app` creates one `DeepSeekTextClient`. Recruitment and Support
Tickets use its `TextGenerator` interface for application summaries,
`/opinion`, and a support welcome sentence. The [agent](agent.md) uses its
`AgentModel` interface and `AgentSession` for planning and answers.

Feature and agent code choose what to send and check the results. The client
sends the requests and reports usage and failures. [Privacy and AI](../users/privacy-and-ai.md)
lists what is sent. Before adding a new use, check what it sends, who sees the
result, and what happens when a request fails.

## Google Drive and Sheets

Roster, CWL, and health reports use the shared `GoogleSheetsPublisher`.
The Google OAuth credentials give it access. `GOOGLE_DRIVE_FOLDER_ID` picks
the Drive folder; if it's empty, exports go to the main My Drive folder of
that Google account. Each export deletes the bot's exports older than 30 days. Features also use `WorkbookWriter` and `LocalExportStore` for local
Excel files.

## HTML transcripts

Support and hibernation use `chat-exporter` to create HTML transcripts and
upload them to configured private Discord log channels.
