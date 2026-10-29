# Desktop source reference

This reference covers every versioned production source, script, and test source. Runtime modules and application assets live in `src/`; command-line and build tools live in `scripts/`. The root `KBox-make-all.sh` remains the release entry point. The desktop application stores local media and JSON records, then exchanges an immutable synchronization journal with the web service and Android.

## Runtime modules

| File | Responsibility |
| --- | --- |
| `src/app.py` | Tk user interface: collection selection, metadata editing, media previews, searching, poster creation, and background tasks. `App` coordinates all UI state; dialog and canvas classes isolate individual controls. |
| `src/core.py` | Domain layer for setup parsing, profile discovery, media metadata, JSON records, indexes, posters, searching, deletion, and synchronization orchestration. `Settings` derives safe local and remote paths; `Profile` represents a selected box. |
| `src/chaos_sync.py` | Transport-independent immutable journal. `Store` captures local changes, merges operations, detects conflicts, applies records and media, and maintains recovery history. |
| `src/sync_transport.py` | SFTP, SSH, and rsync transport adapters. It locks the remote journal, transfers operations and blobs, writes JSON/media projections, and maintains manifests/checkpoints. |
| `src/i18n.py` | Translation tables and lookup helpers for the desktop UI. |
| `src/markdown_render.py` | Small, safe Markdown-to-Tk-text renderer used by help/content dialogs. |

## Command-line and installation tools

| File | Responsibility |
| --- | --- |
| `scripts/sync-data.py` | CLI for manual synchronization, conflict inspection, history lookup, restore, and target reporting. |
| `scripts/cleanup-sync.py` | Finds recoverable synchronization debris that is older than the retention period and optionally removes it under journal locking. |
| `scripts/install.py` | Installs or updates desktop applications, credentials, launchers, icons, and desktop entries while preserving existing installation setup. |
| `scripts/uninstall.py` | Removes an installation's launcher and desktop integration without deleting user data implicitly. |
| `scripts/build_user_installer.py` | Builds a self-extracting per-user installer and exports installation-specific synchronization settings. |
| `scripts/build_user_uninstaller.py` | Builds a matching self-extracting uninstaller. |
| `scripts/make_all.py` | Release pipeline: validates shared versions, uses the installation setup as APK input, deploys web code, synchronizes boxes, signs Android, builds installers, and publishes artifacts. |
| `KBox-make-all.sh` | Strict root-level shell entry point for `scripts/make_all.py`. |
| `scripts/install-desktop.sh` / `scripts/uninstall-desktop.sh` | Shell launchers for installation and removal. |
| `scripts/run-desktop.sh` | Starts the application after checking its Python dependencies. |
| `scripts/install-dependencies.sh` | Installs/checks optional desktop runtime dependencies. |
| `scripts/ibilder.sh` / `scripts/ihaus.sh` | Convenience installers for the Bilder and Haushalt installation directories. |

## Test sources

| File | Coverage |
| --- | --- |
| `tests/test_app.py` | UI-independent application helpers and state behavior. |
| `tests/test_core.py` | Setup, metadata, records, search, index, poster, and media operations. |
| `tests/test_sync.py` | Immutable journal capture, merge, conflict, recovery, and initialization behavior. |
| `tests/test_sync_transport.py` | SFTP/SSH/rsync protocol details, projections, locks, and checkpoints. |
| `tests/test_installation_sync.py` | Installation identity and cross-client target isolation. |
| `tests/test_make_all.py` | Release pipeline order, APK setup export, artifact publication, and configuration safeguards. |
| `tests/test_user_installer.py` | Installer payloads, per-user setup export, and uninstaller generation. |
| `tests/test_cleanup_sync.py` | Retention plans, locking, and cleanup safety. |
| `tests/test_media_delete.py` | Local deletion and synchronized deletion semantics. |
| `tests/test_poster.py` | Poster geometry, captions, colors, pagination, and rendering. |
| `tests/test_markdown.py` | Markdown rendering rules. |
| `tests/test_setup_english.py` | English/German setup-key normalization. |
| `tests/test_drag_drop.py` | Drag-and-drop parsing and media selection. |
| `tests/test_uninstall.py` | Launcher and desktop-entry removal behavior. |
| `tests/gtk_drop_smoke.py`, `tests/tk_drop_smoke.py` | Optional native drag-and-drop smoke checks. |
| `tests/tk_markdown_smoke.py`, `tests/tk_smoke.py` | Optional Tk widget/rendering smoke checks. |

Run the Python suite with `python3 -m unittest` from this repository. The protocol contract is documented separately in `SYNC-PROTOCOL.md`.
