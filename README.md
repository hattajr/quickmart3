# quickmart3



### Start dev
- Design the database model at `/migrations/0001_initial.sql`
- Run migration `uv run --env-file .env.dev -- migrations/migration.py`
- Or you can rebuild the database with `uv run --env-file .env.dev -- migrations/migration.py --rebuild`
- Deploy `uv run --env-file .env.dev -- app/main.py`
