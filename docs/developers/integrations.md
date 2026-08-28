# External services

## Clash of Clans

Every request uses the application-owned
`elbow_helper.infrastructure.clash.ClashClient`. It owns the HTTP session, API
authentication, request attempts, timeouts, response handling, and rate-limit
details. Features decide what data they need and what the workflow should show.

Use `elbow_helper.domain.player_tags` for tag normalization and URL encoding.

## AI text generation

Recruitment and Support Tickets request text through `TextGenerator`.
`elbow_helper.app` creates the `OpenAITextClient` that implements it.

- Recruitment uses it to summarize application answers and provide second
  opinions through `/opinion`.
- The support-ticket feature uses it for one welcome sentence.

Before adding another use, identify the exact data sent, audience, output
checks, privacy impact, and failure tests.

## Google Drive and Sheets

Roster, CWL, clan-health, and record reports publish through the
application-owned `GoogleSheetsPublisher`. Google OAuth credentials provide
access. `GOOGLE_DRIVE_FOLDER_ID` selects the destination; if it is empty,
exports go to the authenticated account's default location.

## HTML transcripts

Support and hibernation use `chat-exporter` to create HTML transcripts and
upload them to configured private Discord log channels.

New integrations use one app-owned client and expose a narrow interface to
features. Load their settings in `elbow_helper.app`, document what data leaves
the bot, and define what the feature does when the service is unavailable.
