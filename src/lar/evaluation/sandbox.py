import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .seccomp import SeccompError, create_seccomp_fd

logger = logging.getLogger(__name__)


class SandboxError(RuntimeError):
    """Errore nella configurazione o nell'esecuzione della sandbox."""


@dataclass(frozen=True)
class SandboxConfig:
    """Limiti e percorsi degli strumenti di isolamento."""

    bwrap_path: str = "/usr/bin/bwrap"
    systemd_run_path: str = "/usr/bin/systemd-run"
    systemctl_path: str = "/usr/bin/systemctl"
    prlimit_path: str = "/usr/bin/prlimit"
    python_path: str = "/usr/bin/python3.14"

    memory_max: str = "512M"
    cpu_quota: str = "100%"
    tasks_max: int = 32

    file_size_bytes: int = 10 * 1024 * 1024
    open_files: int = 64
    cpu_time_grace_seconds: int = 2

    def __post_init__(self) -> None:
        """Valida la configurazione prima di avviare la sandbox."""

        path_fields = (
            "bwrap_path",
            "systemd_run_path",
            "systemctl_path",
            "prlimit_path",
            "python_path",
        )

        for field_name in path_fields:
            value = getattr(self, field_name)

            if (
                not isinstance(value, str)
                or not value.strip()
                or "\\x00" in value
                or not Path(value).is_absolute()
            ):
                raise SandboxError(
                    f"Percorso non valido per {field_name}: {value!r}"
                )

        if (
            not isinstance(self.memory_max, str)
            or re.fullmatch(r"[1-9][0-9]*[KMGTPE]", self.memory_max) is None
        ):
            raise SandboxError("memory_max deve essere una quantità positiva, ad esempio 512M.")

        quota_match = (
            re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)%", self.cpu_quota)
            if isinstance(self.cpu_quota, str)
            else None
        )

        if quota_match is None or not 1 <= float(quota_match.group(1)) <= 100000:
            raise SandboxError("cpu_quota deve essere una percentuale tra 1% e 100000%.")

        positive_fields = (
            "tasks_max",
            "file_size_bytes",
            "open_files",
        )

        for field_name in positive_fields:
            value = getattr(self, field_name)

            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise SandboxError(f"{field_name} deve essere un intero positivo.")

        grace = self.cpu_time_grace_seconds

        if isinstance(grace, bool) or not isinstance(grace, int) or grace < 0:
            raise SandboxError(
                "cpu_time_grace_seconds deve essere un intero non negativo."
            )


@dataclass(frozen=True)
class SandboxResult:
    """Risultato di un'esecuzione isolata."""

    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    launch_error: str | None = None
    oom_killed: bool = False

    @property
    def passed(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and self.launch_error is None
            and not self.oom_killed
        )

    @property
    def status(self) -> str:
        if self.timed_out:
            return "timeout"
        if self.oom_killed:
            return "oom"
        if self.launch_error is not None:
            return "error"
        return "passed" if self.passed else "failed"


class SandboxCommandBuilder:
    """Costruisce il comando di esecuzione isolata."""

    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or SandboxConfig()

    def build(
        self,
        workspace: str | Path,
        timeout_seconds: int,
        unit_name: str,
        seccomp_fd: int,
    ) -> list[str]:
        """Costruisce il comando senza eseguirlo."""

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise SandboxError("Il timeout deve essere un intero positivo.")

        if not isinstance(unit_name, str) or not unit_name.endswith(".scope"):
            raise SandboxError("Il nome dell'unità systemd deve terminare con .scope.")

        if (
            isinstance(seccomp_fd, bool)
            or not isinstance(seccomp_fd, int)
            or seccomp_fd < 0
        ):
            raise SandboxError("Il file descriptor seccomp non è valido.")

        directory = Path(workspace).resolve()
        solution = directory / "solution.py"
        test_runner = directory / "test_runner.py"

        if not solution.is_file():
            raise SandboxError(f"File soluzione inesistente: {solution}")

        if not test_runner.is_file():
            raise SandboxError(f"Test runner inesistente: {test_runner}")

        cpu_limit = timeout_seconds + self.config.cpu_time_grace_seconds

        return [
            self.config.systemd_run_path,
            "--user",
            "--scope",
            "--quiet",
            f"--unit={unit_name}",
            f"--property=MemoryMax={self.config.memory_max}",
            "--property=MemoryAccounting=yes",
            "--property=OOMPolicy=kill",
            f"--property=CPUQuota={self.config.cpu_quota}",
            f"--property=TasksMax={self.config.tasks_max}",
            "--",
            self.config.prlimit_path,
            f"--cpu={cpu_limit}:{cpu_limit}",
            f"--fsize={self.config.file_size_bytes}:{self.config.file_size_bytes}",
            f"--nofile={self.config.open_files}:{self.config.open_files}",
            "--core=0:0",
            "--",
            self.config.bwrap_path,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "PYTHONNOUSERSITE",
            "1",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/etc",
            "/etc",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib",
            "/lib64",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--seccomp",
            str(seccomp_fd),
            "--tmpfs",
            "/workspace",
            "--ro-bind",
            str(solution),
            "/workspace/solution.py",
            "--ro-bind",
            str(test_runner),
            "/workspace/test_runner.py",
            "--remount-ro",
            "/workspace",
            "--chdir",
            "/workspace",
            self.config.python_path,
            "-B",
            "/workspace/test_runner.py",
        ]


class SandboxExecutor:
    """Esegue codice Python tramite systemd e Bubblewrap."""

    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or SandboxConfig()
        self.builder = SandboxCommandBuilder(self.config)

    async def _systemctl(self, *arguments: str) -> int:
        """Esegue un comando systemctl con un timeout interno."""

        try:
            process = await asyncio.create_subprocess_exec(
                self.config.systemctl_path,
                "--user",
                *arguments,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            logger.exception("Impossibile avviare systemctl.")
            return -1

        try:
            return await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
            return -1

    async def _unit_result(
        self,
        unit_name: str,
        wait_seconds: float = 5.0,
    ) -> str | None:
        """Attende la conclusione della scope e restituisce il risultato."""

        deadline = time.monotonic() + wait_seconds

        while True:
            try:
                process = await asyncio.create_subprocess_exec(
                    self.config.systemctl_path,
                    "--user",
                    "show",
                    "--property=ActiveState",
                    "--property=Result",
                    unit_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError:
                logger.exception("Impossibile interrogare lo scope %s.", unit_name)
                return None

            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=5,
                )
            except TimeoutError:
                process.kill()
                await process.communicate()
                return None

            if process.returncode != 0:
                return None

            properties = {}
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    properties[key] = value

            state = properties.get("ActiveState")
            result = properties.get("Result")

            if state in {"inactive", "failed"}:
                return result or None

            if time.monotonic() >= deadline:
                logger.warning(
                    "La scope %s non è terminata entro %.1f secondi (stato: %s).",
                    unit_name,
                    wait_seconds,
                    state,
                )
                return result or None

            await asyncio.sleep(0.1)

    async def _terminate_scope(self, unit_name: str) -> None:
        """Termina tutti i processi dello scope."""

        kill_status = await self._systemctl(
            "kill",
            "--kill-whom=all",
            "--signal=KILL",
            unit_name,
        )

        if kill_status != 0:
            stop_status = await self._systemctl("stop", unit_name)

            if stop_status != 0:
                raise SandboxError(
                    f"Impossibile terminare lo scope systemd: {unit_name}"
                )

    async def _wait_after_termination(
        self,
        process: asyncio.subprocess.Process,
        unit_name: str,
    ) -> None:
        """Verifica che il processo sia terminato dopo il kill."""

        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            await self._systemctl("stop", unit_name)

            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError as exc:
                raise SandboxError(
                    f"Il processo dello scope {unit_name} non è terminato."
                ) from exc

    async def _scope_is_inactive(self, unit_name: str) -> bool:
        """Verifica ripetutamente che systemd abbia terminato lo scope."""

        for attempt in range(10):
            try:
                process = await asyncio.create_subprocess_exec(
                    self.config.systemctl_path,
                    "--user",
                    "show",
                    "--property=ActiveState",
                    unit_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError:
                logger.exception("Impossibile verificare lo scope %s.", unit_name)
                return False

            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=5,
                )
            except TimeoutError:
                process.kill()
                await process.communicate()
                return False

            if process.returncode == 0:
                properties = {}
                for line in stdout.decode("utf-8", errors="replace").splitlines():
                    key, separator, value = line.partition("=")
                    if separator:
                        properties[key] = value

                if properties.get("ActiveState") in {"inactive", "failed"}:
                    return True

            if attempt < 9:
                await asyncio.sleep(0.2)

        return False

    async def _terminate_and_confirm(
        self,
        process: asyncio.subprocess.Process,
        unit_name: str,
    ) -> bool:
        """Termina lo scope e verifica che systemd lo abbia arrestato."""

        await self._terminate_scope(unit_name)
        await self._wait_after_termination(process, unit_name)
        return await self._scope_is_inactive(unit_name)

    async def execute(
        self,
        solution_code: str,
        test_code: str,
        timeout_seconds: int,
    ) -> SandboxResult:
        """Esegue una soluzione e conserva la workspace se lo scope resta attivo."""

        if not isinstance(solution_code, str) or not solution_code.strip():
            raise SandboxError("Il codice soluzione non può essere vuoto.")

        if not isinstance(test_code, str) or not test_code.strip():
            raise SandboxError("Il codice dei test non può essere vuoto.")

        unit_name = f"lar-bench-{uuid.uuid4().hex}.scope"
        workspace = Path(tempfile.mkdtemp(prefix="lar-bench-"))
        preserve_workspace = False
        process = None
        started_at = time.monotonic()

        try:
            solution = workspace / "solution.py"
            test_runner = workspace / "test_runner.py"
            stdout_path = workspace / "stdout.log"
            stderr_path = workspace / "stderr.log"

            solution.write_text(solution_code, encoding="utf-8")
            test_runner.write_text(test_code, encoding="utf-8")

            with (
                stdout_path.open("wb") as stdout_file,
                stderr_path.open("wb") as stderr_file,
            ):
                try:
                    seccomp_fd = create_seccomp_fd()
                except (SeccompError, OSError) as exc:
                    return SandboxResult(
                        returncode=None,
                        stdout="",
                        stderr="",
                        duration_seconds=time.monotonic() - started_at,
                        launch_error=f"Seccomp: {exc}",
                    )

                try:
                    command = self.builder.build(
                        workspace,
                        timeout_seconds,
                        unit_name,
                        seccomp_fd=seccomp_fd,
                    )

                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        pass_fds=(seccomp_fd,),
                    )
                except OSError as exc:
                    return SandboxResult(
                        returncode=None,
                        stdout="",
                        stderr="",
                        duration_seconds=time.monotonic() - started_at,
                        launch_error=str(exc),
                    )
                finally:
                    os.close(seccomp_fd)

                timed_out = False

                try:
                    await asyncio.wait_for(
                        process.wait(),
                        timeout=timeout_seconds,
                    )

                except TimeoutError:
                    timed_out = True
                    try:
                        confirmed = await self._terminate_and_confirm(
                            process,
                            unit_name,
                        )
                    except (SandboxError, OSError) as exc:
                        preserve_workspace = True
                        raise SandboxError(
                            f"Terminazione non confermata per {unit_name}. "
                            f"Workspace conservata in: {workspace}"
                        ) from exc

                    if not confirmed:
                        preserve_workspace = True
                        raise SandboxError(
                            f"Systemd non conferma la terminazione di {unit_name}. "
                            f"Workspace conservata in: {workspace}"
                        )

                except asyncio.CancelledError:
                    preserve_workspace = True
                    cleanup = asyncio.create_task(
                        self._terminate_and_confirm(process, unit_name)
                    )
                    try:
                        confirmed = await asyncio.shield(cleanup)
                    except (SandboxError, OSError):
                        logger.exception(
                            "Errore durante la pulizia dello scope %s. "
                            "Workspace conservata in: %s",
                            unit_name,
                            workspace,
                        )
                    else:
                        if confirmed:
                            preserve_workspace = False
                        else:
                            logger.error(
                                "Terminazione non confermata dopo cancellazione. "
                                "Workspace conservata in: %s",
                                workspace,
                            )
                    raise

                if not await self._scope_is_inactive(unit_name):
                    preserve_workspace = True
                    raise SandboxError(
                        f"Impossibile confermare che {unit_name} sia inattivo. "
                        f"Workspace conservata in: {workspace}"
                    )

                unit_result = await self._unit_result(unit_name)
                oom_killed = unit_result == "oom-kill"
                duration = time.monotonic() - started_at

            stdout = stdout_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            stderr = stderr_path.read_text(
                encoding="utf-8",
                errors="replace",
            )

            return SandboxResult(
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                timed_out=timed_out,
                oom_killed=oom_killed,
            )

        finally:
            if preserve_workspace:
                logger.error(
                    "Workspace non eliminata per sicurezza: %s",
                    workspace,
                )
            else:
                shutil.rmtree(workspace)
