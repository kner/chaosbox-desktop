"""Actionable presentation of errors; never changes recovery or synchronization behavior."""

RULES = [(['untracked'],
  'A destination file already exists, but its contents do not match a known synchronized version. '
  'KBox refused to overwrite or remove that file.',
  'Keep both: copy the existing file to a backup outside the synchronized JPG/TXT folders, verify '
  'the copy, then move the original out of the way and retry synchronization. Compare the '
  'downloaded version with your backup. To keep your local version, import it afterward as a '
  'separate item. Do not delete either copy before comparing them. For a remote file, perform '
  'these steps on the server.'),
 (['media filename conflict'],
  'Two different media items use the same destination filename. Synchronization cannot choose '
  'which item should occupy that path.',
  'Keep both items under different filenames, then synchronize again. On Android, long press '
  'synchronization and choose Repair filename conflicts. Back up the affected files before '
  'resolving the conflict.'),
 (['immutable',
   'incomplete journal',
   'invalid journal',
   'cyclic journal',
   'cross-object',
   'operation filename',
   'invalid revision',
   'unsupported sync operation',
   'invalid device acknowledgment',
   'invalid object action',
   'identity mismatch',
   'invalid blob',
   'unexpected journal',
   'checksum index too large'],
  'The synchronization history is inconsistent or uses an unsupported format. Applying it could '
  'associate changes with the wrong item.',
  'Stop synchronization on all devices and back up .chaosbox-sync. Check that all apps are up to '
  'date. Recover the missing or damaged history from a known-good device or backup with technical '
  'assistance; do not edit or delete journal entries to bypass this check.'),
 (['corrupt', 'digest mismatch', 'missing or corrupt'],
  'A media payload is missing, incomplete, or does not match its recorded checksum.',
  'Check free space and connectivity, then retry the transfer. If the error repeats, preserve the '
  'journal and recover the payload from a known-good device or backup. Do not replace it with an '
  'unrelated file or delete the history.'),
 (['already running', 'transfer.lock', 'synchronization is busy'],
  'Another synchronization owns the lock, or an interrupted run left a lock behind.',
  'Wait for the other synchronization to finish and retry. If it persists, stop all clients and '
  'ask for the lock to be checked. Remove a stale lock only after confirming no synchronization is '
  'running; never remove it during an active transfer.'),
 (['changed during', 'changed after', 'journal changed while'],
  'A file or synchronization state changed while the operation was using it.',
  'Finish edits and stop other transfers, then retry. Some earlier items may already have '
  'completed; review the current files before repeating a batch operation.'),
 (['cancelled', 'canceled', 'transfer stopped'],
  'The operation was interrupted before completion. Earlier steps may already have completed.',
  'Review the current state and start the operation again when ready. Synchronization can be '
  'retried; check completed items before repeating imports or other batch actions.'),
 (['host key', 'known_hosts', 'knownhosts', 'hostkey'],
  'The server identity could not be verified using the configured trusted host keys.',
  'Confirm the server address and verify its host-key fingerprint through a trusted channel. '
  'Update KnownHosts only after verification. Do not disable host-key checking.'),
 (['auth fail', 'authentication', 'credentials', 'ssh key', 'private key'],
  'The SSH credentials are missing, unreadable, or were rejected by the server.',
  'Check the configured user and KeyFile, and confirm that the matching public key is authorized '
  'on the server. Restore missing credentials from your secure backup; do not include private keys '
  'in an error report.'),
 (['permission denied', 'access denied', 'permissionerror'],
  'The app does not have permission to access the requested file or server destination.',
  'Check access to the displayed path. For server errors, check the SSH account and destination '
  'ownership. On Android, grant the required file access in system settings. Retry after '
  'correcting permissions.'),
 (['no space left', 'disk full', 'enospc'],
  'The destination has run out of storage space.',
  'Free space on the affected device or server, keeping your only copies and synchronization '
  'history intact, then retry.'),
 (['timed out',
   'timeout',
   'connection',
   'resolve server',
   'unknownhost',
   'name or service not known',
   'network',
   'no route'],
  'The server could not be reached reliably or the connection was interrupted.',
  'Check the network, server address, SSH port, and server availability, then retry. If it '
  'persists, check whether the server supports the selected SSH/SFTP transport.'),
 (['symlink',
   'symbolic',
   'escaping',
   'unsafe',
   'outside configured',
   'remain inside',
   'stay inside'],
  'The requested path is outside the allowed folders, is a symbolic link, or has an unexpected '
  'file type.',
  'Check the displayed path and configured folders. Use a regular file or directory inside the '
  'intended installation. Preserve the original data before replacing a link or correcting a path; '
  'do not bypass the path safety check.'),
 (['retired'],
  'This device identity has been retired from synchronization.',
  'Back up local changes and register a new device identity using the installation setup procedure '
  'before synchronizing again. Do not reuse or manually reactivate the retired identity.'),
 (['json', 'expected json', 'records must', 'not an object'],
  'The data file does not contain the expected JSON records.',
  'Back up the named file. Correct its JSON syntax and record structure, or restore a known-good '
  'copy, then reopen it. Do not replace it with an empty file if it contains data you need.'),
 (['duplicate', 'identity used for different'],
  'Multiple items share a name, path, or identity that must be unique.',
  'Inspect the named items and preserve both before making changes. Give independently created '
  'items distinct names; if their internal identities collide, re-import one as a new item instead '
  'of editing the journal.'),
 (['selected record',
   'existing conflict revision',
   'historical put revision',
   'unknown device',
   'not registered',
   'could not be registered'],
  'The selected item or revision is no longer available for this action.',
  'Refresh or reopen the box, then select an existing item or revision. Check synchronization '
  'history before restoring or deleting anything.'),
 (['setup',
   'profile',
   'installation',
   'ssh port',
   'ssh transport',
   'ssh host',
   'remote path',
   'sync destination',
   'box title',
   'imagewidth',
   'fields has',
   'language must'],
  'The configuration is incomplete, invalid, or no longer matches the installation.',
  'Open Setup and correct the setting identified in the details. Check the selected box and '
  'installation destination before retrying. Back up setup.ini before editing it manually.'),
 (['poster', 'select at least', 'select between'],
  'The selected images or layout cannot produce the requested poster.',
  'Adjust the selection or the size, margins, grid, and text settings identified in the details, '
  'then preview the poster again.'),
 (['metadata', 'mp4', 'cannot write', 'media reader'],
  'The media metadata could not be read or written in the expected format.',
  'Keep a backup of the original file. Check that it opens normally and that required media tools '
  'are installed. Try a supported media file; if this file alone fails, repair or convert a copy '
  'before importing it.'),
 (['no such file', 'not found', 'filenotfound', 'missing', 'unavailable', 'no longer available'],
  'A required file, folder, or program is unavailable.',
  'Check the displayed path, reconnect any external drive, and confirm that the file or required '
  'program exists and is accessible. Restore missing data from backup before retrying.'),
 (['quantity',
   'category',
   'filename',
   'single line',
   'regular expression',
   'unsupported',
   'select a',
   'must be'],
  'The supplied value or selected file is not accepted for this action.',
  'Correct the value or choose a supported file according to the details, then try again.'),
 (['exited with status', 'failed ('],
  'An external tool stopped with an error.',
  'Review the tool output and check its installation, input paths, and server access. Correct the '
  'reported cause before retrying.')]

FALLBACK = ('The requested operation could not complete. The details below are the available diagnostic information.', 'Review the details and the current files before retrying; earlier steps may have completed. If the error repeats, save this message and the steps that caused it for troubleshooting. Keep backups and do not delete synchronization history as a workaround.')

def describe(error, translate=lambda text: text):
    details = str(error).strip() or type(error).__name__
    if "\n\nWhat happened:" in details:
        return details
    search = (type(error).__name__ + " " + details).lower()
    problem, options = next(((problem, options) for keys, problem, options in RULES
                             if any(key in search for key in keys)), FALLBACK)
    return translate(details) + "\n\nWhat happened: " + problem + "\n\nYour options: " + options
