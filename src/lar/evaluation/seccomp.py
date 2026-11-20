"""Generazione di filtri seccomp BPF tramite libseccomp."""

import ctypes
import errno
import os


class SeccompError(RuntimeError):
    """Errore nella creazione del filtro seccomp."""


# Denylist iniziale per Linux x86_64.
# Il filtro usa EPERM per le syscall vietate e ALLOW per le altre.
DENIED_SYSCALLS = (
    # Debugging e accesso ad altri processi
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",

    # Kernel e funzionalità privilegiate
    "bpf",
    "keyctl",
    "add_key",
    "request_key",
    "perf_event_open",
    "userfaultfd",

    # Namespace e mount
    "setns",
    "unshare",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "mount_setattr",
    "move_mount",
    "open_tree",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",

    # Moduli e gestione kernel
    "init_module",
    "finit_module",
    "delete_module",
    "kexec_load",
    "kexec_file_load",
    "reboot",

    # Accesso diretto a dispositivi e risorse privilegiate
    "iopl",
    "ioperm",
    "swapon",
    "swapoff",
    "mknod",
    "mknodat",

    # API avanzate non necessarie ai benchmark
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "fanotify_init",
    "fanotify_mark",
    "open_by_handle_at",
    "name_to_handle_at",
)


class _LibSeccomp:
    """Wrapper minimo delle API libseccomp utilizzate."""

    def __init__(self) -> None:
        library = ctypes.util.find_library("seccomp")
        if library is None:
            raise SeccompError("libseccomp non è installata.")

        try:
            self.lib = ctypes.CDLL(library, use_errno=True)
        except OSError as exc:
            raise SeccompError(f"Impossibile caricare {library}: {exc}") from exc

        self.lib.seccomp_init.argtypes = [ctypes.c_uint32]
        self.lib.seccomp_init.restype = ctypes.c_void_p

        self.lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        self.lib.seccomp_syscall_resolve_name.restype = ctypes.c_int

        self.lib.seccomp_rule_add.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        self.lib.seccomp_rule_add.restype = ctypes.c_int

        self.lib.seccomp_export_bpf.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.lib.seccomp_export_bpf.restype = ctypes.c_int

        self.lib.seccomp_release.argtypes = [ctypes.c_void_p]
        self.lib.seccomp_release.restype = None


# Caricamento ritardato: importare il modulo non richiede libseccomp.
import ctypes.util


def create_seccomp_fd() -> int:
    """Crea un memfd contenente il filtro BPF e restituisce il suo FD.

    Il chiamante è responsabile della chiusura del file descriptor.
    Il filtro consente tutte le syscall non presenti nella denylist.
    """

    seccomp = _LibSeccomp()

    SCMP_ACT_ALLOW = 0x7FFF0000
    SCMP_ACT_ERRNO = 0x00050000 | errno.EPERM

    context = seccomp.lib.seccomp_init(SCMP_ACT_ALLOW)
    if not context:
        raise SeccompError("seccomp_init non è riuscita.")

    fd = -1

    try:
        for name in DENIED_SYSCALLS:
            number = seccomp.lib.seccomp_syscall_resolve_name(
                name.encode("ascii")
            )

            if number < 0:
                raise SeccompError(
                    f"Syscall non riconosciuta da libseccomp: {name}"
                )

            result = seccomp.lib.seccomp_rule_add(
                context,
                SCMP_ACT_ERRNO,
                number,
                0,
            )

            if result != 0:
                raise SeccompError(
                    f"Impossibile aggiungere la regola {name}: "
                    f"{os.strerror(-result)}"
                )

        if not hasattr(os, "memfd_create"):
            raise SeccompError("memfd_create non è disponibile.")

        fd = os.memfd_create("lar-seccomp-filter", flags=0)

        result = seccomp.lib.seccomp_export_bpf(context, fd)
        if result != 0:
            raise SeccompError(
                f"Esportazione BPF fallita: {os.strerror(-result)}"
            )

        os.lseek(fd, 0, os.SEEK_SET)
        return fd

    except Exception:
        if fd >= 0:
            os.close(fd)
        raise

    finally:
        seccomp.lib.seccomp_release(context)
