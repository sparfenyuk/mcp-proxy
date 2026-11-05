"""Opt-in runtime checks for a locally built container image.

Run with MCP_PROXY_TEST_IMAGE set to a local image tag. Containers have no network
access, and fixture mounts are read-only.
"""

import io
import os
import shutil
import subprocess
import tarfile
import uuid
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


@pytest.fixture
def container_flavor() -> str | None:
    """Check flavor-specific behavior only when the caller selects a flavor."""
    flavor = os.environ.get("MCP_PROXY_TEST_FLAVOR")
    if flavor is not None:
        assert flavor in {"alpine", "debian", "debian-node"}
    return flavor


def run_container(
    image: str,
    entrypoint: str | None,
    *args: str,
    fixtures: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one disposable offline container with only explicit test arguments."""
    docker = shutil.which("docker")
    assert docker is not None, "Docker is required when MCP_PROXY_TEST_IMAGE is set"
    container_name = f"mcp-proxy-test-{uuid.uuid4().hex}"
    command = [docker, "run", "--name", container_name, "--pull=never", "--network=none"]
    if fixtures is not None:
        command += ["--mount", f"type=bind,source={fixtures},target=/fixtures,readonly"]
    if entrypoint is not None:
        command += ["--entrypoint", entrypoint]
    command += [image, *args]
    # The command is a resolved Docker executable; no shell or host environment is forwarded.
    timed_out = False
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
    except subprocess.TimeoutExpired:
        timed_out = True
        raise
    finally:
        # A timed-out Docker CLI can leave its uniquely named container running in the daemon.
        inspection = subprocess.run(  # noqa: S603
            [docker, "inspect", "--format", "{{.Id}} {{.Name}} {{.State.Status}}", container_name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if inspection.returncode == 0:
            container_id, identified_name, state = inspection.stdout.strip().split()
            assert identified_name == f"/{container_name}", inspection.stdout
            cleanup = subprocess.run(  # noqa: S603
                [docker, "rm", "--force", container_id],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert cleanup.returncode == 0, (
                f"Could not remove test container {container_id} ({state}): {cleanup.stderr}"
            )
            if timed_out and state == "running":
                timeout_message = (
                    f"Docker CLI timed out; test container {container_id} was running "
                    "and forcibly removed"
                )
                raise AssertionError(timeout_message)


def test_container_default_entrypoint_help(container_image: str) -> None:
    """The image's configured supervisor and mcp-proxy entrypoint remain usable."""
    result = run_container(container_image, None, "--help")
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_container_runtime_flavor(container_image: str, container_flavor: str | None) -> None:
    """An explicitly selected image flavor has the expected libc distribution."""
    if container_flavor is None:
        pytest.skip("Set MCP_PROXY_TEST_FLAVOR to check the image's runtime flavor")
    expected_id = "alpine" if container_flavor == "alpine" else "debian"
    result = run_container(container_image, "cat", "/etc/os-release")
    assert result.returncode == 0, result.stderr
    assert f"ID={expected_id}" in result.stdout.splitlines()


def test_container_python_runtime(container_image: str) -> None:
    """The venv metadata, runtime interpreter, and base image use one Python version."""
    code = (
        "from configparser import ConfigParser\n"
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "config = ConfigParser()\n"
        "config.read_string('[venv]\\n' + Path('/app/.venv/pyvenv.cfg').read_text())\n"
        "print('.'.join(map(str, sys.version_info[:3])))\n"
        "print(config['venv']['version_info'])\n"
        "print(os.environ['PYTHON_VERSION'])\n"
    )
    result = run_container(container_image, "/app/.venv/bin/python", "-c", code)
    assert result.returncode == 0, result.stderr
    runtime, venv, image = result.stdout.splitlines()
    assert runtime.startswith("3.13.")
    assert runtime == venv == image


def test_container_node_tools(container_image: str, container_flavor: str | None) -> None:
    """The debian-node flavor exposes Node.js and both npm launchers."""
    if container_flavor != "debian-node":
        pytest.skip("Set MCP_PROXY_TEST_FLAVOR=debian-node to check Node.js tools")
    for executable in ("node", "npm", "npx"):
        result = run_container(container_image, executable, "--version")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip()
        if executable == "node":
            assert result.stdout.startswith("v24.")


def test_container_npx_runs_local_tool_offline(
    container_image: str, container_flavor: str | None, tmp_path: Path
) -> None:
    """Npx installs and launches a local dependency-free package without a registry."""
    if container_flavor != "debian-node":
        pytest.skip("Set MCP_PROXY_TEST_FLAVOR=debian-node to check offline npx")
    package = tmp_path / "container-smoke-tool.tgz"
    files = (
        (
            "package/package.json",
            b'{"name":"container-smoke-tool","version":"1.0.0",'
            b'"bin":{"container-smoke-tool":"bin/container-smoke-tool.js"}}',
            0o644,
        ),
        (
            "package/bin/container-smoke-tool.js",
            b'#!/usr/bin/env node\nconsole.log("offline-node-tool-ok")\n',
            0o755,
        ),
    )
    with tarfile.open(package, "w:gz") as archive:
        for name, contents, mode in files:
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            member.mode = mode
            archive.addfile(member, io.BytesIO(contents))
    result = run_container(
        container_image,
        "npx",
        "--offline",
        "--yes",
        "--package=file:/fixtures/container-smoke-tool.tgz",
        "container-smoke-tool",
        fixtures=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "offline-node-tool-ok"


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
