# quickmart3

## Development Setup

### Start dev
- Design the database model at `/migrations/0001_initial.sql`
- Run migration `uv run --env-file .env.dev -- migrations/migration.py`
- Or you can rebuild the database with `uv run --env-file .env.dev -- migrations/migration.py --rebuild`
- Deploy `uv run --env-file .env.dev -- app/main.py`

## Docker Deployment

### Build Docker Image

Build the Docker image using the uv-based Dockerfile:

```bash
docker build -t quickmart3:latest .
```

### Run Docker Container

#### Production Mode

Run the container with production environment variables from `.env.prod`:

```bash
docker run -d --restart unless-stopped --env-file .env.prod -p 8756:8756 --name quickmart3-app quickmart3:latest
```

#### Development Mode

Run the container with development environment variables:

```bash
docker run -d --restart unless-stopped --env-file .env.dev -p 8756:8756 --name quickmart3-app quickmart3:latest
```

#### Interactive Mode (See Logs)

Run the container in foreground to see logs:

```bash
docker run --env-file .env.prod -p 8756:8756 --name quickmart3-app quickmart3:latest
```

### Manage Docker Container

#### View Logs
```bash
docker logs -f quickmart3-app
```

#### Stop Container
```bash
docker stop quickmart3-app
```

#### Start Container
```bash
docker start quickmart3-app
```

#### Remove Container
```bash
docker rm quickmart3-app
```

#### Rebuild and Restart
```bash
docker stop quickmart3-app
docker rm quickmart3-app
docker build -t quickmart3:latest .
docker run -d --restart unless-stopped --env-file .env.prod -p 8756:8756 --name quickmart3-app quickmart3:latest
```

### Access Application

Once the container is running, access the application at:
- **URL**: `http://localhost:8756`
- **Port**: 8756 (configured in `.env.prod`)

### Docker Image Details

- **Base Image**: `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`
- **Python Version**: 3.13
- **Package Manager**: uv
- **Web Server**: Uvicorn with 3 workers
- **Security**: Runs as non-root user (appuser, UID 1000)
- **Features**:
  - Tailwind CSS auto-compilation on startup
  - PostgreSQL database connection
  - S3/Supabase storage integration
