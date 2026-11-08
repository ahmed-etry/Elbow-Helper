# Agent knowledge files

The agent reads community reference material from `data/agent/knowledge/`.
These files are excluded from Git. Restore them with the rest of `data/` when
moving the bot to a new host.

## File format

Save UTF-8 Markdown files directly in the knowledge directory. Subdirectories
are not searched. Each file must be at most 1 MiB. Symbolic links are rejected.

Start unfinished material with these TOML settings between `+++` lines:

```toml
+++
visibility = "public"
status = "draft"
always_loaded = false
+++
```

The settings apply to the whole file:

| Field | Values | Default |
|---|---|---|
| `visibility` | `public`, `lead`, `lead_plus`, `core`, `recruiter`, `cwl_helper`, `examiner` | `public` |
| `status` | `approved`, `draft`, `retired` | `approved` |
| `always_loaded` | `true` or `false`, without quotes; either way the agent opens a section only when a question needs it | `false` |

Each visibility requires a role in the listed
[access group](../reference/commands-and-permissions.md#access-groups):

| Visibility | Who can read it |
|---|---|
| `public` | Anyone who can use the agent |
| `lead` | Lead (Co-Leaders and Cross-Leaders) |
| `lead_plus` | Lead Plus (Lead and Co-Leader Applicants) |
| `core` | Core leadership |
| `recruiter` | Recruiters or Lead Plus |
| `cwl_helper` | CWL helpers or Lead Plus |
| `examiner` | Examiners or Lead Plus |

Members with several roles can read material for each matching group. An
answer in a channel uses only sections everyone in that channel may read. If a
restricted section matches, the agent can say more is available privately,
without seeing that section's title or contents while it answers.

When that section could change the answer, **Show full answer** gives the
asker a private answer that uses their own access, but only after they click
it. **Post here** checks access again, then shares that answer; later answers
in the channel still can't use the restricted section. Answers that use the
audit log can't be posted.

Files without settings are approved and public as soon as they load. Use
`status = "draft"` while editing new material. Only `+++` settings control
access and approval; YAML `---` settings and review dates have no effect.

Use distinct, descriptive headings. Headings from `#` through `######` outside
code blocks start sections titled with their full path, such as
`Rank rules › Co-Leader › Requirements`. Headings without body text become
part of their children's paths. Text before the first heading uses the filename
as its title, without `.md`. Long sections split at paragraph boundaries;
an individual paragraph or code block is kept whole.

## Add or change material

1. Create a draft file with the intended visibility. Use separate files for
   sections that different members can read.
2. Check every section against the community's approved rules and workflows,
   then set `status = "approved"`.
3. For each question, the agent first sees the list of headings the asker may
   read, then opens only the sections it needs, including those marked
   `always_loaded`. Every approved section can still be found by searching,
   within its visibility.
4. To retire a file, set `status = "retired"` or remove it. To retire one
   section, remove it from the approved file or move it to a retired file.
5. After reloading, ask the agent about the changed topic. For restricted
   files, check with a member who has the required role and can use the agent.

## Reloading

The files load when the agent starts. Later requests check for file changes
at most once a minute. After saving, wait a minute and ask about the changed
topic, or restart the bot. A reload writes `Community knowledge loaded` to
the log.

If the log warns that the topic or file index is over its limit, shorten
headings or filenames. Sections left out of those lists can still be found by
searching.

## Missing content

- `Cannot inspect community knowledge files`: check that the directory exists
  and the bot can read it. Knowledge is unavailable until the files can load.
- `Cannot read community knowledge: <file>`: check the error for that file.
  Correct its TOML, field values, encoding, size, or file permissions, then
  save it again. Valid files still load; the rejected file is left out.
- If there is no load error, check the file's location, status, visibility,
  and headings. A search by title ignores case and works with the full heading
  path, or with just the heading when only one section the asker can read has
  it.
