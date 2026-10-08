.PHONY: release release-smoke install-release version-check version-set lint format-check format wheels

version-check:
	uv run python scripts/check-versions.py

version-set:
	@test -n "$(VERSION)" || { echo "usage: make version-set VERSION=X.Y.Z" >&2; exit 2; }
	uv run python scripts/set_workspace_version.py "$(VERSION)"

release: version-check
	./scripts/build-release-macos-arm.sh
	./scripts/smoke-release-macos-arm.sh

release-smoke:
	./scripts/smoke-release-macos-arm.sh

install-release: release
	./scripts/install-release-macos-arm.sh

lint:
	uv run ruff check .

format-check:
	uv run ruff format --check .

format:
	uv run ruff format .

wheels: version-check
	uv run python scripts/encrypt-builtin-packs.py
	uv build --all-packages --wheel --out-dir dist/wheels
