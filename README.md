# ChaosBox Desktop for Ubuntu 24.04

A native Python/Tk application using the same metadata fields, JSON records and
setup profile format as the Android app. No browser or Android emulator is needed.
The interface is in English; category names, custom labels and snippets retain
their configured language.

## Install and launch

On Ubuntu 24.04 the dependencies are available from Ubuntu packages:

```bash
sudo apt install python3-tk python3-pil python3-pil.imagetk python3-paramiko libimage-exiftool-perl ffmpeg xclip fonts-dejavu-core
```

From the repository root:

```bash
./run-desktop.sh --check
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

Launch **Chaosbox-cb1** or **Chaosbox-cb2** from the Ubuntu application menu.
Each launcher is permanently bound to its installation directory. Shared application files
are stored under `~/.local/share/chaosbox/desktop`; box launchers are stored under
`~/.local/share/chaosbox/installations/<installation-id>`;
menu entries are `~/.local/share/applications/<installation-id>.desktop`.
The ID and window class depend on the full installation path, so directories
with the same final name still have distinct launchers. Run the installer once
with any box path to update the application for **all installed boxes**, preserving
each box's setup and data. Existing per-box launchers are migrated automatically.
All boxes use the new version on their next launch; reopen any running windows.
No administrator privileges are needed when dependencies are already present.

## Debian package (Ubuntu 24.04)

Build an architecture-independent installer using the system Python and dpkg:

```bash
cd desktop
/usr/bin/python3 build_deb.py
sudo apt install ./dist/chaosbox-desktop_1.0.2_all.deb
```

Launch **ChaosBox Desktop** from the application menu or run `/usr/bin/chaosbox`.
The package installs to `/usr/share/chaosbox-desktop`; apt installs its runtime
dependencies. It includes the current full-screen and file-operation fixes.
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
`[Box]` sections declare projects; `Titel` is the folder name. `JPG=` and `Daten=`
are removed because the subfolders are always `JPG` and `TXT`.

```ini
[App]
Felder=Box,Quantity,Device,Alias,Category,Comment,Package
Kategorie=Werkzeug,Elektronik

[Box]
Titel=Bilderbox
Felder=Box,,,Tags,Category,Comment
Kategorie=Familie,Berg,Urlaub

[Box]
Titel=Chaosbox1
Kategorie=Haushalt,Garten
```

Each project inherits its parent's fields and settings, with independent
categories when specified. An optional `Bilderbox/setup.ini` can override
`[App]`, `[ImageSize]`, `[Poster]`, `[TextSnippets]` and `[SSH]`, and declare more
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

- **Open JPG / MP4** shows a scrollable thumbnail grid with **1–10 columns**.
  The window can be maximized and restored using its title-bar controls.
  Only files directly in the current folder are shown. **Choose folder …** changes
  folders. The last selected folder is remembered per profile across restarts,
  including folders used through **Other files …**.
  Double-clicking a folder in the folder picker selects it and opens its images
  immediately. Enter a folder path and press Enter to navigate directly.
  Click tiles to toggle multiple selections, Shift-click to select a range, or
  use **Select all** / **Clear**. The selection survives column changes.
  Previews load in the background; unavailable previews remain selectable.
  Thumbnail size follows the column width while preserving image proportions.
  **Show UserComment** displays **box | category | comment** below each thumbnail,
  limited to 60 characters in total. Comments load in the background when enabled.
  **Other files …**
  also imports PNG files. The first selected file supplies the preview. Selection
  order is preserved; deselecting and reselecting a tile moves it to the end.
  Select all appends unselected tiles in display order.
  Multiple files show only identical metadata values. Mixed values appear blank.
  **Save** asks “Attention! All selected Images get this texts” for multiple
  selected files. OK proceeds; Cancel leaves files and form inputs untouched.
  **Save** preserves unchanged fields independently for each file. Entering a value
  in an initially blank field appends it to each file (comma separator, newline
  for Comment); Quantity adds the entered number. Editing a shared nonempty field
  replaces that field. Other metadata and each file's creation date are retained.
  After a successful **Save**, all form fields and the selection reset just as
  with **Clear all**. Cancelled or failed saves keep the inputs.
  Local saves refresh only the search index entries of the saved files.
  Startup loads the shared index, building it only when absent or with `--newindex`.
  Searches and profile switches reuse it. Run with `--newindex` after external
  changes or profile configuration changes, or to replace a damaged cache.
- Saved media goes into the category folder, or **unassigned** when no category
  is specified. Existing profile media moves there when its category changes;
  media already in the correct folder is updated in place. Imports receive a separate `_cb`
  filename; name collisions get numbered suffixes. PNG files become JPG with a
  white background. Images exceeding `[ImageSize] LIMIT` are resized. Smaller
  JPGs retain their encoded image data; MP4 video/audio is not re-encoded.
- JPG metadata uses EXIF `UserComment`. MP4 metadata uses ItemList `Comment`;
  existing Keys comments are synchronized. JSON strings use the Android field
  names, including `anzahl` and `package`.
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
  Saving a selected record preserves unknown fields and its creation date.
- **Search** first enters search mode; pressing it again runs the query. Queries
  use case-insensitive Python regular expressions. Nonempty fields are combined
  with AND. Category and Device also search Comment; Comment and Alias search
  Device, Alias and Comment. Python-specific regex syntax can differ from Java.
  **Cancel search** restores the form; **Repeat search** reuses the last query.
- Double-click an image preview for full-screen viewing. Zoom using the mouse
  wheel or +/−, drag to pan, double-click for 2.5×/reset, and press Escape to close.
  Zoom reaches 8×; full-screen images are loaded up to 12000 pixels per side.
  **COPY** copies the displayed image as PNG to the clipboard for pasting with
  Ctrl+V (requires `xclip`; also works in Ubuntu's XWayland session).
  MP4 files show a still preview, without an embedded video player.
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
  pixels, independent of the normal image LIMIT. All four margins are configured in millimeters;
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
  composited over that color. Individual source images are never cropped. Its title contains the
  current **Category** text box content, used only for this export. A blank
  Category leaves the title blank. Each image is captioned as `box: comment`
  from its saved EXIF metadata, wrapped across lines. Newly entered Box and
  Comment values are appended to the existing caption; unchanged form values
  are not duplicated. Poster export does not save these fields to source files. Text space is reserved before resizing images.
  Equal grid cells and 3 mm gutters distribute the image/caption groups evenly.
  Long text uses a smaller font; text that cannot fit produces an error instead
  of being cut off. JPEG print resolution is derived
  from these settings. Every image keeps its aspect ratio, is centered, and fits
  without cropping; unused panel space uses IMAGE-BACKGROUND-COLOR. When the grid is incomplete, the last image occupies all remaining full
  rows. Earlier images fill preceding rows in selection order; a partial row
  divides its width evenly among its images. For ROWS=3, COLS=3 and three images,
  the first two share row 1 and the last spans rows 2–3 across the full width.
  If no full row remains, the final row shares its width evenly. A single image
  uses the whole grid area. A full grid keeps equal cells. POSTER-FIX remains
  accepted for compatibility; both values use this layout. Selection must fit within COLS × ROWS; excess images
  produce an error instead of being silently omitted.
- **TXT** opens configured text snippets and copies the selection to the clipboard.
- Keyboard shortcuts: Ctrl+O opens media, Ctrl+S saves, Ctrl+F enters/runs search.

## Manual SSH upload

**Upload saved files** sends saved data; it never runs automatically. Host, port,
user, destinations, key file and known-hosts file are configured in `[SSH]`.
The local installer reuses the project's existing Android key and known-hosts
files when available, placing private copies in the credentials directory.
Existing credential files are retained. Keys are never included in desktop source
packages. Connections require a known host key and public-key authentication.

As with Android, only profile paths inside `ChaosBox` are eligible for upload;
the default Bilderbox profile is excluded. Relative subdirectories are retained.
Files are sent when missing remotely or locally newer, using a temporary remote
file and atomic rename. Remote extra files are not deleted. Cancel or close the
upload dialog to stop. The server must support OpenSSH's POSIX rename extension.

## Verification

```bash
/usr/bin/python3 -m unittest discover -s desktop/tests -v
/usr/bin/python3 desktop/tests/tk_smoke.py
```

The second command requires a graphical session. Tests use temporary data and
cover metadata round-trips, encoded image/video preservation, JSON edits,
category persistence, search, installation, UI workflows and simulated SFTP.
They do not connect to the live SSH server.
# chaosboxDesktop
# chaosboxDesktop
# chaosboxDesktop
# chaosbox-desktop
