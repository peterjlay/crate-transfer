from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from .protocol import validate_manifest


METADATA_WARNING = (
    "M3U export preserves playlist hierarchy, track order and original media bytes. "
    "It does not convert database-only cues, loops, BPM/key or beatgrids, and is not OneLibrary."
)


class ExportError(ValueError):
    pass


def safe_component(name: str, maximum_bytes=180) -> str:
    safe = "".join("_" if c in '/\\<>:\"|?*' else c for c in name).rstrip(". ")
    safe = safe.encode("utf-8")[:maximum_bytes].decode("utf-8", errors="ignore")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if not safe or safe.startswith(".") or safe.split(".")[0].upper() in reserved:
        safe = "_" + safe
    return safe


def export_bundle(manifest: dict, sources: dict[str, Path], directory: Path,
                  export_id: str, progress=lambda text: None, cancelled=lambda: False,
                  format="m3u") -> dict:
    validate_manifest(manifest)
    if format not in ("m3u", "rekordbox-xml", "traktor-nml"):
        raise ExportError("Unsupported playlist export format.")
    if len(export_id) != 32 or any(c not in "0123456789abcdef" for c in export_id):
        raise ExportError("Invalid export identity.")
    if set(sources) != {item["id"] for item in manifest["files"]}:
        raise ExportError("Export media does not match the selected playlists.")
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise ExportError("Choose an existing export folder or mounted USB volume.")
    required = sum(item["size"] for item in manifest["files"])
    if shutil.disk_usage(directory).free < required + 256 * 1024**2:
        raise ExportError("Not enough free space for export files and a 256 MiB safety margin.")
    destination = directory / f"Crates-{export_id}"
    if destination.exists() or destination.is_symlink():
        raise ExportError("An export with this identity already exists. Nothing was overwritten.")
    staging = Path(tempfile.mkdtemp(prefix=".crate-export-", dir=directory))
    reserved = False
    bundle = destination / "Export"

    def check_cancelled():
        if cancelled():
            raise ExportError("Export cancelled. No output was published.")

    try:
        music = staging / "Music"
        music.mkdir()
        media_paths, used_names = {}, set()
        for item in manifest["files"]:
            check_cancelled()
            original = Path(item["name"])
            name, number = item["name"], 2
            while name.casefold() in used_names:
                name = f"{original.stem[:140]} ({number}){original.suffix}"
                number += 1
            used_names.add(name.casefold())
            target = music / name
            progress(f"Copying {item['name']}")
            digest, size = hashlib.sha256(), 0
            with Path(sources[item["id"]]).open("rb") as source, target.open("xb") as output:
                while True:
                    check_cancelled()
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > item["size"]:
                        raise ExportError(f"Source file changed during export: {item['name']}")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
                raise ExportError(f"Source file changed or checksum failed: {item['name']}")
            media_paths[item["id"]] = target
        playlists = staging / "Playlists"
        playlists.mkdir()
        folders, sibling_names = {(): playlists}, {}
        for crate in manifest["crates"]:
            check_cancelled()
            key = ()
            for part in crate["path"]:
                child = key + (part,)
                if child not in folders:
                    used = sibling_names.setdefault(key, set())
                    original = safe_component(part, maximum_bytes=160)
                    name, number = original, 2
                    while name.casefold() in used:
                        name = f"{original} ({number})"
                        number += 1
                    used.add(name.casefold())
                    folders[child] = folders[key] / name
                    folders[child].mkdir()
                key = child
            folder = folders[key]
            with (folder / "playlist.m3u8").open("x", encoding="utf-8", newline="\n") as output:
                output.write("#EXTM3U\n")
                for file_id in crate["tracks"]:
                    relative = os.path.relpath(media_paths[file_id], folder).replace(os.sep, "/")
                    output.write(relative + "\n")
                output.flush()
                os.fsync(output.fileno())
        check_cancelled()
        if format != "m3u":
            from .exchange import write_exchange
            final_paths = {key: bundle / path.relative_to(staging) for key, path in media_paths.items()}
            exchange_file = write_exchange(format, manifest, final_paths, staging)
            with exchange_file.open("r+b") as output:
                os.fsync(output.fileno())
        # A unique directory is published only once all media and playlists verify.
        try:
            destination.mkdir()
        except FileExistsError as error:
            raise ExportError("Export destination appeared during export. Nothing was overwritten.") from error
        reserved = True
        staging.rename(bundle)
        reserved = False
        warnings = list(manifest.get("warnings", []))
        if format == "m3u":
            warnings.append(METADATA_WARNING)
        else:
            warnings.append("Exchange file created. Import it in the destination DJ app; this is not "
                            "automatic native integration or a OneLibrary USB. Do not relocate this "
                            "bundle before import: XML/NML references its destination-local paths.")
        return {"format": format, "crates": len(manifest["crates"]), "files": len(manifest["files"]),
                "memberships": sum(len(crate["tracks"]) for crate in manifest["crates"]),
                "output": str(bundle), "warnings": warnings}
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if reserved:
            destination.rmdir()
