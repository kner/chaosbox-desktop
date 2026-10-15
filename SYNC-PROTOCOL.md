# ChaosBox-Synchronisation v1

Android, Desktop und Webanzeige verwenden dasselbe Journal. Der Transport
entscheidet nicht über den Datenstand. Änderungszeiten dienen nicht zur Konfliktauflösung.

## Gemeinsames Ziel

Für das Profil ChaosBox gilt auf dem ausgewählten Server:

- SFTP: `chaosbox-data/TXT/.chaosbox-sync/`
- SSH/rsync auf dem bestehenden Webspace:
  `/kunden/homepages/13/d983197478/htdocs/chaosbox-data/TXT/.chaosbox-sync/`
- Webanzeige: `CHAOSBOX_DATA_DIR` zeigt auf den absoluten Ordner `chaosbox-data`.

Andere Profile erhalten standardmäßig `chaosbox-data/profiles/<profilname>/TXT`.
Gleiche Datenbestände müssen auf allen Geräten auf dasselbe Ziel zeigen;
unterschiedliche Datenbestände brauchen unterschiedliche Ziele.

Desktop `setup.ini`, optional auch in der Konfiguration eines einzelnen Profils:

```ini
[SSH]
Transport=rsync
DataDestination=chaosbox-data/TXT
ShellDataDestination=/kunden/homepages/13/d983197478/htdocs/chaosbox-data/TXT
```

`Transport=sftp`, `rsync` oder `ssh` ist möglich. `ssh` verwendet Tar-Datenströme
über SSH, `rsync` läuft über SSH. `ShellDataDestination` berücksichtigt einen
anderen Pfadraum für SSH als für SFTP; optional überschreibt
`SftpDataDestination` das SFTP-Ziel. Für den bestehenden Host ist der absolute
SSH-Standardpfad bereits voreingestellt. Host, Port, Benutzer, Schlüssel und
KnownHosts bleiben konfigurierbar. Die vorhandene Setup-Bearbeitung kann diese
Werte ändern; zusätzlich gibt es die Desktop-CLI:

```sh
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --transport rsync
```

Android verwendet SFTP. In `ChaosBox/Setup/setup.ini` können `[SSH]` und ein
profilspezifisches `[SSH.Bilderbox]` Host, Port, User und DataDestination
überschreiben. Der vorhandene private SSH-Schlüssel und KnownHosts werden
weiterverwendet; unbekannte Hostschlüssel werden nicht automatisch akzeptiert.

## Format und Austausch

Lokal bleiben Medien in JPG und Datensätze in TXT beziehungsweise im
konfigurierten Android-JSON-Ordner. JSON-Dateien bleiben Listen von Datensätzen;
alte Einzelobjekte und gruppierte Objekte werden beim Übernehmen unterstützt.
Jeder Datensatz und jedes Medium erhält eine dauerhafte UUID `id` sowie eine
`revision`. Neue Medien speichern sie in `Xmp.chaosbox.id` und
`Xmp.chaosbox.revision`; MP4 verwendet dieselben JSON-Felder im Comment.
Beim ersten Abgleich erhalten auch übernommene Medien diese Felder. JSON-
Zusatzfelder und alte count/pack-Aliase bleiben erhalten.

Das Journal liegt im JSON-Ordner unter `.chaosbox-sync`:

- `ops/<revision>.op`: unveränderlicher JSON-Schnappschuss einer Änderung.
- `blobs/<sha256>.blob`: unveränderliche vollständige Medienversion.
- `device`: lokale Geräte-ID, **nicht übertragen**.
- `materialized.json`: lokale Prüfsummen der mit Metadaten versehenen Medien,
  **nicht übertragen**.
- `conflicts.json`, `trash/`, `local.lock`: nur lokaler Zustand, **nicht übertragen**.

Übertragen werden ausschließlich ops und blobs. Das Journal enthält auch
Gerätebestätigungen und Austragungen. Normale Medien und JSON-Dateien werden
anschließend lokal aus dem zusammengeführten Stand hergestellt. Die Webanzeige
liest die Projektion direkt, ohne Quelldaten zu verändern.

Ein Änderungseintrag enthält mindestens:

```json
{
  "schema": 1,
  "id": "6c7fd779-555b-4cf7-b5ba-22db6a02dac4",
  "revision": "0badb353-6265-49e4-a462-bc2f453c64a2",
  "parents": ["63ca2d5e-aa7b-44cf-b17c-8c80aa6da745"],
  "kind": "record",
  "path": "a11.json",
  "action": "delete",
  "device_id": "2807a806-99cb-49a2-ac36-a8395ccad1f2",
  "issued_at": "2026-10-14T14:00:00+00:00"
}
```

`kind` ist record, media oder device. Bei put enthalten Änderungen `record`
und bei Medien zusätzlich `blob`. Die Pfade sind relativ zum jeweiligen
Medien-/JSON-Ordner; absolute Pfade, versteckte Pfadkomponenten und Traversal
werden abgelehnt. Eine neue Revision benennt ihre Vorgänger in parents.
Zeitstempel sind nur zur Anzeige bestimmt. Bei einer einzelnen Vorgängerrevision speichert `base_revision` deren ID auch im Datensatz/XMP. Damit kann eine nach einem Prozessabbruch bereits geschriebene Mediendatei sicher erneut ins Journal übernommen werden; eine unbekannte Bearbeitungsbasis wird als Konflikt erhalten. Vorhandene Daten ohne ID erhalten
plattformübergreifend identische UUID-v3-IDs aus Typ, relativem Pfad und
Datensatzposition. Bei gruppierten JSON-Objekten werden die Schlüssel zur ID-Vergabe nach Unicode sortiert. Ihre erste Revision ist eine deterministische SHA-256-ID;
spätere Revisionen verwenden UUIDs. Daher sind getrennt übernommene identische
Altbestände kein Konflikt.

## Löschen, Konflikte und Wiederherstellen

Eine fehlende Datei erzeugt **keinen** Löschauftrag. Löschungen erfolgen über
die Programme oder die CLI und werden als action=delete übertragen.
Eine alte Offline-Kopie wird damit nicht wieder zum aktuellen Stand.

- Einzelne JSON-Datensätze werden unabhängig voneinander gelöscht und gemischt.
- „Delete box“ markiert alle enthaltenen Datensätze zur Löschung. Eine leere
  JSON-Liste darf als Dateihülle verbleiben.
- Zugeordnete Medien werden beim Löschen einer Box nicht mitgelöscht.
- Medien werden lokal in den Papierkorb verschoben. Frühere JSON-Stände und
  ersetzte Medien bleiben ebenfalls wiederherstellbar.
- Bearbeitung gegen Löschung oder mehrere parallele Bearbeitungen ergeben
  mehrere aktuelle Revisionen. Alle Versionen bleiben erhalten. Ein explizites
  Auswählen einer Version erzeugt eine neue Revision mit allen Konfliktköpfen
  als Vorgängern. Kein Zeitstempel gewinnt automatisch.
- Belegen unterschiedliche Medien-IDs denselben Dateinamen, stoppt die
  Herstellung dieses Standes vor dem Überschreiben. Eine Version umbenennen
  und erneut synchronisieren.

Desktop: „Conflicts“ zeigt die Revisionen mit vollständigen Metadaten;
„Keep selected version“ entscheidet ausdrücklich. Android: lange auf den
Synchronisationsknopf drücken; dort gibt es Konflikte und gelöschte Einträge.
Kurzes Antippen des Papierkorbs löscht den ausgewählten Eintrag; langes Drücken
löscht nach Rückfrage die ganze JSON-Box. Nach einem Abgleich muss eine bereits
geöffnete Auswahl erneut geladen werden, bevor sie gespeichert wird; die
Formularwerte bleiben erhalten.

Desktop-CLI (IDs/Revisionen durch die angezeigten Werte ersetzen):

```sh
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --conflicts
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --resolve OBJECT_ID REVISION
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --history OBJECT_ID
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --restore OBJECT_ID REVISION
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --delete ~/ChaosBox/ChaosBox/TXT/a11.json --record 0
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --delete ~/ChaosBox/ChaosBox/TXT/a11.json --whole-box
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --devices
/usr/bin/python3 sync-data.py --data ~/ChaosBox/ChaosBox --retire DEVICE_ID
```

Gerätebestätigungen (kind=device, action=ack) enthalten die bekannten
Datenrevisionen in seen. Sie werden nach erfolgreicher lokaler Anwendung
veröffentlicht. Austragen erzeugt eine retire-Revision. Ein ausgetragenes
Gerät muss eine neue lokale Identität erhalten, bevor es wieder bestätigt.
Zum erneuten Anmelden eines ausgetragenen Gerätes bei gestoppter App nur die lokale Datei `.chaosbox-sync/device` nach Sicherung entfernen; das Journal bleibt erhalten, und die nächste Sitzung erzeugt eine neue Geräte-ID.
Es gibt bewusst **keine automatische Bereinigung** des Journals oder des
Papierkorbs. Auch bestätigte Löschmarkierungen und frühere Medien bleiben
bis zu einer gesondert geplanten Bereinigung erhalten; alte Offline-Geräte
können dadurch keine entfernten Daten wieder einführen.

## Betrieb und Wiederanlauf

Vor dem ersten Austausch alle schreibenden Clients aktualisieren; alte Programme mit Zeitstempel-Uploads dürfen nicht parallel weiter synchronisieren. Den vollständigen bisherigen Bestand auf einem
Desktop übernehmen. Vorhandene Daten nur auf dem alten Server sind zuerst
als Bestand zu importieren; die Programme raten aus fehlenden lokalen Dateien
keine Löschung. Danach dürfen Android und Desktop unabhängig offline arbeiten.
Der Server enthält die Vereinigung aller Änderungen, keinen führenden Rechner.

Jeder Transfer nimmt `transfer.lock` atomar per Verzeichniserstellung.
Downloads gehen zunächst in ein temporäres Verzeichnis; Historie, Pfade und
SHA-256-Prüfsummen werden vor der Übernahme geprüft. Medien werden vor ihren
Änderungseinträgen veröffentlicht. Unterbrochene Transfers lassen sich wiederholen.
Nach einem Prozessabbruch eine eventuell verbleibende local.lock/transfer.lock
nur entfernen, nachdem sicher kein zugehöriger Prozess mehr läuft.

`push-data`/`pull-data` übertragen dasselbe Journal mit rsync, ohne --delete.
Nicht die kompletten Geräteordner spiegeln: lokale Identität, Papierkorb,
Konfiguration und Indizes dürfen nicht zwischen Geräten kopiert werden.

Die Webanzeige erkennt neue Journaleinträge und aktualisiert ausschließlich
`.indices/web-catalog.json`. Bei laufender oder unvollständiger Übertragung
bleibt ein vorhandener gültiger Katalog verfügbar. Der PHP-Benutzer braucht
Leserechte auf die Daten und Schreibrechte auf `.indices` sowie auf den schon
vorhandenen Authentifizierungs-Cache `.auth`; keine Schreibrechte auf JPG/TXT.
Ohne Journal bleibt die bisherige manuelle Indexierung gültig.

Das Journal enthält auch gelöschte Versionen und darf nicht direkt per HTTP
abrufbar sein. Die Transferprogramme legen bei Bedarf eine Apache-.htaccess
mit `Require all denied` im Journal an. Bei Nginx den Zugriff auf
`/.chaosbox-sync/` in der Serverkonfiguration sperren oder die Daten außerhalb
des Webroots ablegen. Authentifizierte aktuelle Bildabrufe laufen über image.php.

Die Protokollimplementierung ändert keine Serverdaten beim Installieren/Bauen.
Erst ein ausdrücklich gestarteter Abgleich überträgt Änderungen.

## Prüfsummenindex und inkrementeller Abgleich

`TXT/.chaosbox-sync/checksums.json` liegt lokal und auf dem Server. Das zusätzliche,
optionale Format hat `schema: 1`, `algorithm: "sha256"`, `ops` und `checksum`.
`ops` ordnet jedem unveränderlichen `.op`-Dateinamen die SHA-256-Prüfsumme seines
kanonischen JSON-Inhalts zu. `checksum` ist die SHA-256-Prüfsumme der kanonisch
sortierten `ops`-Zuordnung. Da Medienoperationen die SHA-256 ihres Inhalts
enthalten, beschreibt der Index auch die referenzierten Medienversionen.
Gerätebestätigungen, Löschungen und historische Versionen gehören ebenfalls dazu.

Beim Speichern/Erfassen lokaler Änderungen und beim Anwenden des Journals wird
der lokale Index erneuert. Nicht erfasste Änderungen durch externe Programme
werden erst beim nächsten lokalen Scan erkannt. Gleiche Prüfsummen beschreiben
denselben Journalstand; sie ersetzen diesen Scan nicht. Lokale, noch nicht
hochgeladene Änderungen dürfen eine andere Prüfsumme als der Server haben.

SFTP-Indizes enthalten zusätzlich `inventory`: Dateigröße und Änderungszeit
jeder entfernten Operation. Diese serverabhängigen Werte gehören nicht zur
Gesamtprüfsumme. Unter der Transfersperre wird die aktuelle Server-Dateiliste
mit dem Index verglichen. Nur bei gültigem Index und passendem Bestand werden
bereits lokal vorhandene Operationen mit identischer Inhaltsprüfsumme nicht
erneut heruntergeladen. Fehlende Operationen werden anhand der `ops`-Zuordnung
übertragen. Neue Medien und empfangene Operationen durchlaufen weiterhin die
vorhandene Integritäts-, Historien- und Konfliktprüfung.

Alle Daten werden zuerst übertragen. Erst danach wird der Serverindex unter
der Transfersperre ersetzt und lokal gespeichert. Bei einem Abbruch bleibt ein
alter oder fehlender Index zurück; er kann aus den unveränderlichen Journaldateien
rekonstruiert werden. Ein fehlender, beschädigter oder nicht zum Serverbestand
passender Index bewirkt einen vollständigen Journalabgleich. Ältere Schreiber
bleiben damit kompatibel: neue Operationen machen ihren alten Index ungültig.
SSH/rsync veröffentlichen dieselbe Gesamtprüfsumme; ein Serverinventar wird beim
nächsten SFTP-Abgleich ergänzt. Die Webanzeige liest weiterhin das Journal und
muss den Index nicht schreiben.

Journaldateien niemals nachträglich bearbeiten. Wie bisher sind sie unveränderlich;
eine Bearbeitung erzeugt eine neue Operation. Die Inventarprüfung ist ein Cache
für dieses Protokoll, kein Manipulationsnachweis gegen einen kompromittierten
Server. Beim Verdacht auf manuelle Journaländerungen `checksums.json` entfernen,
damit der nächste Abgleich alle Operationen erneut prüft.
