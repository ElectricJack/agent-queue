# Pool display names and settings navigation

`pool_rename` (`aq pool rename --profile-id <id> --name <name>`) changes the
display name of an existing global pool profile. The profile ID, routing,
bounds, sessions and task assignments remain unchanged. Names are trimmed,
contain 1–120 characters, and cannot contain control characters.

For vault profiles, the command patches only the frontmatter `name`, keeps
an exact backup, writes atomically, and immediately updates the database
name. Derived profiles retain their `extends` and authored configuration.
For legacy database-only profiles, it backs up the profile row before updating
its name. Persistence failures are reported as failures, never successful
non-durable edits. `pool.renamed` records the profile ID, old and new names,
and backup path. Repeating an unchanged name is a no-op.

Pool status exposes `name` alongside `profile_id`. The dashboard directory,
flock rail, pool window and live-session pool labels display that name, with
the ID as a fallback. Profile pickers refresh after a rename.

Directory rows and their explicit Settings links open the pool's settings
tab at `/agents?agent=pool%3A<id>&pool-view=settings`. The settings tab is
addressable across reloads and browser Back/Forward. A Back to pools link
returns to `/agents`. Enable switches operate independently of row navigation.
