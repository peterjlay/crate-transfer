from __future__ import annotations

from .protocol import NATIVE_DESTINATIONS


def compatibility_warnings(source: str, destination: str) -> list[str]:
    warnings = []
    if source == "serato" and destination == "serato3":
        warnings.append(
            "Version downgrade: Serato DJ 4+ SQLite to Serato DJ 3 legacy crates. Only crate membership and "
            "media are converted; SQLite revisions, database-only metadata and analysis cannot be carried "
            "back to DJ 3. DJ 3 must scan/reload the media. This is not a database downgrade."
        )
    elif source == "serato3" and destination == "serato":
        warnings.append(
            "Version upgrade: Serato DJ 3 legacy crates to Serato DJ 4+ SQLite. New destination-native crates "
            "are created, not an in-place library upgrade. DJ 3 database-only metadata and analysis are not "
            "imported; use a supported DJ 4 SQLite schema and verify playback."
        )
    if destination in ("serato", "serato3"):
        warnings.append(
            "Serato compatibility: the DJ 3 legacy and DJ 4+ SQLite destinations are separate. Choose the "
            "version actually used on the receiving computer; copying one version's database does not "
            "convert it for the other. Embedded media tags remain unchanged; unsupported codecs may need "
            "conversion outside this tool."
        )
    elif destination in NATIVE_DESTINATIONS:
        warnings.append(
            "Version compatibility: the installed destination DJ-app version is not determined by this tool. "
            "Its configured library schema/runtime is checked before import; unsupported schemas are refused. "
            "Newer-to-older libraries are not downgraded, and older-to-newer libraries are not upgraded in place. "
            "Unsupported metadata/analysis may be omitted; verify playback in the destination version."
        )
    elif destination != "m3u":
        warnings.append(
            "Exchange-version compatibility: XML/NML output uses the supported exchange format, not a native "
            "database upgrade/downgrade. Older DJ versions may reject fields or media; import and verify in "
            "the destination app. Unsupported metadata is reported in Activity."
        )
    return warnings


def add_compatibility_warnings(manifest: dict, source: str, destination: str) -> None:
    warnings = list(dict.fromkeys([*manifest.get("warnings", []), *compatibility_warnings(source, destination)]))
    if len(warnings) > 100:
        raise ValueError("Too many conversion warnings; choose fewer playlists.")
    manifest["source_format"] = source
    if warnings:
        manifest["warnings"] = warnings
