# Privacy and AI

Elbow Helper uses Discord and Clash of Clans information to run community
features. What members can access depends on their Discord roles and channel
permissions.

## Information the bot may use

- Discord member IDs, roles, nicknames, messages, reactions, time spent in
  voice channels, joins, leaves, and activity in tickets;
- linked Clash account tags, profiles, clan membership, activity, and
  progression history;
- recruitment answers and notes, trial status, roster
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
a welcome sentence. This request includes no ticket conversation.

These requests are currently processed through the DeepSeek API. See
[DeepSeek's privacy policy](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)
for how DeepSeek handles API data.

## Agent

Lead Plus, Core leadership and members with the Agent Tester role can
mention Elbow Helper to ask a question or request an action. Reply to its
answer to continue the conversation. Your roles and channel permissions determine what you can
ask it to read or do. For changes such as assigning roles, the bot shows a
preview and asks you to confirm.

Agent requests use DeepSeek. To answer you, the bot may send:

- your message, nearby messages, and earlier parts of the conversation;
- community reference files and the Discord or Clash information it looks up;
- images and GIFs from your message, messages you reply to or link, and
  earlier parts of the conversation;
- text and spreadsheet contents it reads for your request.

An answer in a channel only uses what everyone in that channel can see. If
something only you can see would help, **Show full answer** gets you a private
answer, but only after you click it. **Post here** shares that answer with the
channel. Some results appear behind **View result** instead, with their own
button to share them. The bot checks your access again before it shares
anything or answers a follow-up. Answers that use the Discord audit log stay
private, with no **Post here** button.

The bot forgets a conversation, and drops any longer Discord search it started,
after six hours with no new messages. Separately, it keeps request and reply
text, attachment names, a short note of each lookup and what it looked up, and
reports of wrong answers, with no automatic deletion. Detailed troubleshooting
records, including what each lookup found, are kept for 30 days. Forgetting a
conversation doesn't delete these.
