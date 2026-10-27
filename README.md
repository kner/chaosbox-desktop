# ChaosBox Desktop for Ubuntu 24.04

A native Python/Tk application using the same metadata fields, JSON records and
setup profile format as the Android app. No browser or Android emulator is needed.
The interface supports English, German, French, and Italian. Set `Language=en`,
`Language=de`, `Language=fr`, or `Language=it` in the global `[App]` section of
`setup.ini`. Category names, custom labels, and text snippets retain their
configured language.

## Install and launch

Install all runtime dependencies on Ubuntu 24.04 from the repository root:

```bash
./install-dependencies.sh
```

The script installs the Ubuntu packages and checks that the application can
load its dependencies, including registering a native file-drop target with
`tkdnd`. The drag-and-drop check uses a virtual X display (`xvfb` and `xauth`),
so it also runs without a desktop session. It uses `sudo` when needed. Then launch the app with:

```bash
./run-desktop.sh
```

To install for the current user, run:

```bash
./install-desktop.sh
```

Each installation gets its own application menu entry; all boxes use one shared application:

```bash
./install-desktop.sh --installdir=~/cb1
./install-desktop.sh --installdir=~/cb2
```

Without `--installdir`, the installer detects all registered installations and
updates their application and launchers while preserving setup, media and records.
It creates `~/ChaosBox` only when no existing installation is found. Use
`--installdir` to add a new installation or explicitly update a particular one.
An existing `setup.ini` in the installation directory is kept unchanged,
including its SSH settings. A default setup is created only if the file is missing.
An existing empty directory is also accepted. When creating a new setup,
only the first box receives missing files from `assets/JPG` and
`assets/TXT` in its respective folders, including subdirectories. Other boxes
start empty. If the setup contains no `[Box]` section, the installer adds
`[Box]` with `Titel=ChaosBox` and copies the assets there.

Launch **Chaosbox-cb1** or **Chaosbox-cb2** from the Ubuntu application menu.
Each launcher is permanently bound to its installation directory. Shared application files
are stored under `~/.local/share/chaosbox/desktop`; box launchers are stored under
`~/.local/share/chaosbox/installations/<installation-id>`;
menu entries are `~/.local/share/applications/<installation-id>.desktop`.
The ID and window class depend on the full installation path, so directories
with the same final name still have distinct launchers. Run the installer once,
with or without a box path, to update the application for **all installed boxes**, preserving
each box's setup and data. Existing per-box launchers are migrated automatically.
All boxes use the new version on their next launch; reopen any running windows.
No administrator privileges are needed when dependencies are already present.

To uninstall a per-user installation, close its windows and run:

```bash
./uninstall-desktop.sh --installdir=~/cb1
```

Without `--installdir`, this selects `~/ChaosBox`. The script removes that
installation's launcher and application menu entry. Shared application files
are removed after the last installation's launcher is removed. All directories,
box data, `setup.ini`, indexes, UI state and SSH credentials remain intact.
Run it once for each installation you want to uninstall. Repeated runs are safe.
For a Debian installation, use `sudo apt remove chaosbox-desktop` instead.

## Debian package (Ubuntu 24.04)

Build an architecture-independent installer using the system Python and dpkg:

```bashssh-copy-id -i ~/ChaosBox/.state/credentials/id_ed25519.pub kner
cd desktop
/usr/bin/python3 build_deb.py
sudo apt install ./dist/chaosbox-desktop_1.0.2_all.deb
```

Launch **ChaosBox Desktop** from the application menu or run `/usr/bin/chaosbox`.
The package installs to `/usr/share/chaosbox-desktop`; apt installs its runtime
dependencies. It includes the current preview and file-operation behavior.
Settings and media remain in the user's home directory and survive removal with
`sudo apt remove chaosbox-desktop`. SSH credentials are not included.

An earlier per-user installation can leave a second menu entry. The old entry
uses `~/.local/share/chaosbox`; `/usr/bin/chaosbox` always starts the Debian build.
To use only the Debian menu entry, remove the old
`~/.local/share/applications/chaosbox.desktop` launcher.

For subsequent releases use `build_deb.py --version VERSION`. The `dist` folder
contains the `.deb` and a SHA-256 checksum file.

## Data and settings

`--installdir` selects an independent installation, defaulting to `~/ChaosBox`.
The installation directory manages setup, shared index and UI state. It is not
a selectable box and has no JPG or TXT folders. Only configured boxes appear in
the selector; the first box is selected at startup. The title is `Chaosbox-cb2`
for an installation at `/x/y/cb2`:

```text
~/ChaosBox/
  setup.ini
  .indices/             # common search index for all projects
  .state/               # UI state and SSH credentials
  Bilderbox/
    JPG/
    TXT/
    setup.ini           # optional local overrides and child projects
```

```bash
./run-desktop.sh ~/cb2
./run-desktop.sh ~/cb2 --newindex
```

The installation directory can be passed directly, or with `--installdir=~/cb2`.
`--data-root` is replaced by `--installdir`. The index is always in `.indices`
inside the installation. `--state-dir` and `--setup` remain optional overrides.
JSON records retain their `.json` extension inside the `TXT` folder.

The **Setup** button edits the active project's configuration. `[App]` configures
inherited defaults at installation level and box overrides in local setup files.
`Standard=` is no longer used. Repeated
`[Box]` sections declare projects; `Title` is the folder name. `JPG=` and `Daten=`
are removed because the subfolders are always `JPG` and `TXT`.

```ini
[App]
ImageWidth=2048
Fields=Box,Quantity,Device,Alias,Category,Comment,Package
Categories=Werkzeug,Elektronik

[Box]
Title=Bilderbox
Fields=Box,,,Tags,Category,Comment
Categories=Familie,Berg,Urlaub

[Box]
Title=Chaosbox1
Categories=Haushalt,Garten
```

Set `ImageWidth` (1–20000 pixels) centrally in `[App]`. Each `[Box]` inherits
it and can override it with its own `ImageWidth` value. Local `[App]` sections
and nested boxes follow the same inheritance. The default is 3000 pixels.
Replace the old `[ImageSize] LIMIT` setting with `[App] ImageWidth`.

Each project inherits its parent's fields and settings, with independent
categories when specified. An optional `Bilderbox/setup.ini` can override
`[App]`, `[Poster]`, `[TextSnippets]` and `[SSH]`, and declare more
`[Box]` sections such as `Titel=Urlaub` for `Bilderbox/Urlaub`. New categories are
saved to the matching Box section or local App section. Repeated Box sections
are preserved independently. Titles must be unique within their parent and
must be single folder names; reserved names and symbolic project folders are
rejected.

Existing configurations must be converted from `[App.NAME]` to `[Box]` with
`Titel=NAME`, removing `Standard=`, `JPG=` and `Daten=`. Place the main settings
in `<installdir>/setup.ini`, media in each project's `JPG` folder and records
in its `TXT` folder. Existing files are not moved automatically. Rebuild with
`--newindex` after migrating data or changing the configured projects.

All projects share one search index. Local saves update only affected entries.
External changes become searchable after starting with `--newindex`. Loose
media is organized into category folders when rebuilding the index; duplicate
names are retained without overwriting files.

## Editing

- The **Comment** box has **Markdown** and **Preview** tabs. Edit the original text
  in Markdown; Preview displays headings, bold/italic (including combined styles),
  strikethrough, bullet/numbered lists, block quotes, links and inline/fenced code.
  Preview links can be opened by clicking them. Existing line breaks are retained.
  HTML is displayed as text and embedded images show their alternative text.
  The original Markdown is preserved when saving metadata and JSON records.
  Tables and Markdown extensions such as footnotes are not supported.
- **Open JPG / MP4** öffnet einen vollständig deutsch beschrifteten, scrollbaren
  Vorschauraster mit **1–10 Spalten**. Die Schaltfläche **Selektion editieren**
  übernimmt die bisherige Mehrfachbearbeitung. **Einzelbilder editieren** öffnet
  die ausgewählten Dateien nacheinander, beginnend mit der ersten Auswahl.
  The window can be maximized and restored using its title-bar controls.
  Only files directly in the current folder are shown. **Choose folder …** changes
  folders. The last selected folder is remembered per profile across restarts,
  including folders used through **Other files …**.
  Double-clicking a folder in the folder picker selects it and opens its images
  immediately. Enter a folder path and press Enter to navigate directly.
  Kacheln schalten die Auswahl um; Umschalt-Klick wählt einen Bereich. **Alle
  auswählen** und **Auswahl aufheben** ändern die Auswahl. Sie bleibt beim
  Wechsel der Spaltenzahl erhalten.
  Previews load in the background; unavailable previews remain selectable.
  Thumbnail size follows the column width while preserving image proportions.
  **Metadaten anzeigen** displays the full **box | category | comment** below each
  thumbnail. Comments remain in their original Markdown source form, including
  line breaks, and tiles grow to show the entire comment. Comments load in the
  background when enabled.
  **Andere Dateien …**
  also imports PNG files. The first selected file supplies the preview. Selection
  order is preserved; deselecting and reselecting a tile moves it to the end.
  Select all appends unselected tiles in display order.
  Multiple files show only identical metadata values. Mixed values appear blank.
  If fields differ, a warning names their labels from setup.ini and offers
  **Erweitert** (append new content) or **Gelöscht** (replace with new input).
  Cancelling leaves the previous selection unchanged. The selected mode applies
  to Save and Poster. Extend appends text with commas (newlines for Comment)
  and adds Quantity; blank inputs preserve existing content. Replace overwrites
  differing fields with the new inputs, clearing them if left blank.
  **Save** asks “Attention! All selected Images get this texts” for multiple
  selected files. OK proceeds; Cancel leaves files and form inputs untouched.
  **Save** preserves unchanged common fields independently for each file.
  Other metadata and each file's creation date are retained.
  In der Einzelbildbearbeitung bleibt das Bild nach **Save** angezeigt und die
  Pfeile wechseln zyklisch nur zwischen den ausgewählten Dateien. **Felder
  leeren** setzt die Metadatenfelder zurück, ohne das angezeigte Bild zu schließen;
  so können auch Bilder ohne weitere Änderungen durchgeblättert werden. In der
  normalen Mehrfachbearbeitung setzt **Save** weiterhin Felder und Auswahl zurück.
  Abgebrochene oder fehlgeschlagene Speicherungen behalten die Eingaben.
  Local saves refresh only the search index entries of the saved files.
  Metadata for selections, poster exports and index updates is read once per
  unique file, including comments and capture dates. Image metadata uses the
  GExiv2 Python binding; MP4 metadata uses Mutagen and FFprobe. ExifTool is no
  longer required.
  Initial index creation reuses metadata already read while organizing files.
  Startup loads the shared index, building it only when absent or with `--newindex`.
  Searches and profile switches reuse it. Run with `--newindex` after external
  changes or profile configuration changes, or to replace a damaged cache.
- Saved media goes into the category folder, or **unassigned** when no category
  is specified. Existing profile media moves there when its category changes;
  media already in the correct folder is updated in place. Imports receive a separate `_cb`
  filename; name collisions get numbered suffixes. PNG files become JPG with a
  white background. Images wider than `ImageWidth` are resized proportionally. Smaller
  JPGs retain their encoded image data; MP4 video/audio is not re-encoded.
- JPG metadata is stored as individual embedded XMP properties:
  - `dc:description` (language `x-default`): Comment.
  - `xmp:CreateDate`: the preserved creation date.
  - `chaosbox:box`, `chaosbox:anzahl`, `chaosbox:device`, `chaosbox:alias`,
    `chaosbox:category`, `chaosbox:package`: inventory fields, using the namespace
    `urn:chaosbox:metadata:1.0/`. `chaosbox:version=1` marks a complete record,
    including fields deliberately left empty.
  Other EXIF, IPTC and XMP metadata is retained; resized images retain their ICC
  profile, with corrected orientation/dimensions and obsolete thumbnails removed.
  MP4 metadata remains JSON in ItemList `Comment`, with existing QuickTime Keys
  comments synchronized. Video and audio streams are retained without re-encoding.
- Existing EXIF `UserComment` records remain readable. Saving an image migrates
  the edited fields to XMP and removes EXIF `UserComment` and its XMP EXIF mirror.
  Merely opening or indexing images does not rewrite them. External import sources
  remain unchanged. Older Android versions that only read `UserComment` need XMP
  support to read newly saved JPG metadata; JSON records and MP4 keep their format.
- Legacy text comments recognize `Kategorie:` or the second `|`-separated
  field (`Anzahl | Kategorie | ...`). Unicode and line breaks are preserved.
- JSON records omit `modified`. Media `created` uses EXIF DateTimeOriginal,
  then CreateDate (also for MP4), then a date in the filename (for example
  `IMG_20240102_030405.jpg` or `2024-01-02_03-04-05.png`). Invalid dates are
  ignored. Date-only names remain date-only; unknown timezones are not invented.
  Otherwise, an existing `created` is preserved, or the original file's modification
  time is used as an approximation. Each image in a batch keeps its own date.
  Saving removes old `modified` fields; rebuilding the index refreshes image dates
  in its JSON entries without rewriting the source images.
- **Open JSON** uses the Box field if supplied, or opens a file chooser. Select a
  record through Device; profiles with hidden Device use a record chooser.
  Saving a selected record preserves unknown fields and its creation date. Click
  the preview to open the media gallery for the record; **Show metadata** there
  reveals full Markdown comments. Selecting a file keeps the JSON fields in the
  form and Save writes them into media under the profile's `JPG` folder.
- **Search** first enters search mode; pressing it again runs the query. Queries
  match Category by a case-insensitive literal prefix; other fields use
  case-insensitive Python regular expressions. Categories are stored in uppercase.
  Nonempty fields are combined with AND. Device also searches Comment; Comment and Alias search
  Device, Alias and Comment. Python-specific regex syntax can differ from Java.
  **Clear all** clears the form and exits search mode; Escape restores the form.
  **Repeat search** reuses the last query.
- **Del** deletes the displayed media file or the current record in a JSON file
  and updates the search index. Other JSON records are retained.
- Double-click the selected image preview to open the file in the system's default
  application. Right-click the preview to display its encoded `file://` link and
  copy it to the clipboard. **Open in editor** launches the configured `editor`
  from the active profile's `setup.ini`, passing it the selected file path. The
  left and right double-chevron overlays browse selected images or neighboring
  JSON files and wrap at either end. The selected file path remains visible above
  the preview.
  MP4 files show a still preview; double-click opens the video in its default player.
- **Poster** creates a JPEG poster from the selected JPG/PNG images in selection
  order, left to right and top to bottom. It saves to `poster` beside the profile's
  image folder, for example `~/ChaosBox/poster`. Source images stay intact;
  repeated exports get distinct filenames. Videos are rejected.
  Configure the poster in **Setup** (existing setups receive the section when opened):

  ```ini
  [Poster]
  POSTER-LIMIT=6000
  POSTER-SIZE=200x100
  POSTER-COLS=3
  POSTER-ROWS=3
  POSTER-FIX=true
  POSTER-MARGIN-LEFT=15
  POSTER-MARGIN-RIGHT=10
  POSTER-MARGIN-TOP=10
  POSTER-MARGIN-BOTTOM=15
  POSTER-FRAMES=eeee
  POSTER-BACKGROUND-COLOR=9bb195
  POSTER-IMAGE-BACKGROUND-COLOR=FFFFFF
  POSTER-IMAGE-PADDING=2
  ```

  SIZE is **height × width in millimeters**; LIMIT is the longest output edge in
  pixels, independent of `ImageWidth`. All four margins are configured in millimeters;
  column widths and row heights use the remaining area. The margins must leave
  positive space within POSTER-SIZE. Colors accept hex RGB/RGBA or RRGGBB/RRGGBBAA,
  optionally prefixed with #; `eeee` means translucent light gray (#EEEEEEEE).
  Image/caption panels use IMAGE-BACKGROUND-COLOR with 0.3 mm colored frames.
  POSTER-IMAGE-PADDING adds an inner inset in millimeters between each frame
  and its image/caption content (default 2 mm, zero allowed).
  The first readable JPG, PNG, WebP, BMP or TIFF directly beside the active
  setup.ini (filename order) covers the full poster background, including margins.
  The background image keeps its proportions and is center-cropped to fill;
  without a usable image, BACKGROUND-COLOR is used. Transparent backgrounds are
  composited over that color. Individual source images are never cropped.
  Each poster's title is the first field (Box) of its first photo. Only each
  photo's Comment appears below it, rendered with the same Markdown styles as
  Preview. Poster links show styled link text. Font sizing and wrapping account for
  headings, lists and code; text that cannot fit produces an error rather than
  being truncated. Other fields are not shown. Form edits
  apply according to the append/replace mode chosen for the selection;
  unchanged common fields retain each photo's metadata. Source files stay intact.
  Photos fill the configured grid in selection order with 3 mm gutters.
  With POSTER-FIX=false, portrait photos (after EXIF rotation) occupy two vertically adjacent cells.
  A portrait that cannot fit below the current cell moves to the next poster;
  following photos can fill the space left on the current poster. Portraits
  require at least two configured rows. Additional posters are created until
  all photos are placed, each with the title of its first photo.
  Text space is reserved before resizing images. Long text uses a smaller font;
  text that cannot fit produces an error. Print resolution follows SIZE/LIMIT.
  POSTER-FIX=true keeps the configured grid dimensions and assigns exactly one
  cell to each photo, including portraits; a single configured row is allowed.
  With POSTER-FIX=false, field 2 (`anzahl`) supplies a zoom factor, as a number
  or `Z=number` (decimal dot or comma accepted). Blank, zero and values up to 1
  use the normal footprint. Values above 1 multiply the reserved cell count:
  e.g. Z=2 reserves two cells for a landscape photo or four for a portrait.
  Cell counts round up, with a rectangular footprint chosen to suit the photo;
  a rectangle may require additional cells. Photos that cannot fit move to the
  next page, leaving free cells for following photos. A zoom larger than the
  whole configured grid produces an error. Fixed grids ignore field 2.
  In flexible mode,
  empty outer rows and columns are removed and panels expand into adjacent
  free cells. A single photo on a poster uses the whole image area below its
  title, within the configured margins, while keeping its aspect ratio.
- **TXT** opens configured text snippets and copies the selection to the clipboard.
- Keyboard shortcuts: Ctrl+O opens media, Ctrl+S saves, Ctrl+F enters/runs search.

## Manual synchronization

**Synchronize** exchanges the shared immutable journal with Android and the
server. `[SSH] Transport=sftp`, `rsync` or `ssh` selects SFTP, rsync over SSH,
or tar streams over SSH. No synchronization starts automatically. Configure
Host, Port, User, KeyFile, KnownHosts and DataDestination in setup.ini; the
existing Setup editor can edit these values. ShellDataDestination accounts
for a different path namespace on SSH hosts. The common root is `chaosbox-data`.
See [SYNC-PROTOCOL.md](SYNC-PROTOCOL.md) for exact paths and the format.
After exchanging the journal, synchronization also publishes current media under
`chaosbox-data/JPG/` with their original filenames and category subfolders.
Renames and explicit deletions update that view; conflicts retain its existing
copy until resolved. Unrelated server files are retained. SSH/rsync requires
`sha256sum` on the server.

The local installer preserves its per-installation Ed25519 key pair under
`.state/credentials`. Authorize its public key on the SSH server. Unknown host
keys are rejected. Other profiles use dedicated server subtrees by default;
explicit `[SSH.ProfileName]` overrides must match the same data set on Android.

Datensatz IDs, revisions and explicit tombstones replace timestamp comparisons.
Deletion moves media to trash; JSON entries are merged individually. Delete box
removes its records without deleting related media. Concurrent changes appear
under **Conflicts**; all versions remain in the journal. The CLI can inspect
history, restore a version, list device acknowledgments and retire a device.
There is no automatic garbage collection. Missing files alone never delete data.

```sh
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --transport rsync
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --conflicts
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --history OBJECT_ID
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --restore OBJECT_ID REVISION
```

## Verification

```bash
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 tests/tk_smoke.py
/usr/bin/python3 tests/tk_markdown_smoke.py
```

The Tk commands require a graphical session. Tests use temporary data and
cover metadata round-trips, encoded image/video preservation, JSON edits,
category persistence, search, installation, Markdown parsing/rendering and source
preservation, UI workflows and simulated SFTP.
They do not connect to the live SSH server.
# chaosboxDesktop
# chaosboxDesktop
# chaosboxDesktop
# chaosbox-desktop
===

The companion website and `push-data`/`pull-data` scripts use the same source layout
on both machines: media in `JPG/`, JSON sources in `TXT/` under the configured
data root. The shared journal is `chaosbox-data/TXT/.chaosbox-sync`; media versions are content-addressed blobs there. Existing installations
can override these with `ImageDestination` and `DataDestination` in their setup.ini.
Generated website catalogs live in `.indices/web-catalog.json` below the website
data root and are not JSON sources. Desktop keeps its shared `.indices` at the
installation level.

This host exposes htdocs as SFTP `/`. The absolute SSH/rsync path is configured separately with ShellDataDestination; both address the same journal.

## Independent installation synchronization

Each installation directory and each configured Box has its own server dataset
under `l1/storage/installations/<installation-id>/profiles/<box>/`. Without an explicit `Installation`, the target uses the local identity bound to
this computer and absolute path. Set `[SSH] Installation=...` (or a per-box
`[SSH.Haushalt]` override) to synchronize an existing server installation.
Copying a setup with an explicit Installation shares that server dataset.
KeyFile and KnownHosts accept absolute paths, `~`, and paths relative to the
installation root; omitted or blank values use `.state/credentials/`. Synchronization logs
show the ID; `sync-data.py --install-dir DIR --data BOX_DIR --show-target` prints
the effective destinations without transferring data. Android must explicitly
select that installation in `[SSH] Installation=...`. The website selector keeps
searches and image links within the chosen dataset. See SYNC-PROTOCOL.md.

Desktop installations use individual icon colors derived from their installation ID.
The application menu, desktop shortcuts, and window/taskbar icon share the same color.
Run `./install-desktop.sh` to refresh icons for all registered installations, then
reopen running windows. Existing desktop shortcuts copied outside the application
menu may need to be recreated from the updated menu entry.

## Installation reset contract

All records, media, journals, caches, setup and UI state belong below `--installdir`.
A random generation is stored in `.installation-id` inside that directory. Close the
app and remove the installation directory to reset it. Recreating even the same
path generates a new server namespace; old server journals cannot be downloaded
through the new default target. An explicit `[SSH] Installation=...` overrides
the server target, including when setup.ini is copied. Local identity and reset
checks remain independent of that target. External credential paths are supported;
external setup/state paths and custom synchronization destinations are unsupported.
Installation no longer seeds data from bundled assets or legacy folders. Existing
installations receive a fresh namespace on their first launch with this version;
existing local data stays local and is synchronized into that new namespace.

The sync target belongs to the website installation: by default
`l1/storage/installations/<generation>/profiles/<profile>/`. The website reads
that same `storage/` directory. `WebInstallation=l1` in [SSH] names the website
installation relative to the SSH home; use the same value on Android if changed.

Large SFTP journal uploads and downloads use up to four independent channels,
with automatic single-channel fallback if the server refuses extra channels.
Files are staged and renamed under the existing transfer lock; interrupted
transfers resume from already published immutable files. Media transfers remain sequential.

In **Select JPG / MP4**, select thumbnails and click **Del Selection** to remove
them from the dataset and their original disk locations. Confirm the displayed
count before deletion. Recovery copies remain in local trash and sync history;
use manual synchronization to propagate deletions to the server and Android.
Files outside the selected dataset cannot be deleted through this button.

### Fast unchanged media sync

Desktop SFTP, SSH and rsync save `TXT/.chaosbox-sync/media-checkpoint.json` after
successfully publishing the current JPG/MP4 view. An unchanged media journal and
unchanged remote file metadata skip full media content checks on subsequent syncs.
JSON-only edits do not invalidate this checkpoint. SFTP lists each managed folder
once instead of downloading every media file. SSH/rsync checks server-side stat
metadata instead of hashing every media file. The first sync after upgrading,
changing transport, or receiving a basic Android checkpoint verifies media once.

Missing media or changed size/timestamps trigger full verification; unknown remote
edits are retained and reported. Failed publication invalidates the checkpoint so
the next sync verifies again. This metadata cache is not a tamper check: SFTP can
miss same-size changes made within its timestamp resolution or with restored
modification times. Remove only `media-checkpoint.json` to force content verification.
Android can read the shared v1 fields; desktop adds its metadata fingerprint.

### Drag and drop media

Dropping media into the editor preserves all current text fields, including the
comment. The dropped files become the selected media; their embedded metadata
does not replace the form.

Drag one or more JPG/JPEG, PNG or MP4 files from the file manager onto the main
preview, the Open JPG/MP4 button, or the media-selection window. Existing form
values are retained; originals stay in place until an explicit save. Dropping is disabled during searches and background work.
Attaching media to a JSON record accepts one file. Native file drops use Ubuntu's
`tkdnd` package, included in the dependency installer and Debian package dependencies.

### Initialize synchronization from organized local files

Select the collection, organize its JSON files in TXT and its JPG/MP4 files in
JPG, then use ☰ → **Initialize sync from local files**. Save form edits first.
The action uses the selected collection's configured server installation and
remote profile; it does not create a new installation ID or touch other profiles.
It receives server history without applying it, then establishes the local files
as authoritative. Known records and media absent locally are marked deleted,
and all known conflict branches are resolved to the local state. An empty local
collection therefore clears the synchronized collection. Duplicate record IDs
in local files must be resolved first.

Recovery copies of local files and the received operation history are saved in
`TXT/.chaosbox-sync/reset-backups/<unique-id>/`. Existing journal history and media
blobs remain available. After completion, use Synchronize normally. Other devices
should synchronize before making edits; unsynchronized offline changes can still
produce conflicts. This action is not a permanent rejection of future changes
from other clients. If a transfer fails, retry initialization with the same local
files.

### Clean up synchronization recovery files

`cleanup-sync.py` is a standalone Python 3 script, installed alongside the app.
Select one collection directory containing TXT and JPG. Preview is the default:

```bash
python3 ~/.local/share/chaosbox/desktop/cleanup-sync.py --data ~/CBHaushalt/Haushalt
```

To permanently remove the previewed candidates, repeat with `--apply`. By default,
only entries older than 30 days are eligible. Use `--older-than-days 0` to include
all ages, or another nonnegative number for a different retention period. It
removes old `trash/` and `reset-backups/` entries and media blobs unreferenced by
any journal operation. A directory is eligible only when all its contents are
old enough. Both preview and deletion acquire sync locks; a running sync causes
cleanup to stop. No collection or journal is created if the path is wrong.

The script also works on server collection directories. For example, upload this
script to the server and run there with:

```bash
python3 cleanup-sync.py --data 'l1/storage/installations/INSTALLATION_ID/profiles/haushalt'
```

Inspect the preview before adding `--apply`. Active JSON/JPG files, journal
operations, deletion markers and every blob referenced by history are retained.
Deleted media therefore still occupies space in historical blobs. Removing that
history requires a coordinated protocol migration of every client, which this
cleanup script does not perform. Other clients can upload orphan blobs again;
cleanup is local to the chosen collection and does not propagate.

Desktop synchronization also materializes current JSON record heads in the server
collection's `TXT/` directory, for SFTP, SSH and rsync. This includes initialization
and subsequent syncs. Empty, moved or deleted JSON sources are removed;
paths involved in unresolved record conflicts keep their existing server files.
The immutable journal remains the synchronization history.

## Gesamtablauf: Desktop, Web und Android

Aus diesem Projekt starten (die Geschwisterprojekte `chaosbox-android` und
`chaosbox-kner-at` müssen daneben liegen):

```bash
./KBox-make-all.sh Bilder
```

1. Installiert/aktualisiert Desktop für `~/Kbox-Bilder` mit dem bestehenden Installer.
2. Lädt die Web-Anwendung zum Server aus den SSH-Einstellungen hoch (standardmäßig
   die Website sync-kner.at, Verzeichnis `l1`). Die Server-setup.ini bleibt erhalten.
3. Synchronisiert alle in der Installation konfigurierten Boxen bidirektional.
4. Baut eine signierte Release-APK mit der zentralen setup.ini dieser Installation;
   jede Box erhält explizit ihre Desktop-Installations-ID und ihr RemoteProfile.
   Desktop und Android verwenden dieselben `[App]`-Defaults und wiederholten
   `[Box]`-Abschnitte mit `Title`, `ImageWidth`, `Fields` und `Categories`.
   `ImageWidth` wird pro Box vererbt oder überschrieben.
   Desktop-Einstellungen ohne Android-Unterstützung bleiben auskommentiert.
5. Veröffentlicht `chaosbox-android/released/KBox-Bilder-<Version-ohne-Punkt>.apk`
   unter `l1/storage/apk/` mit abschließender atomarer Umbenennung.

Für eine bestehende Installation an einem anderen Ort:

```bash
./KBox-make-all.sh Bilder --installdir ~/CBBilder
```

Das Skript führt echte Uploads durch. Vorhandene lokale Einstellungen und Daten
bleiben erhalten. Eine neue Installation erhält das Standardsetup des Installers.
Der Installer übernimmt vorhandene Hostschlüssel aus dem Android-Projekt.
Vor dem ersten Upload prüft der Gesamtbuild den Installationsschlüssel und
autorisiert ihn bei Bedarf mit `ssh-copy-id` über eine bestehende Anmeldung
per SSH-Schlüssel oder SSH-Agent. Alle SSH-Aufrufe laufen ohne Passwortabfrage
(`BatchMode=yes`). Ohne bestehende Anmeldung muss der öffentliche
Installationsschlüssel einmal auf dem Server autorisiert werden. Fehlen diese Voraussetzungen, bricht das Skript ab; anschließend mit
demselben Aufruf fortsetzen. Android-Build und Signierung verwenden die vorhandene
Toolchain und `signing-keys` aus dem Android-Projekt. Die Produktversion muss in
allen drei Projekten übereinstimmen.

Die APK enthält den privaten SSH-Schlüssel und die bekannten Hostschlüssel dieser
Installation und ist deshalb nur für deren berechtigte Benutzer bestimmt.
Android übernimmt das eingebettete Setup und die Zugangsdaten bei einer frischen
Installation; vorhandene Android-Einstellungen werden bei Updates beibehalten.
Der Build nutzt ein temporäres Android-Projekt und verändert die Setup-Vorlage
im Quellprojekt nicht. Unterstützt werden Boxen aus der zentralen setup.ini mit
einem gemeinsamen Webserver und gemeinsamen SSH-Zugangsdaten; verschachtelte
Boxen und lokale Box-setup.ini werden vor dem Upload mit einer Fehlermeldung
abgewiesen. Bei einem Fehler stoppen die folgenden Schritte; bereits abgeschlossene
Installationen, Uploads und Synchronisationen werden nicht zurückgerollt.


Desktop and Android share the same setup entries: `[App]` supplies defaults,
repeated `[Box]` sections use `Title` as the folder name, and `ImageWidth`,
`Fields` and `Categories` inherit from `[App]` with per-box overrides. The first
Box is the default; Android can remember a different selection. Both use
`[TextSnippets]`, `[SSH]` and `[SSH.<Title>]`. The APK export keeps this format
and comments out Desktop-only `Language`, `editor`, `[Poster]` and SSH transport
options. Local credential paths are omitted from the APK setup.
Android migrates its former `[App.Name]`, `Standard` and `[ImageSize] LIMIT`
entries when the setup is opened/saved; original folder IDs and SSH targets
are preserved. No data folders are renamed by this migration.

Setup keys are English: `Title`, `Fields`, `Categories`. Legacy `Titel`, `Felder`, and `Kategorie` remain readable; saving a setup converts these keys while preserving values, comments and text snippets. Section names are already English and remain unchanged.

Desktop installations named `Kbox-NAME` or `KBox-NAME` display `KBox-NAME`
in the window, application header and application menu. Reinstall to refresh an
existing menu entry, then reopen running windows.
