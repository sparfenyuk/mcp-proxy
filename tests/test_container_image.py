"""Opt-in runtime checks for a locally built container image.

Run with MCP_PROXY_TEST_IMAGE set to a local image tag. Containers have no network
access, and fixture mounts are read-only.
"""

import os
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

MCP_SERVER = """from mcp.server.fastmcp import FastMCP

server = FastMCP("container-smoke")

@server.tool()
def echo(message: str) -> str:
    return message

server.run()
"""

MCP_CHECK = """import asyncio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp_proxy.proxy_server import create_proxy_server

async def main():
    params = StdioServerParameters(
        command="uv",
        args=["run", "--offline", "--no-project", "--no-python-downloads",
              "--python", "/app/.venv/bin/python", "/fixtures/server.py"],
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as backend:
            proxy = await create_proxy_server(backend)
            async with create_connected_server_and_client_session(proxy) as client:
                tools = await client.list_tools()
                assert [tool.name for tool in tools.tools] == ["echo"]
                result = await client.call_tool("echo", {"message": "container-roundtrip"})
                assert not result.isError
                assert result.content[0].text == "container-roundtrip"
    print("container-roundtrip-ok")

asyncio.run(main())
"""


@pytest.fixture
def container_image() -> str:
    """Use a caller-built image without pulling or building during normal tests."""
    image = os.environ.get("MCP_PROXY_TEST_IMAGE")
    if not image:
        pytest.skip("Set MCP_PROXY_TEST_IMAGE to run container integration tests")
    return image


def run_container(
    image: str,
    entrypoint: str,
    *args: str,
    fixtures: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one disposable offline container with only explicit test arguments."""
    docker = shutil.which("docker")
    assert docker is not None, "Docker is required when MCP_PROXY_TEST_IMAGE is set"
    command = [docker, "run", "--rm", "--pull=never", "--network=none"]
    if fixtures is not None:
        command += ["--mount", f"type=bind,source={fixtures},target=/fixtures,readonly"]
    command += ["--entrypoint", entrypoint, image, *args]
    # The command is a resolved Docker executable; no shell or host environment is forwarded.
    return subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603


@pytest.mark.parametrize("executable", ["uv", "uvx"])
def test_container_uv_executables(container_image: str, executable: str) -> None:
    """Both launchers must be executable in the final runtime image."""
    result = run_container(container_image, executable, "--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(f"{executable} ")


def test_container_uvx_runs_a_local_tool(container_image: str, tmp_path: Path) -> None:
    """Install and launch a dependency-free local wheel without a package index."""
    wheel = tmp_path / "container_smoke_tool-1.0-py3-none-any.whl"
    dist_info = "container_smoke_tool-1.0.dist-info"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("container_smoke_tool.py", 'def main():\n    print("offline-tool-ok")\n')
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\nName: container-smoke-tool\nVersion: 1.0\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\ncontainer-smoke-tool = container_smoke_tool:main\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")

    result = run_container(
        container_image,
        "uvx",
        "--offline",
        "--no-python-downloads",
        "--python",
        "/usr/local/bin/python",
        "--from",
        f"/fixtures/{wheel.name}",
        "container-smoke-tool",
        fixtures=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "offline-tool-ok"


def test_container_uv_stdio_proxy_roundtrip(container_image: str, tmp_path: Path) -> None:
    """Launch an MCP server through uv and call its tool through the real proxy."""
    (tmp_path / "server.py").write_text(MCP_SERVER, encoding="utf-8")
    result = run_container(
        container_image,
        "/app/.venv/bin/python",
        "-c",
        MCP_CHECK,
        fixtures=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "container-roundtrip-ok"
