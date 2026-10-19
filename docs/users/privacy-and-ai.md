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

The bot logs ticket transcripts and saves spreadsheet exports to the
community's Google Drive. Spreadsheet exports older than 30 days are deleted
during a later export.

## AI use

Recruitment uses AI for application summaries and `/opinion` second opinions
based on the selected applicant ticket. Application answers, ticket messages,
and attachment filenames may be sent. Attachment contents stay in Discord
for these requests.

Support tickets use the member's Discord display name and ticket topic to write
a welcome sentence. The ticket conversation stays in Discord for this request.

These requests are currently processed through the DeepSeek API. See
[DeepSeek's privacy policy](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)
for how DeepSeek handles API data.

## Agent

Core leadership and members with the Agent Tester role can mention Elbow
Helper to ask a question or request an action. Reply to its answer to continue
the conversation. Your roles and channel permissions determine what you can
ask it to read or do. For changes such as assigning roles, the bot shows a
preview and asks you to confirm.

Agent requests use DeepSeek. To answer you, the bot may send:

- your message, nearby messages, and earlier parts of the conversation;
- community references and Discord or Clash information it reads;
- images and GIFs from your message, messages you reply to or link, and
  earlier parts of the conversation;
- text and spreadsheet contents it reads for your request.

If the bot cannot confirm that everyone in the channel can see the sources
used for an answer, it offers **View result** so you can read it privately.
**Post here** shares the answer with the channel. Later answers to you in
that conversation can then appear in the channel when their sources and
required permissions are already covered by your sharing choices.
Answers using the Discord audit log stay private, with no **Post here** button.

The bot saves agent request and reply text with no automatic deletion. It
also keeps troubleshooting records for 30 days, including questions, planned
steps, lookup results, and answers. These records remain saved when the
conversation expires.
