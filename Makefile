.PHONY: release release-patch release-minor release-major release-bump release-smoke install-release version-check version-set lint format-check format wheels

version-check:
	uv run python scripts/check-versions.py

version-set:
	@test -n "$(VERSION)" || { echo "usage: make version-set VERSION=X.Y.Z" >&2; exit 2; }
	uv run python scripts/set_workspace_version.py "$(VERSION)"

release: version-check
	./scripts/build-release-macos-arm.sh
	./scripts/smoke-release-macos-arm.sh

release-patch:
	@$(MAKE) release-bump PART=patch

release-minor:
	@$(MAKE) release-bump PART=minor

release-major:
	@$(MAKE) release-bump PART=major

release-bump:
	@case "$(PART)" in patch|minor|major) ;; *) echo "PART must be patch, minor, or major" >&2; exit 2;; esac
	uv run python scripts/bump-workspace-version.py "$(PART)"
	uv sync --all-packages
	@$(MAKE) release

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
