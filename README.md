# Machina CAD De-ID

A resumable pipeline for the Machina CAD De-ID Buildathon track. It processes
the published manifest in stable order, limits local staging, records private
resume state, and can upload completed batches to a team's Builders folder.

## Constraints and safety

- Use Ubuntu on WSL2 (or another case-sensitive Linux filesystem). The challenge
  corpus contains case-sensitive paths and native CAD formats.
- The raw input, manifest, SQLite run state, pseudonym key, upload token, and
  scratch output belong outside the submitted attempt. `.state/` and `.scratch/`
  are ignored by Git.
- `--scratch-limit-gb` defaults to 10. A single file larger than the limit is
  recorded as an omission instead of silently overflowing the working disk.
- The Builders folder is private. The pipeline does not configure AWS, make a
  bucket public, submit an attempt, or send a message to organizers.
- Unsupported binary CAD formats are retained intact and listed as exceptions;
  this preserves usability but does not satisfy checks for identifying data
  inside those files. Unsupported archive members are retained and reported
  while archive paths and supported members are still processed.

## Setup (Ubuntu / WSL2)

```bash
cd /mnt/c/Projects/Buildathon_Mechina_CAD
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
```

OCR requires the system Tesseract executable. The Python OCR package alone is
not enough. If Tesseract is unavailable, image text is not claimed to be
redacted; the affected files are recorded in the exceptions report.

On Ubuntu / WSL2, install OCR support with `sudo apt-get install tesseract-ocr`
and install the optional Python package with `python -m pip install -e .[ocr]`.

## Commands

Inspect the available commands and options:

```bash
machina-deid --help
machina-deid run --help
```

Fetch and parse the official TSV manifest, then process a small bounded sample
before attempting the full corpus:

```bash
machina-deid run \
  --work-dir .state \
  --scratch-dir .scratch \
  --max-files 100 \
  --scratch-limit-gb 10 \
  --attempt 1
```

For a full run, choose **Get upload token** on the official track page and pass
it in `MACHINA_UPLOAD_TOKEN` (or use `--token-file` for a token file that can be
updated when the one-hour token expires). Keep the destination in your team's
private Builders folder:

```bash
export MACHINA_UPLOAD_TOKEN='token-from-track-page'
machina-deid run \
  --work-dir .state \
  --scratch-dir .scratch \
  --scratch-limit-gb 10 \
  --attempt 1 \
  --dest 'gs://builders-machina-submissions-1120/teams/TEAM_ID/attempt-1/'
```

The uploader asks for a replacement token in an interactive terminal if the
current token expires. With `--token-file`, write the newly generated token to
that file when prompted. Each uploaded batch is verified by the storage PUT
response before its local scratch files are removed. The same command resumes
from SQLite state after interruption. Use a new `--attempt` value for each
scored attempt; never change a remote attempt after submitting it.

The command creates `README.txt` and uploads it with the source code after the
corpus is complete. Review the exceptions and category-level score before
deciding whether to submit the attempt on the track page.

## Format coverage

The implementation favors content-preserving edits for text-based CAD and
document formats, ZIP-family packages, PDFs, and OCR-capable raster images.
Native proprietary CAD binaries are not rewritten unless a safe parser is
available. This avoids corrupting geometry but leaves known coverage gaps that
must be reviewed in the generated `README.txt`. There is no dedicated logo
detector, so identifying marks in raster artwork or unsupported embedded
previews may remain. PDF attachments that cannot be safely updated are reported
per file.

