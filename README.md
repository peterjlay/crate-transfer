# Crate Transfer

A macOS menu-bar / Windows system-tray app for **manually sending selected
Serato DJ 4 crates and their music to another computer on your LAN**.
No cloud, accounts, folder watching, mirroring, or automatic synchronization.

## Run from source

Python 3.9-3.13, macOS 12+ or Windows 10/11:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[build]"
python -m crate_transfer
```

On Windows, use `python` instead of `python3`, and activate with
`.venv\Scripts\Activate.ps1` in PowerShell.

## Transfer a crate

1. Run the app on **both** computers. Allow local-network access and the
   receiver's firewall prompt on private networks.
2. In **Settings**, choose each computer's Serato **Library folder** and
   **Received music folder**. Start Serato DJ 4 once if the library does not
   exist yet. The app deliberately does not invent an empty Serato database.
   Selecting the parent `Serato` folder also works: the app resolves its
   `Library` subfolder and saves the actual database-folder path.
3. On the receiver, open **Computers → Allow pairing for 2 minutes**.
4. On the sender, select the discovered receiver and click **Pair selected
   receiver**, or use **Pair by address**, and enter the receiver's six-digit
   code. No fingerprint copying or comparison is required. Pair for the first
   time on a trusted private network.
5. **Quit Serato DJ on the receiver.** In **Send crates**, check the crates
   you want, choose that paired receiver, and click **Send selected crates**.
   Selecting a parent includes its descendants. No other crates are sent.
6. Follow **Activity** on either computer. When the receiver reports
   **Complete**, open Serato. Crates retain their original names, hierarchy and
   track order. If a crate name already exists, the new crate gets a number
   such as `Live Set (2)`; existing crates are never merged or replaced.

Pairing enables **both sending and receiving** on both computers with one code.
Update both apps first. Existing directional pairings can be upgraded by
pairing again once; no crates or music are sent during pairing.
After pairing, trusted computers can import without a prompt whenever the
receiver is running and Serato is closed. **Forget / revoke selected** removes
the local outgoing pairing and incoming permission. Remove the pairing on
the other computer too to revoke access there.

Closing the window keeps the agent in the menu bar / tray. Use its **Quit**
menu item to stop it. Add the packaged app to macOS **Login Items** or Windows
**Startup** if you want the agent available after signing in.

## Defaults and configuration

| Setting | macOS | Windows |
|---|---|---|
| Serato Library | `~/Library/Application Support/Serato/Library` | `%LOCALAPPDATA%\Serato\Library` |
| Incoming music | `~/Music/Serato Transfers` | `%USERPROFILE%\Music\Serato Transfers` |
| Agent settings, logs, backups | `~/Library/Application Support/CrateTransfer` | `%LOCALAPPDATA%\CrateTransfer` |
| Receiver | TCP 49783 | TCP 49783 |

Folder changes take effect for the next transfer. Name/port changes require
an app restart. Use the actual path if Windows Known Folders are redirected,
for example to OneDrive.
Each folder field has a **Copy** button that copies its complete path, even
when the field is scrolled or only part of its text is selected. Standard
text selection, Copy/Paste context menus, and keyboard shortcuts also work.

The sender's absolute paths are **not sent to or reused on the receiver**.
A received file is stored as:

```text
<received-music-folder>/<crate>/<subcrate>/<original filename>
```

That machine's canonical, volume-relative path is written into Serato's
`asset.portable_id`. macOS leading slashes and Windows drive prefixes are
handled locally. Files referenced by multiple crates are stored once under the
first referencing crate. Invalid folder characters are replaced with underscores.
Identical files in the same folder are reused after checksum verification;
different songs with the same filename receive a numbered filename instead of
overwriting existing music. Subsequent transfers create separate numbered
crates, **not merges or syncs**. Earlier hash-named folders are left untouched
so their existing Serato paths remain valid.

## Serato 4 support and safety

This is a **first release with a schema-checked Serato 4 adapter**, based on
publicly documented reverse engineering. It is not affiliated with Serato.
It does not rely on legacy `Subcrates/*.crate` exports.

- Browses `master.sqlite` read-only, including volume mappings from
  `connection.database_uri`. A SQLite read transaction keeps the exported
  crate selection/membership consistent. The source library is never modified.
  Supports the aggregate's `container_asset.location_container_id →
  location_container.container_id` membership mapping, as well as direct
  `container_id` layouts; both browsing counts and exported track order use
  the same validated mapping.
  If SQLite cannot open the library read-only, the app requires Serato to be
  closed and reads a private temporary snapshot instead. Database and existing
  WAL/rollback journals are copied together, source changes during copying are
  rejected, and query-only access is enforced after journal recovery.
- Receives into the authoritative **`root.sqlite`**, not the aggregate cache.
  Writes `container → container_asset → space_asset → asset`, preserving
  crate hierarchy and track order, then advances the global and space
  revisions. Serato rebuilds its aggregate on next launch.
- Checks the running Serato DJ Pro/Lite process before reception, before
  import, inside the write transaction, and before commit. Do not start
  Serato until the transfer is complete.
- Makes SQLite Backup API snapshots of **both** databases, verifies backup
  integrity, uses a single `BEGIN IMMEDIATE` transaction, and checks foreign
  keys and SQLite integrity before committing.
- Verifies every music file's size and SHA-256 before importing. Transfers
  stream files in bounded chunks rather than loading music into memory.
- Never replaces an existing crate or differing music file. Reused music is
  checksum-verified. Original file bytes, including embedded tags, cues and
  beatgrids, remain unchanged. Serato can analyze newly imported tracks;
  database-only metadata, history and preferences are not copied.
- Uses TLS 1.2+, persistent certificate pinning, two-minute single-use pairing
  codes (five attempts), and revocable per-computer tokens. Incoming tokens
  are stored hashed; outgoing tokens and private keys live in the local
  user settings folder. Do not expose the port to the internet or share this
  folder.
- Discovery uses Bonjour/mDNS (`_seratocrate._tcp.local.`, UDP 5353) on IPv4 LANs.
  Discovery announcements are **not trust**. During explicit code pairing, the
  app learns the certificate directly from the selected address, verifies it
  again before sending the code, and saves its fingerprint only after success.
  Future transfers require that saved certificate; a mismatch is never silently
  accepted. This trust-on-first-use flow does not protect initial pairing from
  an active network impersonator relaying the code. Pair only on a trusted
  private network. Manual addressing works if multicast is blocked.

### First-release limits

- **Received music must be on the same local volume as the receiver's
  Serato Library.** Writing external-drive/network location databases is not
  implemented; those destinations are rejected rather than written incorrectly.
  Source external-drive music is supported when Serato's aggregate has a
  resolvable local volume mapping and its files are mounted.
- Normal local-file crates only. Smart-crate rules, streaming subscriptions,
  stems sidecars and DRM-protected media are not migrated.
- Supported filename extensions: MP3, FLAC, WAV, AIFF/AIF, M4A, AAC, OGG,
  ALAC, MP4 and MPEG video (MPG/MPEG). Video bytes are transferred unchanged,
  not transcoded; playback still requires Serato Video and a compatible codec.
  Filenames must be portable to Windows. Invalid names,
  missing files and unknown schemas fail explicitly.
- Per transfer: up to 5,000 crates, 20,000 distinct files, 200,000 memberships,
  32 hierarchy levels, 20 GiB per file, 500 GiB total, and an 8 MiB manifest.
- One incoming transfer at a time; uploads are sequential. Cancellation works
  before import; it does not interrupt an active database transaction.
- No automatic retry/resume. A lost final acknowledgement can mean the
  receiver imported successfully: check its **Activity** before resending.
  Interrupted imports are marked **Uncertain**, never success.
- macOS Apple Silicon bundle launch, Bonjour discovery and a complete synthetic
  music/crate import into the packaged receiver are verified in development.
  **No real Serato library or second physical computer was
  used for validation.** Windows paths and packaging are implemented with
  macOS/Windows CI configured, but a Windows executable has not been run
  here. Treat this as a preview; first test against a disposable library copy.

### Backup and restore

If opening the library fails, the error shows the exact database path,
SQLite's reason, and any private-snapshot failure. Confirm the selected
folder and your account's access to the database and journal files. Quit
Serato and refresh to allow the private-snapshot fallback. When copying a
library, quit Serato first and copy the complete folder. The app never uses
`immutable=1` to bypass journal/access errors: that could hide committed
tracks or crates in a WAL journal.

**Activity → Open backups** opens the local agent's `backups` folder. Each
successful import records its receiver-side backup path. Backups are retained
until you explicitly remove them.

To undo an import:

1. Quit Serato and Crate Transfer.
2. Make another copy of your current Serato Library folder.
3. Restore that transfer's `root.sqlite` and `master.sqlite` snapshots into
   the configured Serato Library folder. Preserve/move aside any existing
   `root.sqlite-wal`, `root.sqlite-shm`, `master.sqlite-wal` and
   `master.sqlite-shm` alongside the old databases; do not combine stale
   journal sidecars with restored snapshots.
4. Open Serato and verify. Restoring rolls back **all** library changes since
   that snapshot, not just this app's changes. Received music is left in
   place; remove only known unreferenced transfer files manually if desired.

If a transfer failed before import, no crate changes are committed and newly
published music files are removed. A crash can leave unreferenced completed
music files; these are not deleted automatically because they could already
be referenced. Abandoned partial upload staging is cleaned on agent restart.

## Test and package

```sh
python -m unittest discover -s tests -v
python -m crate_transfer --smoke-test --data-dir /tmp/crate-transfer-smoke
python -m PyInstaller --noconfirm CrateTransfer.spec
```

On headless runners set `QT_QPA_PLATFORM=offscreen`. Tests use temporary,
synthetic Serato-shaped databases and synthetic media, **not your library**.
For an isolated development agent use `--data-dir <absolute-folder>`.

- macOS output: `dist/Crate Transfer.app`. Copy it to Applications.
  Separate downloads are named `Crate-Transfer-macOS-arm64.zip` (Apple Silicon)
  and `Crate-Transfer-macOS-x86_64.zip` (Intel), both requiring macOS 12+.
  Intel builds are tested under Rosetta on Apple Silicon, not on physical Intel
  hardware.
- Windows output: `dist/Crate Transfer/Crate Transfer.exe`. Distribute the
  **whole folder**, not the executable alone.
- Build separately on each target OS/architecture. Local macOS builds are
  ad-hoc signed, **not Developer ID signed or notarized**. Distribution needs
  the appropriate signing certificates/notarization. The GitHub Actions
  workflow tests and packages on macOS and Windows.
  For an isolated Intel Python environment, keep outputs separate:

  ```sh
  /path/to/intel-python -m PyInstaller --noconfirm \
    --distpath dist/intel --workpath build/intel CrateTransfer.spec
  ditto -c -k --sequesterRsrc --keepParent \
    "dist/intel/Crate Transfer.app" dist/Crate-Transfer-macOS-x86_64.zip
  ```

## Format references

These are references for format facts, not runtime dependencies:

- [Serato: backing up and moving your library](https://support.serato.com/hc/en-us/articles/202305054-Backing-up-and-moving-your-library-to-a-new-computer)
- [Serato Crates Sync: Serato 4 SQLite internals](https://github.com/LegendT/serato-crates-sync/blob/main/docs/internals.md)
- [Serato Crates Sync: crate-engine research](https://github.com/LegendT/serato-crates-sync/blob/main/docs/serato4-crate-engine-plan.md)
- [Track Studio: Serato 4 aggregate reader / volume mapping](https://github.com/coderDJing/Track-Studio/blob/main/src/main/services/externalLibrary/seratoV4.ts)
- [CueRithm: Serato 4 local-volume path conventions](https://github.com/johnneerdael/CueRithm/blob/main/crates/cuerithm-dj-sync/src/serato4.rs)
