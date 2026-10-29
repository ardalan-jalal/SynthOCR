# Small, private hosting

The web app is a single-user tool. Keep it on `127.0.0.1`, including on a server.
For access from another computer, use an SSH tunnel. A public URL needs a separate
authenticated gateway and deployment review; this image does not add public access.

## Run without Docker

```sh
uv sync --locked --extra web
uv run --extra web synthocr ui --no-browser --output-root /your/output/folder
```

Use one app process for the configured storage locations. Generation is
serialized across them; additional Uvicorn workers would duplicate caches and
break the single-job boundary. Start with one render worker and raise it only
after measuring memory and throughput. Native installation and RAQM requirements
are described in [the contributor guide](development.md).

## Configure storage locations

The app generates into its own workspace on the server; these locations are
maintenance configuration and never appear on the page. A default location comes from `--output-root` (or `dataset/ui/` relative to the config),
and up to eight additional locations use repeated `--output-location NAME=PATH`:

```sh
uv run --extra web synthocr ui --no-browser --output-root dataset/ui --output-location archive=dataset/archive --output-location team-data=dataset/team
```

Location names are lowercase IDs beginning with a letter, followed by letters,
digits or hyphens, at most 32 characters. `default` is reserved. Paths resolve
from the configuration file unless absolute; quote the entire `NAME=PATH`
argument when the path includes spaces. On Windows, for example,
`--output-location "archive=D:\OCR datasets"` uses that server's D: drive.

Provide writable directories separate from corpora and fonts. The app locks
every configured location and keeps one generation executor. The compatible directory APIs can
list and create child folders inside these roots; it cannot traverse outside
them or browse through symlinks. Listings are bounded, and large directories
may be marked incomplete. Dataset creation requires a fresh name in an existing
parent folder, so a generated dataset is never overwritten or used as a parent.

Job history, uploads, fonts users add and the shared download cache stay under the default
location. Keep that location persistent alongside additional dataset locations.
In a container, mount extra destinations as writable volumes and pass their
container paths to `--output-location`; a host path is not visible until mounted.

Users get their files on their own computer: the page writes the verified
dataset into a folder they pick (Chrome/Edge) or downloads a ZIP. Through an SSH
tunnel, that folder is on the user's computer, not the server. The server never
receives or opens client folder paths. The app remains one private workspace, not an authenticated
multi-tenant storage service.

## Minimal container

```sh
docker build --pull -t synthocr:local .
docker volume create synthocr-data
docker run --rm --name synthocr --network host \
  --memory 512m --cpus 1 --pids-limit 128 \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --mount type=volume,source=synthocr-data,target=/data \
  --log-opt max-size=5m --log-opt max-file=2 \
  synthocr:local
```

Open <http://127.0.0.1:8000>. Host networking preserves loopback binding. Docker's
[host networking documentation](https://docs.docker.com/engine/network/drivers/host/)
lists Linux support; Docker Desktop 4.34 or later requires its **Enable host networking**
setting. On macOS or Windows, the native `uv run` command is simpler if that
setting is unavailable. Do not replace host networking with a public port mapping
or change the app's bind address to make it reachable from the internet.

The memory limit is a starting budget, not a measured peak or guarantee. Large
uploads, additional fonts and output formats can require more. `/tmp` is an
ephemeral memory-backed filesystem and counts toward the container's memory
limit. Increase the budget or mount a private disk-backed temporary directory
when preparing a large corpus. Output storage is separate and must have its own
capacity limit or volume quota; Docker's memory limit does not limit disk use.

The container runs as UID/GID `10001`. A Docker-managed volume gets the prepared
ownership on first use. For an existing bind-mounted output directory, grant that
user write access before starting. Generated datasets, job metadata and processed
indexes persist under `/data`; the image itself stays read-only.

Processed text stays in SQLite; the optional duplicate processed JSONL is disabled.
The download/export cache is limited to 256 MiB with `--cache-mb 256`. That is a
disk budget, not a RAM allocation. Generated datasets and processed indexes remain
separate from that cache and are retained until deliberately removed.

The build uses digest-pinned Python 3.14.7 and uv 0.13.5, resolves the `web` extra
from `uv.lock`, installs the project as a wheel, and keeps uv and build tools out
of the runtime. Only Noto Naskh Arabic and Noto Sans, with their OFL licenses, are
included. Their existing catalog SHA-256 values and Sorani/Kurmanji coverage are
checked during the build. The build also refuses Pillow without RAQM. Refresh
image digests and the lockfile deliberately when applying upstream security
updates.

The allowlist in `.dockerignore` excludes raw corpora, processed indexes, generated
images, Git history, local environments, tests, caches and other font families from
the build context. No corpus downloads happen at server startup. Uploaded files and
the bundled spelling check work without a corpus mount. The small image includes
two font faces; request 176 images for the complete Sorani spelling check, 98 for
Kurmanji or 274 for both, rather than assuming the full font collection's capacity.
To use corpus mode, mount
the maintained corpus directory read-only at `/corpora`:

```sh
--mount type=bind,source=/absolute/path/to/corpora,target=/corpora,readonly
```

Supply language-tagged JSONL corpora, or mount a reviewed config that declares
`data.source_languages` for unlabelled sources. Do not infer a language from its
script. Preparation produces one active corpus entry per canonical language in
`/data/processed/corpora.json`, backed by the SQLite index. Source snapshots and
their attribution remain intact; language aliases share a corpus identity while
explicit dialect and script tags stay separate. Additional licensed fonts can be
mounted read-only at `/extra-fonts`.
For this repository's older Tatoeba snapshots, also mount the pinned source catalog
read-only at `/corpora.json` (the parent of `/corpora`). This preserves precise
Mandarin/Iranian Persian/Swahili identities from their verified catalog metadata;
new snapshots carry their original language codes directly in attribution.
Use `data.source_languages` in a reviewed mounted config for unlabelled TXT
sources; the generic container intentionally has no filename guesses.
Installing more corpora does not install the fonts needed to render their scripts;
font checking remains per label and generation reports unsupported text.

On a remote Linux host, start the same loopback service there and open a tunnel
from your own computer:

```sh
ssh -N -L 8000:127.0.0.1:8000 your-user@your-server
```

Then open <http://127.0.0.1:8000> locally. Port 8000 must be free on each machine.
Keep container/runtime updates and storage backups under the host's normal
maintenance policy.

## Why keep this design small?

[FastAPI's deployment guide](https://fastapi.tiangolo.com/deployment/docker/)
describes one process per container as a practical way to control memory.
[uv's Docker guide](https://docs.astral.sh/uv/guides/integration/docker/) supports
non-editable installs copied from a separate build stage, and
[Docker's guidance](https://docs.docker.com/build/building/best-practices/) recommends
small trusted bases, multi-stage builds and an explicit build context.
[Pillow's Linux installation notes](https://pillow.readthedocs.io/en/stable/installation/basic-installation.html)
explain the separate FriBiDi dependency for RAQM.

[TRDG](https://github.com/Belval/TextRecognitionDataGenerator) offers dictionary,
string and Wikipedia generators with a broad augmentation CLI. Its
[dependency manifest](https://github.com/Belval/TextRecognitionDataGenerator/blob/master/setup.py)
includes OpenCV and separate Arabic reshaping/bidi packages. SynthOCR keeps
Pillow/RAQM shaping in one implementation and uses explicit, attributed corpus
snapshots; this supports a smaller runtime without downloading live text during
generation.

[SynthTIGER](https://github.com/clovaai/synthtiger) supports flexible templates,
nonlinear geometry, glyph masks and character coordinates, with a broader
[image/scientific dependency set](https://github.com/clovaai/synthtiger/blob/master/setup.py).
[SynthDoG](https://github.com/clovaai/donut/tree/master/synthdog) builds full synthetic
documents on that engine. SynthOCR focuses on text-image labels and verified,
model-neutral exports, so those heavier layout dependencies stay optional future
work. These are capability and packaging comparisons, not measured claims that
one engine is faster or produces better training data.

Use `synthocr benchmark` with the fonts and corpus you actually deploy, and record
container peak memory as well as the command's parent RSS. Measurements from
different machines, worker counts or augmentation settings are not directly
comparable.

Local validation with the full font collection generated and verified at least
one fully augmented image for each of the 35 named language/dialect corpora,
and all 274 Kurdish spelling cases with two workers. A 1,000-image Sorani
benchmark with one worker and the app's eight-font cache recorded 81.6 MiB
peak parent RSS. This is a native macOS measurement, not a container budget or
browser-memory measurement. The slim image's two licensed font families total
about 2.3 MiB, instead of embedding the full 65 MiB font collection.

Validation on 2026-10-25 checked this configuration and the two bundled fonts,
then generated and verified all 274 Kurdish spelling cases locally. The Docker
daemon was unavailable in that environment, so the image build, container peak
memory and final image size remain unmeasured. Run the build above and inspect
`docker image inspect synthocr:local` and `docker stats synthocr` on the target
host before assigning a production budget. The Linux container CI job builds the
image, starts the read-only non-root app with the budget above, generates all
274 spelling cases through the API, and independently verifies the saved data.
