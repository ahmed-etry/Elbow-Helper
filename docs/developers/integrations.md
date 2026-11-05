# External services

## Clash of Clans

Every request uses the application-owned
`elbow_helper.infrastructure.clash.ClashClient`. It owns the HTTP session, API
authentication, request attempts, timeouts, response handling, and rate-limit
details. Family collectors fetch the wars and league groups used by features;
war and CWL consumers read `data/reads` rather than requesting them again.
Ranked history returns saved battles and when each battle log was checked, without
asking the Clash API for missing battles. Health reports read the family database as well. Only the agent's live Clash API
lookup asks the Clash API itself, through the shared client.

Use `elbow_helper.domain.player_tags` for tag normalization and URL encoding.

## AI text generation

Recruitment and Support Tickets request text through `TextGenerator`.
`elbow_helper.app` creates the `DeepSeekTextClient` that implements it.

- Recruitment uses it to summarize application answers and provide second
  opinions through `/opinion`.
- The support-ticket feature uses it for one welcome sentence.

The [agent](agent.md) uses the same application-owned client through
`AgentModel` and `AgentSession`. The client sends messages, tool results, and
images to DeepSeek and reports usage and request failures. The agent feature
plans and runs tools, checks permissions and answers, and sends the reply to
Discord. See [Privacy and AI](../users/privacy-and-ai.md#agent) for the data
these requests may include.

Before adding another use, identify the exact data sent, audience, output
checks, privacy impact, and failure tests.

## Google Drive and Sheets

Roster, CWL, and clan-health reports publish through the
application-owned `GoogleSheetsPublisher`. Google OAuth credentials provide
access. `GOOGLE_DRIVE_FOLDER_ID` selects the destination; if it is empty,
exports go to the authenticated account's default location.

## HTML transcripts

Support and hibernation use `chat-exporter` to create HTML transcripts and
upload them to configured private Discord log channels.

New integrations use one app-owned client and expose a narrow interface to
features. Load their settings in `elbow_helper.app`, document what data leaves
the bot, and define what the feature does when the service is unavailable.
