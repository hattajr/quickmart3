.PHONY: dev

dev:        ## run hot-reload dev server (local SQLite) + devtunnel to laptop
	@command -v devtunnel >/dev/null || { echo "devtunnel not found on PATH"; exit 1; }
	@APP_PORT=$$(grep '^APP_PORT=' .env.dev | cut -d= -f2 | tr -d ' '); \
	devtunnel $$APP_PORT $$APP_PORT mbp & \
	TUNNEL_PID=$$!; \
	trap 'pkill -TERM -P $$TUNNEL_PID 2>/dev/null; kill -TERM $$TUNNEL_PID 2>/dev/null' EXIT INT TERM; \
	uv run --env-file .env.dev run_dev.py
