# Discord application setup

## Gateway intents

Enable:

- Server Members Intent
- Message Content Intent

## Scopes and permissions

The application uses the `bot` and `applications.commands` scopes. Its role
must sit above the members and roles it manages.

Across its features, the bot needs permission to:

- view channels and message history;
- send, edit, manage, and embed messages;
- attach files and add reactions;
- manage roles and nicknames;
- create and manage channels or threads;
- manage the server for invite tracking;
- view the audit log for departure reports;
- kick members through applicant cleanup.

Make sure channel permissions do not block the bot in any channel it uses.
