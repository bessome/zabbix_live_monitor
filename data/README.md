# Offline MAC database

`mac-vendors.json.gz` is a compact snapshot derived from
https://github.com/runZeroInc/mac-tracker (`data/macs.json`).
The upstream MIT license is included in `mac-tracker-LICENSE.txt`.
The snapshot retains the latest organization and earliest observed date per
24-, 28-, or 36-bit prefix; blocks whose latest record is a deletion are omitted.
Longest-prefix matching is required (36, 28, then 24 bits).

Historical dates come from archived registry observations. They are not guaranteed
IEEE allocation dates and do not determine the age of an individual device.

The bundled copy makes the application work offline immediately. Admin can
download an updated copy through Settings. Updates are validated and atomically
stored beside `APP_DB_PATH` as `mac-vendors.json.gz`; the previous copy survives
failed downloads. In Compose this directory is the persistent `/data` volume.
The application falls back to the bundled copy if the updated copy is unreadable.
Individual MAC addresses are never sent to the database provider.
