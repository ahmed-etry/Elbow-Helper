# Privacy and AI

Elbow Helper uses Discord and Clash of Clans information to run community
features. What members can access depends on their Discord roles and channel
permissions.

## Information the bot may use

- Discord member IDs, roles, nicknames, messages, reactions, time spent in
  voice channels, joins, leaves, and activity in tickets;
- linked Clash account tags, profiles, clan membership, activity, and
  progression history;
- recruitment answers and notes, trial status, leadership records, roster
  signups, and support topics.

The bot logs ticket transcripts and saves Google spreadsheet exports to the
community's Google Drive.

## AI use

Recruitment uses AI for application summaries and `/opinion` second opinions
based on the selected applicant ticket. Application answers, ticket messages,
and attachment filenames may be sent, but attachment contents remain in
Discord.

Support tickets use the member's Discord display name and ticket topic to write
a welcome sentence. The ticket conversation remains in Discord.

These requests are currently processed through the OpenAI API. See
[OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data)
for how OpenAI handles API data.
