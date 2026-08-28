# Data and migrations

The feature that creates data owns its format and access.

## JSON

A feature defines its path, fields, defaults, supported older formats, and
handling for missing or malformed files. Write replacements atomically. Other
features use an owning service or reader instead of opening the file.

## SQLite

A feature owns its schema, queries, row conversion, current version, and
supported upgrades. Change `PRAGMA user_version` only after a complete
transaction. Reject unknown newer versions and non-empty unversioned databases
whose history cannot be proven.

New databases may begin at a later baseline. For example, a blank roster
database starts at version 4; retired pre-baseline migrations do not need to
run against it.

## Changing a format

1. Record the deployed format and supported backups.
2. Stop the bot and back up the affected data.
3. Add the smallest compatible reader or migration.
4. Make the change atomic or transactional.
5. Test blank, current, supported-old, failed, and unknown-newer states.
6. Update the [saved-data reference](../reference/storage.md).
7. Verify the deployed version after release.

Keep a migration while the live deployment or a usable backup may still
contain the older format.

Paths, JSON fields, spreadsheet labels, component IDs, and saved Discord
message IDs may outlive a Python rename. Change them only with an explicit
migration or transition plan.
