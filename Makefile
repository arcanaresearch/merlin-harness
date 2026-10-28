.PHONY: release release-patch release-minor release-major release-bump release-smoke install-release version-check version-set critical-runtime-test benchmark-tooling-test lint format-check format wheels

version-check:
	uv run python scripts/check-versions.py

version-set:
	@test -n "$(VERSION)" || { echo "usage: make version-set VERSION=X.Y.Z" >&2; exit 2; }
	uv run python scripts/set_workspace_version.py "$(VERSION)"

release: version-check critical-runtime-test
	./scripts/build-release-macos-arm.sh
	./scripts/smoke-release-macos-arm.sh

critical-runtime-test:
	uv run pytest -q \
		apps/merlin-tui/tests/test_local_control.py::test_protocol_reports_cursor_gap_instead_of_silent_event_loss \
		apps/merlin-tui/tests/test_local_control.py::test_stream_can_resume_from_ordered_cursor \
		apps/merlin-tui/tests/test_local_control.py::test_completed_request_replay_survives_daemon_restart \
		packages/merlin-storage-postgres/tests/test_scheduler_store.py::test_pg0_campaign_pause_and_resume_survive_repository_reopen \
		packages/merlin-agents/tests/test_durable_service.py::test_pause_resume_with_dead_driver_lease_redrives_schedule \
		packages/merlin-browser/tests/test_browser_core.py::test_browser_release_is_campaign_scoped_for_shared_authenticated_session \
		packages/merlin-assessments/tests/test_headless_packaged_uat.py

benchmark-tooling-test:
	uv run pytest -q benchmarks/test_headless_campaign.py

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
