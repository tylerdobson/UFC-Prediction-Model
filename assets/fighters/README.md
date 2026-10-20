# Fighter portraits

The dashboard displays official UFC athlete portraits from the public image URLs
in `manifest.json`. Each fighter links to the corresponding UFC athlete profile;
the dashboard credits UFC.com. These are actual photographs, not generated faces.

Portrait files are loaded directly by the viewer's browser and are not bundled
in this repository. Internet access is needed for the portraits; saved statistics,
forecasts, fonts and downloads remain local. Image availability and appearance
can change at UFC's source. The manifest records the URLs, dimensions and SHA-256
hashes observed when the images were checked, without treating those images as
historical model inputs or evidence of future fight outcomes.

To update a portrait, review the official profile and update its manifest entry.
If no matching entry exists, the dashboard shows initials. Images and UFC marks
remain the property of their respective rights holders. The project is independent
and is not affiliated with UFC.

The Barlow and Barlow Condensed fonts in `../fonts/` are distributed under the
included SIL Open Font License files.
