# Elbow Helper

Elbow Helper is a Discord bot built for Brown Elbow, a Clash of Clans
community made up of several clans. It started as a way to automate
recruitment work.

Recruitment was one of my main responsibilities in the server, and there
weren't enough of us to keep up with all the repetitive work. Even moving an
applicant to the next stage meant renaming their ticket channel by hand.

Out of curiosity, I looked into whether that one task could be automated. It
could. Once it worked, the project stopped feeling limited to a single task. I
kept turning ideas into working features, then testing and refining them
through daily use.

From there, the bot grew into leadership workflows and took over more
repetitive work. It also began collecting activity and war history and turning
it into reports, boards, and planning tools that would be impractical to
maintain by hand.

## What it brings together

- recruitment tickets, applicant conversations, account linking, trials, and
  membership;
- member achievements, coins, raffle tickets, events, and rewards;
- rosters, schedules, posts, roles, and exports;
- CWL signups, planning, clan transfers, briefs, war boards, and bonus reports;
- regular war boards, summaries, role updates, and follow-up messages;
- clan and account activity history, health reports, records, and workbooks;
- hibernation and return flows, promotion examinations, support tickets, role
  connections, and community news.

## Documentation

- [Using the bot](docs/users/getting-started.md)
- [Commands and access](docs/reference/commands-and-permissions.md)
- [Running the bot](docs/operators/deployment.md)
- [Architecture](docs/developers/architecture.md)
- [All documentation](docs/README.md)

Discord `/help` is the source for current command options, examples, and
restrictions.

## Running the bot

Elbow Helper runs on Python 3.12.

```bash
uv sync --locked
```

Copy `.env.example` to `.env`, fill the deployment credentials, and review the
[community configuration](docs/operators/configuration.md). Then start the bot:

```bash
uv run python -m elbow_helper
```

Runtime databases, state, logs, and exports live under `data/` and
are excluded from Git.

## Attribution

Some features include code adapted from
[ClashPerk](https://github.com/clashperk/clashperk); others were informed by its
workflows. Adapted code remains covered by ClashPerk's MIT notice. See
[third-party notices](THIRD_PARTY_NOTICES.md) for details.

Built with assistance from OpenAI [Codex](https://openai.com/codex/).

This material is unofficial and is not endorsed by Supercell. See
[Supercell's Fan Content Policy](https://supercell.com/en/fan-content-policy/).

## License

Elbow Helper is licensed under the [MIT License](LICENSE).
