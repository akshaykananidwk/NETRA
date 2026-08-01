# Database migrations

Drop numbered `.sql` files here — they run **once each**, in filename order,
during a GitHub update (Settings → 🔄 Update Now) right after the idempotent
`core/schema.sql` is re-applied.

```
migrations/
├── 001_add_visits_note.sql
├── 002_new_index.sql
└── ...
```

Rules:

- Filenames must sort correctly — zero-pad the number (`001_`, `002_`…).
- Each file may contain multiple statements; it runs via `executescript`.
- Applied files are recorded in the `settings` table
  (`migration:<filename>`), so re-running an update never re-applies them.
- Keep migrations additive (`ALTER TABLE ... ADD COLUMN`,
  `CREATE INDEX IF NOT EXISTS`) — SQLite has limited `ALTER` support and
  the same file must work on every installation regardless of age.
- The whole database is backed up automatically before migrations run;
  a failed migration triggers rollback of both code and DB.
