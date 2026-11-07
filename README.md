# Crate Transfer

A macOS menu-bar / Windows system-tray app for **manually converting and
transferring selected DJ crates/playlists and their music**, locally or over
your LAN. Choose a source, selected playlists, and a destination format.
No cloud, accounts, folder watching, mirroring, or automatic synchronization.

## Supported sources and destinations

| Format / app | Read source | Local / network output | Automatic native integration |
|---|---|---|---|
| Serato DJ 4+ | Native SQLite library; includes received root-only crates | Native Serato import, with backups | Yes; supported SQLite schemas |
| Serato DJ 3 | Legacy `_Serato_/Subcrates` + optional database V2 basic tags | Additive native `.crate` files + media | Preview; DJ 3 reloads/scans media |
| rekordbox | Native 6/7 master.db or exported XML | Additive native playlists/media via bundled SDK; XML exchange also available | Runtime/schema-gated preview |
| Traktor | Native collection.nml or exported NML | Additive native collection playlists/media; NML exchange also available | NML 19/20 preview |
| Generic M3U / M3U8 | Playlist file or folder, including VirtualDJ exports | M3U8 playlists + media | No |
| Mixxx | Native SQLite playlists/crates, basic tags | Additive native playlists + media | Schema-gated preview |
| Engine DJ | Database2 local playlists/tracks | Additive native playlists + unanalysed media | Database2 3.0.1/3.0.2 preview |
| algoriddim djay Pro | Native TSAF SQLite playlists/media (preview) | Additive native playlists + new received-media registration | Preview; app playback/authorization not validated |
| VirtualDJ | Native database.xml + virtual folders or M3U | Additive native song registration + ordered virtual folders | Version 8.0-8.5 XML preview |
| OneLibrary USB | Not implemented | Disabled | No compatible device export |
| Dual-format USB | OneLibrary + legacy Device Library requested | Not implemented | Requires both player databases and analysis assets |

All seven DJ apps have native source and destination adapters; previews are
**not a promise of lossless conversion or native-app playback validation**.
Validation uses public format references, synthetic libraries, the real
rekordbox SDK, and local/paired-network round trips—not your DJ libraries.
OneLibrary and dual-format USB remain unavailable: compatible databases and
analysis assets are required, and renaming an M3U bundle is not a device export.

### Local conversion

1. In **Send crates**, choose the DJ source format and its library or
   exchange-file path. Every source remembers its own path. The Serato source
   initially uses the destination Library default, but can be changed independently.
   **Choose source...** selects a native library file/folder or exchange file.
   An individual M3U file can also be entered in the path field.
2. Click **Refresh library**, then check the playlists. Selecting a parent
   includes descendants; changing source clears stale selection.
3. Select **Destination**, then **Convert / export locally...**. No pairing
   or other computer is needed.
4. For a **native destination**, configure that app's existing library and the
   shared received-music folder in Settings. Enable its native reception switch
   and save its destination settings (Serato DJ 4+ uses its existing folder settings).
   Quit the destination DJ app. Local imports use the same protected staging
   and backups as network imports; the tool does not create empty vendor databases.
5. For **M3U / XML / NML + files**, select an existing folder or mounted USB.
   A new output bundle is created; XML/NML must then be imported in the DJ app.

### Library selection and startup

The desktop agent starts without opening or validating any DJ library. It
does not require every DJ app—or even one DJ app—to be installed. Network,
pairing, Activity and agent preferences remain available independently.

Select a source to check its configured path (or usual local locations for
supported discovery). **No existing library for this app found** is an inline
state, not a startup failure or popup. Choose/configure that source's path, then
refresh to open and validate it and list its crates/playlists. Switching source
clears the old selection and restores the new source's saved path. It never
changes the configured native **destination** library.

Malformed or unsupported configured libraries report an actionable error when
you explicitly refresh or transfer—not at agent startup.
Use **Save agent preferences (no DJ library required)** for name, port and
launch-at-login without native-library validation.

| Source / destination | When to close the DJ app |
|---|---|
| Serato DJ 4+ source | Usually read-only while open; close it if snapshot access is required |
| Serato DJ 4+ destination | Must remain closed throughout import |
| Serato DJ 3 source / destination | Must remain closed while browsing and throughout import |
| djay Pro native source / destination | Must be closed before browsing and throughout import |
| Traktor native source / destination | Close Traktor first; keep it closed throughout import |
| rekordbox XML, Traktor exported NML, M3U/M3U8 | Standalone saved exports do not require the app closed |
| Mixxx, Engine DJ, rekordbox, VirtualDJ native source / destination | Must be closed before browsing and throughout import |

Serato DJ **4 or higher** uses schema-checked SQLite support, not a promise
that every future schema is compatible. **Serato DJ 3** uses the separate legacy
crate adapter. Select the correct generation for both source and destination.
No support for versions earlier than 3 is claimed.

### Version compatibility warnings

Local-conversion and network-send confirmations call out **DJ 4+ to DJ 3
downgrades** and **DJ 3 to DJ 4+ upgrades** before the operation starts.
Warnings are also retained in receiver/sender Activity receipts. Conversion
creates destination-native crates and media references, not an in-place vendor
database upgrade/downgrade. Database-only metadata and analysis can be lost;
original embedded media tags stay unchanged.

For other DJ apps, installed app versions are not reliably inferred from
library files. The app explicitly warns about this uncertainty, validates the
configured schema/runtime before importing, and refuses unsupported schemas.
Do not assume that a newer-to-older transfer is lossless or that every codec is
playable in the older version. Verify playback and analyze received tracks.

### Export bundles and paths

Each operation publishes a separate `Crates-<id>/Export/` bundle containing
`Music/<filename>` and hierarchical
`Playlists/<crate>/<subcrate>/playlist.m3u8`. XML/NML exports also include an
exchange file referencing the copied media.

Files are copied once and verified against source sizes and SHA-256 hashes.
Playlist order, repeats and hierarchy are retained. Filename/folder collisions
are numbered; existing output is never replaced. Failed or cancelled exports
remove only their own temporary output.

M3U8 uses UTF-8 relative paths and survives moving the **whole** bundle.
XML/NML references the **final destination-local absolute media paths**:
do not move the bundle before import. To relocate it, export again at the
new location. Native imports similarly reference the receiver's
configured music folder, never sender paths.

### Metadata limits

The exchange pipeline normalizes title, artist, album, BPM/key, cue/loop
positions in seconds, and supported tempo/grid markers where represented by
XML/NML. Format-specific unsupported values are reported rather than promised
as a lossless conversion. Read the export's Activity warnings.
rekordbox output is `rekordbox.xml`: cues/loops and 4/4 grid markers are
supported; musical key export is omitted with a warning. Traktor output is
`collection.nml`: cue/loop seconds are converted to NML milliseconds, with
eight hot-cue slots; musical key and beatgrid export are omitted with warnings.
Traktor grid/load/fade markers and M3U display/directive metadata are not
converted. XML location-keyed playlists are rejected; use TrackID-keyed XML.

M3U does not carry database-only hot cues, loops or beatgrids. The Serato adapter
does not extract or write database-only performance metadata; XML/NML metadata
is not imported into Serato. Original media bytes and embedded tags remain
unchanged. Waveforms, smart-playlist rules, analysis assets and every app's
proprietary metadata are not converted. A USB bundle is **not OneLibrary** and
hardware playback compatibility is not implied.
Serato keeps one occurrence of each track per crate; repeated source entries
are deduplicated during native import and the number omitted is reported.

### Network export reception

The receiver chooses an **existing Portable export folder** in Settings, enables
**Allow paired computers to create M3U / XML / NML exports here**, and clicks
**Save portable export settings**. This opt-in defaults to off and is saved
independently of Serato folder validation. Select the same **Destination** on
the sender, then send to a paired computer.

The receiver controls its output folder; senders cannot supply remote filesystem
paths. Export reception does not modify native DJ libraries and does not require
Serato to close. Upload staging and output copies both need free disk space.
Receipt recovery and duplicate-finish protection apply to all destinations.

There is **one destination-aware network protocol**. Both computers must run
this new application; legacy Serato-only agents are not compatible or discovered.
Every destination, including Serato, is capability/identity-checked before the
source is read or any media uploaded. No legacy fallback is provided.

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

## Transfer over the network

1. Run the app on **both** computers. Allow local-network access and the
   firewall prompts on private networks.
2. Configure the receiver for the selected native destination: its existing
   **destination library**, **Received music folder** and native reception
   opt-in. Exchange outputs instead use the portable export folder/opt-in.
   Start the destination DJ app once to create its own library if needed,
   then quit it; Crate Transfer does not invent an empty vendor database.
3. On either computer, open **Computers → Allow pairing for 2 minutes**.
4. On the other computer, select it and click **Pair selected
   computer**, or use **Pair by address**, and enter its six-digit
   PIN, shown in bold green. No fingerprint copying or comparison is required. Pair for the first
   time on a trusted private network.
5. In **Send crates**, choose the source and refresh it. Select the
   **Destination**, check the playlists, choose that paired computer, and
   click **Send selected crates**. **Quit the destination DJ app for a native
   import**; exchange-file exports do not require this.
   Selecting a parent includes its descendants. No other crates are sent.
6. Follow **Activity** on either computer. When the destination computer reports
   **Complete**, open the destination DJ app for a native import, or open the
   reported export folder for M3U/XML/NML. Read Activity warnings for each
   adapter's hierarchy/order/metadata limits, and verify received tracks in
   the DJ app. Existing playlists are not replaced.

Pairing enables **both sending and receiving** on both computers with one code.
Update both apps first. Existing pairings with unchanged identities remain
saved, but the old agent cannot communicate with this protocol. Pair again
if sending in both directions has not been authorized; pairing sends no music.
After pairing, trusted computers can import without a prompt whenever the
agent is running, that destination is enabled and its DJ app is closed.
**Forget / revoke selected** removes
the local outgoing pairing and incoming permission. Remove the pairing on
the other computer too to revoke access there.

Closing the window keeps the agent in the menu bar / tray. Use its **Quit**
menu item to stop it. Enable **Settings → Launch at login** to start quietly
after signing in. On macOS, copy the packaged app into `/Applications` first;
on Windows keep the extracted application folder in a permanent location.
This opt-in installs a per-user macOS LaunchAgent or Windows Run entry and does
not require administrator access. Disable the checkbox and save to remove it.
It takes effect at the next login, and never starts a transfer automatically.
If no system tray is available, the window opens instead of hiding the agent.

The Computers page shows discovered agents as **Online** in bold green.
**Not discovered** means offline or unavailable to Bonjour/mDNS; it does not
prevent connecting by address. A changed certificate is shown in bold red and
never trusted automatically. Failures and uncertain outcomes are also bold red.
Paired addresses and ports follow current LAN discovery announcements when
their machine ID and certificate fingerprint match the saved pairing. Updates
are checked periodically and immediately before sending; tokens and certificate
pins remain unchanged. No re-pairing is needed for an ordinary IP or port change.
Without discovery, the saved address is used; update it by pairing by address.

## Defaults and configuration

| Setting | macOS | Windows |
|---|---|---|
| Serato Library | `~/Library/Application Support/Serato/Library` | `%LOCALAPPDATA%\Serato\Library` |
| Incoming music | `~/Music/Crate Transfers` | `%USERPROFILE%\Music\Crate Transfers` |
| Portable exports | `~/Music/Crate Transfers/Exports` | `%USERPROFILE%\Music\Crate Transfers\Exports` |
| Agent settings, logs, backups | `~/Library/Application Support/CrateTransfer` | `%LOCALAPPDATA%\CrateTransfer` |
| Agent connection | TCP 49783 | TCP 49783 |

Folder changes take effect for the next transfer. Name/port changes require
an app restart. Use the actual path if Windows Known Folders are redirected,
for example to OneDrive.
**Save received music folder (all native destinations)** saves the shared
media location without requiring Serato DJ 4 or any other library. Native
destinations check their own volume/path constraints when enabled or used.
New receives default to **Crate Transfers**. Explicitly configured paths and
previously received media stay in place so existing DJ-library references
remain valid; the app never silently relocates your music.
Each folder field has a **Copy** button that copies its complete path, even
when the field is scrolled or only part of its text is selected. Standard
text selection, Copy/Paste context menus, and keyboard shortcuts also work.

On Windows, socket error **10013** means Windows denied the agent's listening
port; a reserved range, another service, or security policy can cause this.
Check reservations with `netsh interface ipv4 show excludedportrange protocol=tcp`.
Choose an available **Agent TCP port** in Settings (for example 48783), click
**Save connection port**, and restart. This saves the port independently of
library-folder validation. Bonjour advertises the chosen port; manual pairing must use it.
Do not disable the firewall or run as administrator as a workaround.

Source absolute paths are **not sent to or reused on the destination computer**.
A received file is stored as:

```text
<received-music-folder>/<crate>/<subcrate>/<original filename>
```

Each native adapter writes destination-local paths in its app's representation
(absolute paths/URIs or library/volume-relative paths). Files referenced by multiple crates are stored once under the
first referencing crate. Invalid folder characters are replaced with underscores.
Identical files in the same folder are reused after checksum verification;
different songs with the same filename receive a numbered filename instead of
overwriting existing music. Imports are additive, **not merges or syncs**.
Most adapters number colliding playlists; Engine rejects an existing incoming
root name and asks you to rename it. Previously received media remains in place
so existing library paths stay valid.

## Adapter-specific behavior

### rekordbox

Native source reads normal/nested playlists from the 6/7 `master.db` family.
Native destination uses bundled **pyrekordbox 0.4.4** and **SQLCipher** for real
track registration, playlist creation and update-sequence handling, not guessed
encrypted SQL. The complete SDK schema and `masterPlaylists6.xml` must exist.
Original database, journal sidecars and playlist XML are backed up; failed
multi-file commits are restored only when the application and SDK are closed.

Configure **rekordbox destination** and enable its native reception switch.
Title/artist/album/BPM and existing matching keys are mapped. New tracks require
rekordbox analysis; cues, loops, grids, waveforms and smart rules are not converted.
Unsupported media/runtime/schema combinations fail with actionable errors.
XML exchange remains available and requires manual import.

### Traktor

Native source and destination support `collection.nml` versions 19/20.
Imports register new destination-local media and append hierarchical ordered
playlists without replacing existing playlists. The original collection is
backed up; staged XML is validated before replacement.
Basic tags/BPM and eight hotcue/loop slots are mapped, with positions converted
to native milliseconds. Key/grid/analysis data is omitted with warnings.
Separate NML exchange exports are also available.

### Engine DJ

Native source and destination support the recognized **Database2 3.0.1 desktop
and 3.0.2 device schemas** documented by public Engine-authored captures.
The adapter verifies native registration triggers, ordered playlist chains and
backup equivalence before a transactional import. New media paths are relative
to the Engine Library folder and resolve to the receiver's copied files.

New tracks are unanalysed; basic tags and scalar BPM are supported. Performance
blobs, keys, cues, loops, grids and waveforms are not converted. Engine's native
playlist uniqueness means repeated memberships are deduplicated, with a warning.
Existing incoming root names are rejected rather than merged. Analyze tracks
in Engine DJ; this is not a player-ready USB export.

### algoriddim djay Pro

The preview native adapter reads `MediaLibrary.db` TSAF objects and registers
**new received media**, not only tracks already in djay. It creates media/title
identities, user metadata, location/entity objects and ordered playlists;
existing objects and unrelated database records are preserved. Imports are
additive with collision numbering and never merge into existing playlists.

Choose the native source bundle/database in the source picker. For native
destination import, configure **djay Pro destination library** in Settings,
enable **Allow native djay imports**, and **Save djay destination settings**.
Native reception defaults to off. Incoming media uses the configured received
music folder; source and destination library paths are independent.

**Quit djay first.** Supported schemas/serialization/index policies are checked
before writes. The import verifies a SQLite backup against its locked
pre-import database, writes transactionally, invalidates supported YapDatabase
index versions, checks integrity and rolls back on failure.

This independently implemented adapter follows public CueRithm new-media
registration research. Synthetic read/write and rollback tests are **not**
native djay playback validation. New locations use destination-local file URIs,
not generated sandbox bookmarks: djay may require music-folder authorization.
Title/artist/album/BPM, supported keys and eight cue/loop slots are mapped.
Stored macOS bookmark paths cannot be resolved onto Windows drives; use
destination-local file URIs rather than assuming cross-platform bookmark compatibility.
Beatgrid edits, waveforms and Neural Mix analysis are not generated. Unknown
durations are omitted (not fabricated); readable WAV duration is used where
available. These caveats appear in receipts; verify playback in djay before
relying on the imported library.

### VirtualDJ

Native source reads version-8 `database.xml` and ordered `.vdjfolder`/M3U lists.
Imports register receiver-local songs and create numbered hierarchical virtual
folders under `MyLists` (or an existing older `Folders` directory). Existing
database records and folders are preserved. Basic tags/BPM, cue positions and
tempo-resolvable loop lengths are mapped; full grids/analysis are not generated.

The database is backed up and replaced last after exclusive new-list publication.
Handled failures roll back new lists, but multi-file publication is not
crash-atomic: after an interrupted import, inspect Activity, folders and backups
before resending. Exported M3U exchange also remains available.

### OneLibrary USB

Public SQLCipher readers and writers exist, including **v1vendi/onelibrary**.
A valid database alone is not a complete device export: copied media, correct
analysis references/assets and independent app/player validation are required.
The current USB output is a portable export bundle, not a OneLibrary library.

**Device Library Plus was renamed OneLibrary.** The older compatibility format
is **Device Library**, stored as `PIONEER/rekordbox/export.pdb`; OneLibrary uses
`PIONEER/rekordbox/exportLibrary.db`. A dual-format USB keeps both alongside
shared media and compatible analysis assets, as rekordbox does for older/newer
players. Public dual writers exist (DJ USB Tkit), but integrating its binary
PDB pages, SQLCipher database and player assets is not a small optional addition.
It remains unavailable rather than producing an incomplete USB. Compatibility
depends on the specific player/firmware, not just “rekordbox 6 or earlier”.

### Mixxx

Reads native `mixxxdb.sqlite` playlists and crates. Playlist ordering and repeats
are preserved; crates have no stored order and are sorted by artist/title.
Basic title/artist/album/BPM/key are exported where present. Cue/loop and
beatgrid data are not converted yet and this loss is reported. Close Mixxx
before reading to save your latest changes.
The schema-gated native destination registers new media with basic
title/artist/album/BPM and creates additive ordered playlists, including repeats.
Nested names are flattened with ` / ` separators. Original SQLite snapshots
are compared to the locked database before transactional writes, and membership
readback is checked. Keys, cues, loops and grids are not written; let Mixxx
parse audio headers and analyze received tracks.

### Serato DJ 3 legacy crates

Choose **Serato DJ 3 legacy crates** as the source and an existing `_Serato_`
folder (normally `~/Music/_Serato_`, or a drive's `_Serato_` folder).
`Subcrates` must exist. The bounded UTF-16BE/TLV reader supports `.crate`
membership, `%%` hierarchy and basic tags from a recognized `database V2`.
Source playlist order is preserved; smart-crate rules and analysis are not read.

For native output, configure **Serato DJ 3 destination** in Settings, enable
its reception switch and save. Received music must be on the **same local
volume** as that `_Serato_` store. Native paths are relative to the appropriate
drive root, never to the sender's machine.

Imports create numbered `.crate` files without replacing existing crates,
deduplicate repeated membership, and create empty parent crates for hierarchy.
The existing database, crate files and ordering preferences are backed up and
checked for concurrent changes. New files are published exclusively; handled
failures remove only the newly created files. Multi-crate publication is not
crash-atomic.

`database V2` and `neworder.pref` remain owned by Serato and are **not rewritten**.
Restart Serato DJ 3 so it reloads crates and scans received media; verify
visibility/playback and let it analyze tracks. Database-only cues, loops,
BPM/key/tag edits and grids are not imported; embedded tags/media remain intact.
Actual DJ 3 application acceptance has not been tested. The legacy adapter is
not a fallback for writing DJ 4's authoritative SQLite library.

### Serato DJ 4+ SQLite

This is a schema-checked SQLite adapter based on publicly documented reverse
engineering. It is not affiliated with Serato.
It does not rely on legacy `Subcrates/*.crate` exports.

- Browses `master.sqlite` read-only, including volume mappings from
  `connection.database_uri`. A SQLite read transaction keeps the exported
  crate selection/membership consistent. The source library is never modified.
  Crates present only in authoritative `root.sqlite` are also listed and can
  be sent onward immediately, even when Serato has not added them to its
  aggregate cache. Their memberships and local-volume paths are read directly
  from the authoritative database. Matching full crate hierarchies already in
  the aggregate are listed once, using the aggregate entry. Separate read
  transactions protect each database's view; this is not an atomic snapshot
  across both databases. Refresh the list if Serato changes it during selection.
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
  Some aggregate caches use Serato-only collations such as
  `serato_asset_string_comparator`. If those prevent the aggregate backup's
  integrity check, its complete schema and every table row are compared with
  the same source read snapshot instead. The Backup API preserves its indexes;
  their Serato-specific ordering cannot be independently checked. The original
  aggregate is never changed. No substitute comparator is registered, and the
  authoritative `root.sqlite` must still pass all integrity checks; unsupported
  collations there stop the import before any library changes.
- Verifies every music file's size and SHA-256 before importing. Transfers
  stream files in bounded chunks rather than loading music into memory.
- Never replaces an existing crate or differing music file. Reused music is
  checksum-verified. Original file bytes, including embedded tags, cues and
  beatgrids, remain unchanged. Serato can analyze newly imported tracks;
  database-only metadata, history and preferences are not copied.
## Shared transfer safety

- Uses TLS 1.2+, persistent certificate pinning, two-minute single-use pairing
  codes (five attempts), and revocable per-computer tokens. Incoming tokens
  are stored hashed; outgoing tokens and private keys live in the local
  user settings folder. Do not expose the port to the internet or share this
  folder.
- Discovery uses Bonjour/mDNS (`_cratetransfer._tcp.local.`, UDP 5353) on IPv4 LANs.
  Discovery announcements are **not trust**. During explicit code pairing, the
  app learns the certificate directly from the selected address, verifies it
  again before sending the code, and saves its fingerprint only after success.
  Future transfers require that saved certificate; a mismatch is never silently
  accepted. This trust-on-first-use flow does not protect initial pairing from
  an active network impersonator relaying the code. Pair only on a trusted
  private network. Manual addressing works if multicast is blocked.

### Transfer limits

- **Received music must be on the same local volume as the receiver's
  Serato Library.** Writing external-drive/network location databases is not
  implemented; those destinations are rejected rather than written incorrectly.
  Source external-drive music is supported when Serato's aggregate has a
  resolvable local volume mapping and its files are mounted.
- Normal local-file crates/playlists only. Smart-playlist rules, streaming subscriptions,
  stems sidecars and DRM-protected media are not migrated.
- Supported filename extensions: MP3, FLAC, WAV, AIFF/AIF, M4A, AAC, OGG,
  ALAC, MP4 and MPEG video (MPG/MPEG). Video bytes are transferred unchanged,
  not transcoded; playback requires destination-app video support and a compatible codec.
  Filenames must be portable to Windows. Invalid names,
  missing files and unknown schemas fail explicitly.
- Per transfer: up to 5,000 crates, 20,000 distinct files, 200,000 memberships,
  32 hierarchy levels, 20 GiB per file, 500 GiB total, and an 8 MiB manifest.
- One incoming transfer at a time; uploads are sequential. Cancellation works
  before import; it does not interrupt an active database transaction.
- Uploads do not automatically resume. Imports can take longer on large
  libraries: the app allows ten minutes for the final import response. If the
  connection drops, it checks the authenticated remote transfer status for up
  to another ten minutes, without resending files or importing a second time.
  Completed receipts survive agent restarts. If the outcome still cannot be
  confirmed, it remains **Uncertain**, never success. Select it in Activity and
  use **Check uncertain transfer** to query the other computer again without
  resending. Older agents must be updated to support these status queries.
- Automated validation uses isolated synthetic libraries and media. It covers
  source switching, local conversion, paired network exports and native Serato
  imports. It does not certify every vendor build or DJ device. Test against
  disposable library copies before using a new adapter with your collection.

### Backup and restore

For native Serato imports, if opening the library fails, the error shows the exact database path,
SQLite's reason, and any private-snapshot failure. Confirm the selected
folder and your account's access to the database and journal files. Quit
Serato and refresh to allow the private-snapshot fallback. When copying a
library, quit Serato first and copy the complete folder. The app never uses
`immutable=1` to bypass journal/access errors: that could hide committed
tracks or crates in a WAL journal.

**Activity → Open backups** opens the local agent's `backups` folder. Each
successful import records its receiver-side backup path. Backups are retained
until you explicitly remove them.

For other native apps, quit the DJ app and Crate Transfer, make a safety copy of
the current library, then restore every original recorded in that transfer's
backup into the configured library. Traktor restores `collection.nml`; Mixxx
restores `mixxxdb.sqlite`; Engine restores `Database2/m.db`; djay restores
`MediaLibrary.db`; Serato DJ 3 removes only the new files recorded in
`created-crates.json` and can restore the saved originals if needed; rekordbox restores `master.db`, its original sidecars and
`masterPlaylists6.xml`. VirtualDJ restores `database.xml` and requires removing
only the new virtual-folder files created by that transfer. Keep stale SQLite
WAL/SHM files separate from restored snapshots. Restoring reverts **all** changes
since the backup, not only this transfer.

To undo a Serato import:

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
published music files are removed. If a native multi-file restore cannot be
completed safely, Activity records **uncertain** and retains received media
that the native library might already reference; inspect/restore the backup
before resending. A crash can leave unreferenced completed
music files; these are not deleted automatically because they could already
be referenced. Abandoned partial upload staging is cleaned on agent restart.

If transfer history is unreadable, startup preserves the original as
`transfers-damaged-<unique-id>.json` in the agent settings folder and creates an
empty Activity history. A visible warning gives the backup location; settings,
pairings, music and DJ libraries are untouched. Do not resend old transfers
until you check the destination libraries/outputs and backups on both machines: their previous receipts
are unavailable while that history is damaged. No transfers are retried
automatically. If preserving the file or saving new history fails, startup
stops and reports the error rather than discarding it.

## Test and package

The app uses a neutral black vinyl-record icon with a warm orange paper label
for every DJ source, with no transfer arrows or DJ-vendor marks.
Regenerate macOS and Windows package icons from that same renderer with
`QT_QPA_PLATFORM=offscreen python assets/generate.py` before packaging if the
design changes.
The existing source and packaged `--smoke-test` also verify the pinned
rekordbox SDK, SQLCipher driver and SQLAlchemy dialect without opening a DJ
library. Normal startup still performs no native-runtime/library probe.

```sh
python -m pip install ".[build]"
python -m unittest discover -s tests -v
python -m crate_transfer --smoke-test --data-dir .ui-smoke
python -m PyInstaller --noconfirm CrateTransfer.spec
```

On headless runners set `QT_QPA_PLATFORM=offscreen`. Tests use temporary,
synthetic databases/object graphs for all DJ adapters, XML/NML/M3U fixtures and synthetic media,
**not your library**.
For an isolated development agent use `--data-dir <absolute-folder>`.

- macOS output: `dist/Crate Transfer.app`. Copy it to Applications.
  CI downloads are `Crate-Transfer-macOS-arm64.zip` (Apple Silicon) and
  `Crate-Transfer-macOS-x86_64.zip` (Intel). Extract using Archive Utility
  and drag the app into Applications. ZIP creation uses macOS `ditto` to
  preserve bundle symlinks and metadata. The bundle declares macOS 12+; CI tests macOS 15,
  not the minimum OS version.
- Windows output: `dist/Crate Transfer/Crate Transfer.exe`. Distribute the
  **whole folder**, not the executable alone. CI produces
  `Crate-Transfer-Windows-x64.zip`; extract the entire ZIP before launching.
- Build separately on each target OS/architecture. Local macOS builds are
  ad-hoc signed, **not Developer ID signed or notarized**. Distribution needs
  the appropriate signing certificates/notarization. CI packages are also
  not Developer ID signed or notarized; Windows packages are not Authenticode
  signed.

### macOS Gatekeeper

The current macOS packages are ad-hoc signed, not Apple-notarized. Passing
`codesign --verify` verifies the bundle's signature integrity; it does not
establish Gatekeeper trust. On Sequoia, rejection or a pre-launch bounce can
persist even after **Open Anyway**. ZIP delivery has been restored to avoid
the reported DMG-specific launch problem, but changing archive formats does
not add Apple trust or guarantee Gatekeeper approval.

Trusted distribution requires an Apple Developer membership, a **Developer ID
Application** certificate, hardened-runtime signing of the app and its nested
code, Apple notarization, and stapling/verification of the app before archiving.
These credentials are not currently available, so the existing packages cannot
be claimed to pass Gatekeeper. Do not disable Gatekeeper or remove quarantine
as a workaround. Until signed/notarized packages are available, use the
**Run from source** instructions above rather than the downloaded application.

### Runner builds and releases

- `.github/workflows/build.yml` runs for branch pushes, pull requests, `v*`
  tags and manual **Run workflow** requests. Explicit hosted runners are
  `macos-15` (native Apple Silicon), `macos-15-intel` (native Intel, not
  Rosetta), and `windows-2022` (x64), using matching 64-bit Python 3.12.
  Each job checks runner/Python architecture, runs all tests, builds with
  PyInstaller, and checks the packaged executable architecture. It renders
  the packaged UI with a timeout and requires a screenshot, running from the
  extracted ZIP. macOS jobs also verify archive integrity and the extracted
  bundle's signature integrity (not Gatekeeper approval). Download the matching
  Actions artifact to obtain its ZIP.
- Successful `v*` tag pushes and manual runs against `v*` tags automatically
  publish all three packages to that tag's GitHub Release, only after every
  build/test/smoke check passes. Branch and PR runs never publish. Only the
  release job has `contents: write`; build jobs remain read-only. Packages
  come from the same workflow run, and publication refuses a tag whose current
  commit differs from the built commit. A missing release is created with
  generated notes; an existing release keeps its title, notes, draft/prerelease
  status and other metadata. Matching asset filenames are replaced on reruns;
  unrelated assets are retained.
- To rebuild/publish an **existing tag** without moving it, open **Actions →
  Test and package → Run workflow**, select the existing `v*` tag as the run
  ref (not a branch), and run it. If the UI does not offer a tag selector, use
  `gh workflow run build.yml --ref v0.1.0`, substituting your tag, then monitor
  the run in Actions. The tag must already contain this workflow version;
  existing tags pointing to older workflows will execute those older versions.
  Selecting a tag does not use the workflow from the latest branch. If the tag
  lacks this workflow, merge the workflow changes and create a new version tag
  at that merged commit instead.
  **Do not move published tags.** Create a new version tag for source changes.
  Rerunning the same tag replaces its package assets, so downloaded binaries
  may differ as build dependencies evolve. Immutable releases may reject asset
  replacement; publish a new version instead.
- To create a local ZIP after a native macOS build:

  ```sh
  ditto -c -k --sequesterRsrc --keepParent "dist/Crate Transfer.app" \
    "dist/Crate-Transfer-macOS-$(uname -m).zip"
  unzip -t "dist/Crate-Transfer-macOS-$(uname -m).zip"
  ```

  For an isolated Intel Python environment, pass
  `--distpath dist/intel --workpath build/intel`
  to PyInstaller and archive `dist/intel/Crate Transfer.app` instead. A Rosetta
  local build is not a substitute for the native Intel CI job.

## Public format and native-integration references

These are research references, **not all implemented runtime dependencies**.
Read-only support, native playlist creation, new-track registration and
complete device exports are different capabilities. Public writer existence
is not a claim that this app already integrates it. License compatibility and
schema/application roundtrips must be checked before adopting dependencies.

### Serato
- [Mixxx: legacy Serato TLV reader and local/external volume paths](https://github.com/mixxxdj/mixxx/blob/HEAD/src/library/serato/seratofeature.cpp)
- [serato-tools: database V2 format/version](https://github.com/bvandrc/serato-tools/blob/HEAD/src/serato_tools/database_v2.py)
- [Serato-lib: legacy crate format research](https://github.com/jesseward/Serato-lib/blob/HEAD/seratolib.py)
- [Serato: backing up and moving your library](https://support.serato.com/hc/en-us/articles/202305054-Backing-up-and-moving-your-library-to-a-new-computer)
- [Serato Crates Sync: Serato 4 SQLite internals](https://github.com/LegendT/serato-crates-sync/blob/main/docs/internals.md)
- [Serato Crates Sync: crate-engine research](https://github.com/LegendT/serato-crates-sync/blob/main/docs/serato4-crate-engine-plan.md)
- [Track Studio: Serato 4 aggregate reader / volume mapping](https://github.com/coderDJing/Track-Studio/blob/main/src/main/services/externalLibrary/seratoV4.ts)
- [CueRithm: Serato 4 local-volume path conventions](https://github.com/johnneerdael/CueRithm/blob/main/crates/cuerithm-dj-sync/src/serato4.rs)

### rekordbox

- [rekordbox XML specification, including node types and cue units](https://pyrekordbox.readthedocs.io/en/latest/formats/xml.html)
- [pyrekordbox: native master database track and playlist writes](https://github.com/dylanljones/pyrekordbox/blob/02a208837434e4c05c0a222e8fe6583e6ac0547f/pyrekordbox/masterdb/database.py)
- [Bundled pyrekordbox 0.4.4 SDK release](https://pypi.org/project/pyrekordbox/0.4.4/)
- [Bundled SQLCipher wheels 0.5.7](https://pypi.org/project/sqlcipher3-wheels/0.5.7/)

### Traktor

- [DJ Data Converter: Traktor cue types and millisecond conversion](https://github.com/digital-dj-tools/dj-data-converter/blob/master/src/converter/traktor/cue.clj)
- [DJ Data Converter: Traktor path encoding](https://github.com/digital-dj-tools/dj-data-converter/blob/master/src/converter/traktor/location.clj)
- [Traktor NML utilities: native collection handling and Traktor Pro 4 tests](https://github.com/wolkenarchitekt/traktor-nml-utils/tree/5774ff599604b9688c83ce968ef397e917b1dc6a)

### Engine DJ

- [libdjinterop: native Engine Database2 creation and writes](https://github.com/xsco/libdjinterop/tree/bd1e417759c19eae1ad5dbd7173bb4bc47d6f7c2/src/djinterop/engine)
- [rb2engine: Python Engine database, tracks and performance-data writers](https://github.com/jrgutier/rb2engine/tree/1602d73e75306519a44b610ec1d616b167eb4f8e/src/rb2engine/writer)

### algoriddim djay Pro

- [CueRithm: new-media objects, locations, playlists and index invalidation](https://github.com/johnneerdael/CueRithm/blob/9d96cc95d4b94959ecd6f9f839f56150c01b5d15/crates/cuerithm-dj-sync/src/djay_pro.rs)
- [djay Pro Playlist Optimiser: native playlist objects and view/index updates](https://github.com/tiandrewthh/djay-Pro-Playlist-Optimiser/blob/529374e033a2e0cdb26e89fae001b91460de8118/djay_sorter/export.py)
- [djay playlist automation: existing-track playlist membership](https://github.com/AndreasDietzel/dj-pro-playlist-automation/blob/6801a4240a08f7e4fad51ec11f9f65ea6b167a8e/src/djay-db.ts) (research only; no reusable license verified)

### VirtualDJ

- [OpenFadeR: VirtualDJ native library and virtual-folder support](https://github.com/OpenRsG/OpenFadeR/tree/47867004d604bcdb68180eca98013a09e5a49573/compatibility/virtualdj)

### OneLibrary USB

- [v1vendi/onelibrary: SQLCipher database writer and analysis-asset research](https://github.com/v1vendi/onelibrary/tree/02c6f80c802a635c394e733058502ecf28cf9fc9)
- [onelibrary-connect: read-only SQLCipher integration](https://github.com/chrisle/onelibrary-connect/tree/ac3edd3b8a18f7649d336ebd56d28b15923a05d6)
- [libdjinterop: OneLibrary support, distinct from its Engine writer](https://github.com/xsco/libdjinterop/tree/bd1e417759c19eae1ad5dbd7173bb4bc47d6f7c2/src/djinterop/onelibrary)
- [DJ USB Tkit: dual PDB/eDB compatibility and structural constraints](https://github.com/haivala/dj-usb-tkit/blob/main/docs/PDB.md)
- [Deep Symmetry: legacy rekordbox Device Library format](https://djl-analysis.deepsymmetry.org/rekordbox-export-analysis/exports.html)

### Mixxx

- [Mixxx: native open-source library implementation](https://github.com/mixxxdj/mixxx/tree/main/src/library)
- [DJ USB Tkit: Mixxx schema, playlist/crate and cue-reading research](https://github.com/haivala/dj-usb-tkit/blob/main/backend/src/service/mixxx_import.rs) (research reference; implementation not copied)
