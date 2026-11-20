from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lar.evaluation.sandbox import (
    SandboxCommandBuilder,
    SandboxConfig,
    SandboxError,
    SandboxExecutor,
)


@pytest.mark.parametrize(
    "field,value",
    [
        ("bwrap_path", ""),
        ("bwrap_path", "bwrap"),
        ("python_path", "/tmp/python\\x00"),
        ("memory_max", "0M"),
        ("memory_max", "512"),
        ("memory_max", "512MB"),
        ("cpu_quota", "0%"),
        ("cpu_quota", "-10%"),
        ("cpu_quota", "abc"),
        ("tasks_max", 0),
        ("tasks_max", -1),
        ("tasks_max", True),
        ("file_size_bytes", 0),
        ("open_files", False),
        ("cpu_time_grace_seconds", -1),
        ("cpu_time_grace_seconds", True),
    ],
)
def test_invalid_sandbox_configuration_is_rejected(field, value):
    with pytest.raises(SandboxError):
        SandboxConfig(**{field: value})


@pytest.mark.parametrize("cpu_quota", ["1%", "50%", "100%", "125.5%", "100000%"])
def test_valid_cpu_quota_is_accepted(cpu_quota):
    config = SandboxConfig(cpu_quota=cpu_quota)
    assert config.cpu_quota == cpu_quota


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "solution.py").write_text(
        "def example(): return 42\n",
        encoding="utf-8",
    )
    (tmp_path / "test_runner.py").write_text(
        "print('test')\n",
        encoding="utf-8",
    )
    return tmp_path


def test_build_command_uses_expected_execution_layers(workspace):
    command = SandboxCommandBuilder().build(
        workspace,
        timeout_seconds=10,
        unit_name="lar-bench-test.scope",
            seccomp_fd=99,
    )

    assert command[0] == "/usr/bin/systemd-run"
    assert command[command.index("--seccomp") + 1] == "99"
    assert command.index("/usr/bin/prlimit") < command.index("/usr/bin/bwrap")
    assert command[-3:] == [
        "/usr/bin/python3.14",
        "-B",
        "/workspace/test_runner.py",
    ]


def test_build_command_applies_resource_limits(workspace):
    command = SandboxCommandBuilder().build(
        workspace,
        timeout_seconds=10,
        unit_name="lar-bench-test.scope",
            seccomp_fd=99,
    )

    assert "--property=MemoryMax=512M" in command
    assert "--property=CPUQuota=100%" in command
    assert "--property=TasksMax=32" in command
    assert "--cpu=12:12" in command
    assert "--fsize=10485760:10485760" in command
    assert "--nofile=64:64" in command
    assert "--core=0:0" in command


def test_build_command_mounts_workspace_files_read_only(workspace):
    command = SandboxCommandBuilder().build(
        workspace,
        timeout_seconds=10,
        unit_name="lar-bench-test.scope",
            seccomp_fd=99,
    )

    assert [
        "--ro-bind",
        str(workspace / "solution.py"),
        "/workspace/solution.py",
    ] == command[
        command.index(str(workspace / "solution.py")) - 1:
        command.index(str(workspace / "solution.py")) + 2
    ]

    assert [
        "--ro-bind",
        str(workspace / "test_runner.py"),
        "/workspace/test_runner.py",
    ] == command[
        command.index(str(workspace / "test_runner.py")) - 1:
        command.index(str(workspace / "test_runner.py")) + 2
    ]

    assert "--unshare-all" in command
    assert "--die-with-parent" in command
    assert "--new-session" in command


@pytest.mark.parametrize("timeout", [0, -1, True, 1.5, "10"])
def test_invalid_timeout_is_rejected(workspace, timeout):
    with pytest.raises(SandboxError, match="timeout"):
        SandboxCommandBuilder().build(
            workspace,
            timeout_seconds=timeout,
            unit_name="lar-bench-test.scope",
            seccomp_fd=99,
        )


def test_invalid_systemd_unit_name_is_rejected(workspace):
    with pytest.raises(SandboxError, match="scope"):
        SandboxCommandBuilder().build(
            workspace,
            timeout_seconds=10,
            unit_name="invalid-unit",
            seccomp_fd=99,
        )


def test_missing_solution_is_rejected(tmp_path):
    (tmp_path / "test_runner.py").write_text(
        "print('test')\n",
        encoding="utf-8",
    )

    with pytest.raises(SandboxError, match="solution.py"):
        SandboxCommandBuilder().build(
            tmp_path,
            timeout_seconds=10,
            unit_name="lar-bench-test.scope",
            seccomp_fd=99,
        )


def test_missing_test_runner_is_rejected(tmp_path):
    (tmp_path / "solution.py").write_text(
        "def example(): return 42\n",
        encoding="utf-8",
    )

    with pytest.raises(SandboxError, match="test_runner.py"):
        SandboxCommandBuilder().build(
            tmp_path,
            timeout_seconds=10,
            unit_name="lar-bench-test.scope",
            seccomp_fd=99,
        )


def test_custom_configuration_is_used(workspace):
    config = SandboxConfig(
        memory_max="256M",
        cpu_quota="50%",
        tasks_max=16,
        file_size_bytes=1024,
        open_files=32,
        cpu_time_grace_seconds=5,
    )

    command = SandboxCommandBuilder(config).build(
        workspace,
        timeout_seconds=20,
        unit_name="lar-bench-test.scope",
            seccomp_fd=99,
    )

    assert "--property=MemoryMax=256M" in command
    assert "--property=CPUQuota=50%" in command
    assert "--property=TasksMax=16" in command
    assert "--cpu=25:25" in command
    assert "--fsize=1024:1024" in command
    assert "--nofile=32:32" in command



@pytest.mark.parametrize("fd", [-1, True, 1.5, "99"])
def test_invalid_seccomp_fd_is_rejected(workspace, fd):
    with pytest.raises(SandboxError, match="descriptor seccomp"):
        SandboxCommandBuilder().build(
            workspace,
            timeout_seconds=10,
            unit_name="lar-bench-test.scope",
            seccomp_fd=fd,
        )



async def test_systemctl_start_error_returns_failure(monkeypatch):
    monkeypatch.setattr(
        "lar.evaluation.sandbox.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=OSError("systemctl non disponibile")),
    )

    executor = SandboxExecutor()

    result = await executor._systemctl("stop", "lar-bench-test.scope")

    assert result == -1


async def test_unit_result_start_error_returns_none(monkeypatch):
    monkeypatch.setattr(
        "lar.evaluation.sandbox.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=OSError("systemctl non disponibile")),
    )

    executor = SandboxExecutor()

    result = await executor._unit_result("lar-bench-test.scope")

    assert result is None


async def test_execute_preserves_workspace_when_termination_fails(monkeypatch):
    import shutil
    import tempfile

    from lar.evaluation import sandbox

    real_mkdtemp = tempfile.mkdtemp
    workspaces = []

    def tracked_mkdtemp(*args, **kwargs):
        directory = Path(real_mkdtemp(*args, **kwargs))
        workspaces.append(directory)
        return str(directory)

    class RunningProcess:
        returncode = None

        async def wait(self):
            raise TimeoutError

    process = RunningProcess()

    monkeypatch.setattr(
        sandbox.tempfile,
        "mkdtemp",
        tracked_mkdtemp,
    )
    monkeypatch.setattr(sandbox, "create_seccomp_fd", lambda: 123)
    monkeypatch.setattr(sandbox.os, "close", lambda fd: None)
    monkeypatch.setattr(
        sandbox.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )

    executor = SandboxExecutor()
    executor._systemctl = AsyncMock(return_value=-1)

    try:
        with pytest.raises(SandboxError, match="Workspace conservata"):
            await executor.execute(
                solution_code="def example(): return 42",
                test_code="print('test')",
                timeout_seconds=1,
            )

        assert len(workspaces) == 1
        assert workspaces[0].is_dir()
        assert (workspaces[0] / "solution.py").is_file()
        assert (workspaces[0] / "test_runner.py").is_file()
    finally:
        for directory in workspaces:
            shutil.rmtree(directory, ignore_errors=True)


async def test_execute_preserves_workspace_after_cancel_when_termination_fails(
    monkeypatch,
):
    import shutil
    import tempfile

    from lar.evaluation import sandbox

    real_mkdtemp = tempfile.mkdtemp
    workspaces = []

    def tracked_mkdtemp(*args, **kwargs):
        directory = Path(real_mkdtemp(*args, **kwargs))
        workspaces.append(directory)
        return str(directory)

    class RunningProcess:
        returncode = None

        async def wait(self):
            await asyncio.Event().wait()

    import asyncio

    process = RunningProcess()
    started = asyncio.Event()

    async def fake_create_subprocess_exec(*args, **kwargs):
        started.set()
        return process

    monkeypatch.setattr(sandbox.tempfile, "mkdtemp", tracked_mkdtemp)
    monkeypatch.setattr(sandbox, "create_seccomp_fd", lambda: 123)
    monkeypatch.setattr(sandbox.os, "close", lambda fd: None)
    monkeypatch.setattr(
        sandbox.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    executor = SandboxExecutor()
    executor._terminate_and_confirm = AsyncMock(return_value=False)

    task = asyncio.create_task(
        executor.execute(
            solution_code="def example(): return 42",
            test_code="print('test')",
            timeout_seconds=30,
        )
    )

    try:
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(workspaces) == 1
        assert workspaces[0].is_dir()
        assert (workspaces[0] / "solution.py").is_file()
    finally:
        for directory in workspaces:
            shutil.rmtree(directory, ignore_errors=True)
