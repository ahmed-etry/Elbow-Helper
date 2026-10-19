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
| `always_loaded` | `true` or `false`, without quotes | `false` |

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

Members with several roles can read material for each matching group. Searches
cover all material the asker may read. The agent first tries to answer using
sections everyone in the channel can read; other matches contribute only their
titles and a note that they are available privately. Members without access
receive no titles or hints about those sections.

If the channel's material answers the question, the reply stays public. If it
does not and restricted sections matched, the agent uses those sections in one
additional pass and gives the asker a private answer to open. An unrelated
restricted match does not make an otherwise public answer private.

Files without settings are approved and public as soon as they load. Use
`status = "draft"` while editing new material. Only `+++` settings control
access and approval; YAML `---` settings and review dates have no effect.

Use distinct, descriptive headings. `#`, `##`, and `###` headings outside
code blocks start new sections; deeper headings stay within a section. Text
before the first heading uses the filename as its title, without `.md`.

## Add or change material

1. Create a draft file with the intended visibility. Use separate files for
   sections that different members can read.
2. Check every section against the community's approved rules and workflows,
   then set `status = "approved"`.
3. Set `always_loaded = true` for short references needed across requests.
   These load with research requests only where the channel may read them.
   All approved sections remain searchable.
4. To retire a file, set `status = "retired"` or remove it. To retire one
   section, remove it from the approved file or move it to a retired file.
5. After reloading, ask the agent about the changed topic. For restricted
   files, check with a member who has the required role and can use the agent.

## Reloading

The files load when the agent starts. Later requests check for file changes
at most once a minute. After saving, wait a minute and ask about the changed
topic, or restart the bot. A reload writes `Community knowledge loaded` to
the log.

If the log says the core is too long, shorten the references marked
`always_loaded` or set that field to `false`. If it says the topic or file
index is too long, shorten headings or filenames. Sections left out of these
lists remain searchable.

## Missing content

- `Cannot inspect community knowledge files`: check that the directory exists
  and the bot can read it. Knowledge is unavailable until the files can load.
- `Cannot read community knowledge: <file>`: check the error for that file.
  Correct its TOML, field values, encoding, size, or file permissions, then
  save it again. Valid files still load; the rejected file is left out.
- If there is no load error, check the file's location, status, visibility,
  and headings. Exact-title searches are case-sensitive. Distinct headings
  help the agent open the intended section.
