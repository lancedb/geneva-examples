# UDF Studio sample data

Drop your own sample inputs here; UDF Studio reads from this directory (the
`--data-dir` default) to give your prototype something to run against. One
modality per location:

| Modality | Source                | Each sample is…                              |
|----------|-----------------------|----------------------------------------------|
| image    | `images/`             | the raw bytes of a file (`.png`, `.jpg`, …)  |
| video    | `videos/`             | the raw bytes of a file (`.mp4`, `.mov`, …)  |
| audio    | `audio/`              | the raw bytes of a file (`.wav`, `.mp3`, …)  |
| pdf      | `pdfs/`               | the raw bytes of a file (`.pdf`)             |
| text     | `input.csv`           | a cell from a chosen column                  |

Files in `images/`, `videos/`, `audio/`, and `pdfs/` are git-ignored (only
`.gitkeep` is tracked), so your media never gets committed. `input.csv` ships
with a few example rows — replace it with your own.

The directory is just the default; point the Studio at any folder with this
layout via the **Data directory** field in the UI or `udf-studio --data-dir`.
