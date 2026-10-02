"""Safe reads for the model-facing OptMem import tool.

The user-driven CLI import accepts an explicit path. Model-provided imports
are limited to a single filename in ``<HERMES_HOME>/optmem/imports``.
"""

from __future__ import annotations

import ntpath
import os
import stat

MAX_IMPORT_BYTES = 4 * 1024 * 1024


def _is_within(path: str, directory: str) -> bool:
    path = os.path.normcase(os.path.realpath(path))
    directory = os.path.normcase(os.path.realpath(directory))
    try:
        return os.path.commonpath((path, directory)) == directory
    except ValueError:
        return False


def _validate_filename(filename: object) -> str:
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or "\x00" in filename
        or os.path.isabs(filename)
        or ntpath.isabs(filename)
        or os.path.basename(filename) != filename
        or ntpath.basename(filename) != filename
    ):
        raise ValueError(
            "model imports require a single file name inside "
            "<HERMES_HOME>/optmem/imports/"
        )
    return filename


def _read_fd(file_fd: int) -> bytes:
    try:
        source = os.fdopen(file_fd, "rb")
    except Exception:
        os.close(file_fd)
        raise
    with source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("model import source must be a regular file")
        if metadata.st_size > MAX_IMPORT_BYTES:
            raise ValueError("import file exceeds the 4 MiB size limit")
        data = source.read(MAX_IMPORT_BYTES + 1)
        if len(data) > MAX_IMPORT_BYTES:
            raise ValueError("import file exceeds the 4 MiB size limit")
        return data


def _read_posix(home: str, filename: str) -> bytes:
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("secure model imports are unavailable on this platform")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    descriptors: list[int] = []
    file_fd: int | None = None
    try:
        parent_fd = os.open(home, directory_flags)
        descriptors.append(parent_fd)
        optmem_fd = os.open("optmem", directory_flags, dir_fd=parent_fd)
        descriptors.append(optmem_fd)
        imports_fd = os.open("imports", directory_flags, dir_fd=optmem_fd)
        descriptors.append(imports_fd)
        file_fd = os.open(
            filename,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=imports_fd,
        )
        descriptor, file_fd = file_fd, None
        return _read_fd(descriptor)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _windows_final_path(file_fd: int) -> str:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    win_dll = ctypes.WinDLL  # type: ignore[attr-defined]
    win_error = ctypes.WinError  # type: ignore[attr-defined]
    get_last_error = ctypes.get_last_error  # type: ignore[attr-defined]
    kernel32 = win_dll("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final_path.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(file_fd)  # type: ignore[attr-defined]
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_final_path(handle, buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise win_error(get_last_error())
    path = buffer.value
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.realpath(path)


def _open_windows_regular_path_without_reparse(path: str) -> int:
    """Open the final path component itself, never following a reparse point."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    win_dll = ctypes.WinDLL  # type: ignore[attr-defined]
    win_error = ctypes.WinError  # type: ignore[attr-defined]
    get_last_error = ctypes.get_last_error  # type: ignore[attr-defined]
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        path,
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise win_error(get_last_error())
    try:
        info = FileAttributeTagInfo()
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise win_error(get_last_error())
        if info.attributes & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise ValueError("model import source must not be a symlink or reparse point")
        descriptor = msvcrt.open_osfhandle(  # type: ignore[attr-defined]
            int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
        handle = None
        return descriptor
    finally:
        if handle is not None:
            kernel32.CloseHandle(handle)


def _read_windows(home: str, filename: str) -> bytes:
    home = os.path.realpath(home)
    imports_dir = os.path.realpath(os.path.join(home, "optmem", "imports"))
    if not _is_within(imports_dir, home):
        raise ValueError("model import directory must remain inside <HERMES_HOME>")
    path = os.path.join(imports_dir, filename)
    file_fd = _open_windows_regular_path_without_reparse(path)
    try:
        actual_path = _windows_final_path(file_fd)
        if not _is_within(actual_path, imports_dir):
            raise ValueError("model import source must remain inside the imports directory")
        descriptor, file_fd = file_fd, -1
        return _read_fd(descriptor)
    finally:
        if file_fd >= 0:
            os.close(file_fd)


def read_model_import_lines(hermes_home: str | os.PathLike[str], filename: object) -> list[str]:
    """Read a bounded, regular, confined import file for the model tool."""
    name = _validate_filename(filename)
    home = os.path.realpath(os.fspath(hermes_home))
    data = _read_windows(home, name) if os.name == "nt" else _read_posix(home, name)
    return data.decode("utf-8").splitlines()
