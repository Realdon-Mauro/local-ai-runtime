import os

from lar.evaluation.seccomp import (
    DENIED_SYSCALLS,
    create_seccomp_fd,
)


def test_denylist_contains_critical_syscalls():
    expected = {
        "ptrace",
        "bpf",
        "keyctl",
        "setns",
        "unshare",
        "mount",
        "init_module",
        "io_uring_setup",
        "userfaultfd",
    }

    assert expected.issubset(set(DENIED_SYSCALLS))


def test_create_seccomp_fd_exports_valid_bpf():
    fd = create_seccomp_fd()

    try:
        stat = os.fstat(fd)
        assert stat.st_size > 0
        assert stat.st_size % 8 == 0

        data = os.read(fd, stat.st_size)
        assert len(data) == stat.st_size
    finally:
        os.close(fd)
