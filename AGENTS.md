# Shared ChaosBox product version

Android, desktop and web share the product version in their root `VERSION` files.
The baseline is 1.0. For each subsequent user-requested behavior or UI change,
increment the minor number once (1.0 → 1.1 → 1.2) and update all three VERSION
files together, even if only one application changes. Treat follow-up corrections
within the same unfinished change as one change. Do not bump for questions,
status checks, tests alone, or commit/push requests. Explicit user version
instructions override this convention.

Display the version beside the (c) kner.at credit in every app. Android reads
VERSION at build time; keep its numeric versionCode increasing for updates.
Desktop and web installers must include VERSION. Product versions do not change
journal, cache, XMP, or desktop-entry format schema versions.
