from pathlib import Path

import pytest
from merlin_config import ConfigPaths, CredentialStore, McpRuntimeTuning, load_config
from merlin_mcp import McpCallResult, McpManager, McpServerInfo, McpServerStateError
from merlin_tools import ToolBroker
from merlin_tui.configuration import McpConfiguration
from merlin_tui.mcp_service import McpService


class Manager:
    def __init__(self) -> None:
        self.config = None

    async def add_server(self, config, *, start=False) -> None:
        self.config = config

    async def check(self, name):
        return McpServerInfo(name=name, running=True, tools=())

    async def list_tools(self, name):
        return ()

    async def remove(self, name):
        self.config = None


def test_service_projects_central_tuning_into_local_and_remote_servers() -> None:
    manager = McpManager()
    tuning = McpRuntimeTuning(
        startup_timeout_seconds=20,
        invocation_timeout_seconds=90,
        output_timeout_seconds=15,
        shutdown_timeout_seconds=12,
        connect_timeout_seconds=18,
        health_timeout_seconds=9,
        reconnect_attempts=2,
        reconnect_delay_seconds=0.5,
        max_tools=256,
        max_schema_bytes=512_000,
        max_output_bytes=2_000_000,
    )
    service = McpService(manager, ToolBroker(), tuning=tuning)

    service.load(
        {
            "servers": {
                "local": {"command": "python", "args": ["server.py"]},
                "remote": {
                    "remote_url": "https://mcp.example.test/mcp",
                    "credential_handle": "remote-prod",
                },
            }
        }
    )

    local = manager.server_config("local")
    assert local.timeouts == (20, 90, 15, 12)
    remote = manager.server_config("remote")
    assert remote.connect_timeout == 18
    assert remote.invocation_timeout == 90
    assert remote.health_timeout == 9
    assert remote.shutdown_timeout == 12
    assert remote.reconnect_attempts == 2
    assert remote.reconnect_delay == 0.5
    assert remote.max_tools == 256
    assert remote.max_schema_bytes == 512_000
    assert remote.max_output_bytes == 2_000_000


async def test_add_persists_launch_data_but_not_environment_secrets(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    manager = Manager()
    service = McpService(manager, ToolBroker(), McpConfiguration(paths))

    await service.add_server(
        (
            "local",
            "--cwd",
            str(tmp_path),
            "--env",
            "TOKEN=top-secret",
            "python",
            "server.py",
        )
    )

    assert manager.config.env == {"TOKEN": "top-secret"}
    values = load_config(paths, environ={}).values
    assert values["mcp"] == {
        "servers": {
            "local": {
                "command": "python",
                "args": ["server.py"],
                "cwd": str(tmp_path),
            }
        }
    }
    assert "top-secret" not in paths.config.read_text(encoding="utf-8")

    await service.remove_server("local")
    assert load_config(paths, environ={}).values["mcp"] == {"servers": {}}


class RemoteManager:
    def __init__(self) -> None:
        self.config = None
        self.running = False
        self.admitted = False
        self.restarts = 0
        self.resolver = None

    def set_credential_resolver(self, resolver) -> None:
        self.resolver = resolver

    async def add_server(self, config, *, start=False) -> None:
        self.config = config
        self.running = start

    async def check(self, name):
        return McpServerInfo(
            name,
            self.running,
            (),
            quarantined=not self.admitted,
            inventory_digest="digest-1",
            server_identity="remote-test",
        )

    def list_servers(self):
        return (McpServerInfo("remote", self.running, ()),) if self.config else ()

    async def list_tools(self, name):
        return ()

    async def restart(self, name):
        self.restarts += 1
        self.running = True

    async def stop(self, name):
        self.running = False

    def admit_remote_inventory(self, name, digest):
        assert digest == "digest-1"
        self.admitted = True

    def server_config(self, name):
        return self.config

    async def invoke(self, server, tool, arguments):
        return McpCallResult(server, tool, ({"type": "text", "text": "ok"},), None, False)


async def test_remote_add_restart_disable_and_audit_are_projected(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    CredentialStore(paths).save({"remote-prod": "actual-top-secret"})
    manager = RemoteManager()
    service = McpService(
        manager,
        ToolBroker(),
        McpConfiguration(paths),
        CredentialStore(paths),
    )
    added = await service.add_server(
        (
            "remote",
            "--remote",
            "https://mcp.example.test/mcp",
            "--credential",
            "remote-prod",
            "--reconnect",
            "2",
        )
    )
    assert added["admission"] == "quarantined"
    assert manager.resolver(manager.config.credential_handle) == "actual-top-secret"
    assert "actual-top-secret" not in paths.config.read_text(encoding="utf-8")

    admitted = await service.admit_server("remote", "digest-1")
    assert admitted["admission"] == "admitted"
    restarted = await service.restart_server("remote")
    assert restarted["health"] == "healthy" and manager.restarts == 1
    await service.invoke("remote", "echo", {})
    projection = (await service.list_servers())[0]
    assert projection["reconnect"] == {"attempts": 2, "delay_seconds": 0.1}
    assert projection["recent_call_audit"][0]["tool"] == "echo"

    await service.disable_server("remote")
    with pytest.raises(McpServerStateError, match="disabled"):
        await service.restart_server("remote")


async def test_remote_secret_entry_persists_only_credential_handle_in_config(
    tmp_path: Path,
) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    manager = RemoteManager()
    service = McpService(manager, ToolBroker(), McpConfiguration(paths), CredentialStore(paths))
    secret = "credential-value-never-in-config"

    await service.add_remote_with_secret(
        "remote", "https://mcp.example.test/mcp", "remote-handle", secret
    )

    assert CredentialStore(paths).load(environ={})["remote-handle"] == secret
    config_text = paths.config.read_text(encoding="utf-8")
    assert "remote-handle" in config_text
    assert secret not in config_text
