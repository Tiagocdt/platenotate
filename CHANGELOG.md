# Changelog

All notable changes to PlateNotate. Versions are `MAJOR.MINOR.PATCH`; the number in
`VERSION` is what the app reports, and `run.sh` fast-forwards a git checkout to the
newest commit on launch.

## [1.6.1] — 2026-08-11

### Fixed — on Windows the app could not tell whether it was allowed to write

Reported as "the TIF export doesn't work, and possibly the MP4 — I think there's a write
block, and maybe that's why the annotations aren't going into a database". Both, and the
same root cause: **every check for "can I write here?" and "is this a share?" gave the
wrong answer on Windows.**

- **`os.access(folder, os.W_OK)` reads the POSIX permission bits, and Windows does not use
  them.** There it reports the read-only *attribute* and ignores the ACL actually in
  force, so a folder the user genuinely cannot write to answered **"writable"**. The app
  then aimed the database at it, and the failure surfaced much later and somewhere else.
  Writability is now decided by **writing a probe file**, which cannot be wrong on any
  operating system.
- **`_is_network_fs` shelled out to `mount`, which Windows does not have.** It raised,
  was caught, and returned `False` for *everything* — so every Windows user on a mapped
  drive or a UNC path was told their share was local disk, and the database was created
  **on the share**, where SQLite's write-ahead log is exactly what you must not use. It
  now asks Windows: a UNC path is a share, and a mapped drive is checked with
  `GetDriveTypeW`.
- **Exports were written into the plate folder with no check at all.** On a read-only
  share the job ran to completion and produced nothing. The destination is now **probed
  before the work starts**; if it is refused, the files go to
  `~/Documents/PlateNotate exports/` and **the job says so** — silently writing somewhere
  else is how "the export did nothing" happens, because the folder you go and look in is
  empty.

### You are now told when your annotations are not with your images

`local_fallback` reported *"was the source a network share?"* — a different question,
which answered **False** for the case that actually bites: a folder that is not a share
but still refuses writes. The database quietly lived somewhere else and nothing on screen
said so. It now reports where the file **is**, so the banner appears whenever annotations
end up on this computer rather than beside the plates.

### A failed export leaves evidence

A packaged app has no console, so the traceback went nowhere — which is how "the export
doesn't work" arrives with nothing attached. Failures are now appended to
`~/.medaka_annotator/platenotate-export-errors.log`, and the on-screen message names the
folder and the likely cause instead of a bare `[Errno 13]`.

### Fixed — a test could rewrite your own settings

`db_location_test.py` wrote to the real `~/.medaka_annotator/settings.json`, including
`annotations_dir`. That is one bad line away from the "where did my database go" scare the
file exists to prevent. It now runs against a scratch home, and asserts that isolation
before a single test runs.

## [1.6.0] — 2026-08-07

### A share that stops answering can no longer freeze the app

Reported as "at some point it hangs and never comes back — it stops loading images".

Every frame is read from wherever the plates live, usually an SMB share. When a share
stalls, macOS blocks the read in an **uninterruptible syscall** — no timeout, no way to
cancel it. A browser opens about six connections to one host, so six stuck reads were
enough for the whole interface to stop loading images and never recover, while the process
sat there looking perfectly healthy and the traceback-free silence gave nothing to go on.

A stuck syscall still cannot be unblocked. What it no longer does is consume the app:

- Reads that touch the image store now run on a **bounded pool with a 10 s timeout** (~360×
  the 27.7 ms median measured against a healthy share). Past that the request answers
  **503 instead of hanging**, so the browser gets its connection back.
- Once every slot is held by a stuck read, further requests are **refused in under a
  second** rather than queueing behind them.
- Frames already decoded in memory are served **without touching the store at all**, so a
  stalled share cannot slow down what the app is already holding.
- Prefetch **stops** when the store is in trouble instead of piling more work onto it.
- It **self-heals**: when the mount answers again the stuck reads finish, the slots free
  themselves, and service resumes with nothing to restart.
- A frame that fails now **says why on screen** — "the folder your plates live in stopped
  answering" — instead of a silent broken-image icon that looks identical to a freeze.

Measured end to end: with 20 concurrent frame requests against a store that never answers,
all 20 returned 503 and none hung, while `/api/version` answered in 104 ms and the page in
1 ms. Before, those requests held their threads indefinitely.

### The disk cache no longer collapses as the disk fills

The automatic limit was *a tenth of the free space*, which has two bad properties: the
cache's own files count as "used", so the budget shrank as the cache grew — it chased its
own tail — and it shrank fastest exactly when a full disk makes re-reading a share most
painful. Measured on a 96 %-full disk it had fallen to **4.10 GB holding 74,820 frames —
about twenty wells, less than a single plate**. Opening a second plate evicted the first,
so going back re-read every frame from the network at ~28 ms apiece. That is the
"it gets slow again" half of the same report.

The limit is now a share of the disk's **total** size, which does not move, capped at
20 GB and always leaving **10 GB free** whatever the arithmetic says. On the machine above
that is 18.5 GB instead of 4.1 GB. On a genuinely full disk it still shrinks to nothing,
because filling someone's disk is not an acceptable way to be fast.

### You can now see whether the cache is doing anything

`/api/cache` and **Settings → Image cache** report the **hit rate**, evictions, and stalls,
and warn outright when the cache is full and still missing most frames — the state where
it is costing you disk space and buying nothing. Previously the only symptom was the app
feeling slow again, with no way to tell that from a slow share.

Also: the cache settings are no longer re-read and re-parsed from disk twice per frame.

## [1.5.3] — 2026-08-07

### The Windows test could not start the app it was testing

The v1.5.2 Windows build got through the build, the selftest and the launch test — so the
1.5.2 fix works — and then the new GUI probe failed before running anything:

```
An error occurred trying to start process '…\PlateNotate.exe'.
The operation was canceled by the user.
```

Nobody cancelled anything. **Windows refuses to launch a marked `.exe` at all** — that
message is SmartScreen's "Windows protected your PC", with no one on a headless runner to
click "Run anyway". The test had marked *every* file including the launcher, which is
faithful to what Explorer does but not to the state the app actually runs in: a real person
clicks through that dialog once, and the app then starts **with its `_internal` DLLs still
marked**. That is the state that broke it.

The probe now unblocks the launcher only, and asserts that `Python.Runtime.dll` is *still*
marked before running — so it fails loudly rather than quietly testing nothing.

Nothing in the app changed. Worth knowing as a user, though: the first launch of any
unsigned download shows SmartScreen and needs **More info → Run anyway** — and because
PlateNotate clears the mark from its own launcher on that first run, you should not see it
again.

## [1.5.2] — 2026-08-06

### Fixed — with no console, the Windows app served nothing at all

Caught by the v1.5.1 build itself, on Windows:

```
FAIL /: RemoteDisconnected: Remote end closed connection without response
FAIL /static/app.js: RemoteDisconnected …          (all six, instantly)
```

A windowed build has **no console**: PyInstaller leaves `sys.stdout` and `sys.stderr` as
`None`. `print` quietly tolerates that — `sys.stderr.write(...)` does not, it is an
`AttributeError`. The HTTP request logger wrote to stderr directly, and `send_response`
**logs before it sends a single byte**, so every request in the app died before answering
and the browser saw only a closed connection. The traceback went to the same missing
stream that caused it, so there was nothing to read either.

- `make_console_safe()` now **stands in for a missing stream** instead of leaving `None`
  for the next `.write` to trip over. That closes the whole class of it, wherever the next
  message gets written.
- The request logger cannot raise regardless — it also runs before `self.path` exists when
  a request line is malformed.
- **`console_test.py` now makes a real request over a real socket with both streams set to
  `None`** and requires a 200. Every other check in that file passes with stderr missing,
  which is exactly how this reached a release: the same lesson as the cp1252 banner, one
  layer further in, and the tests only knew about the layer above.

## [1.5.1] — 2026-08-06

### Windows without the WebView2 runtime gets the browser, not Internet Explorer

When the Edge WebView2 runtime is missing, pywebview does not fail — it quietly settles
for **MSHTML, the Internet Explorer 11 engine**, and opens the window anyway. PlateNotate's
interface is modern JavaScript, so that window would come up on a broken page: a worse
outcome than not opening at all, and a far more confusing one to report. The engine is now
checked before the window is created, and MSHTML sends the app to your default browser
instead, with the reason on screen.

The Windows CI probe also verifies its own premise now: it reads the `Zone.Identifier`
stream back after writing it, so a silently-ignored mark cannot let the probe look like it
survived a condition it never met.

## [1.5.0] — 2026-08-06

### Fixed — the Windows app still would not start

```
Failed to execute script 'desktop' due to unhandled exception:
RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
C:\Users\…\Downloads\PlateNotate-Windows\_internal\pythonnet\runtime\Python.Runtime.dll
```

**Windows had marked our own files as untrusted, and .NET obeyed.** Every file
extracted from a downloaded `.zip` carries a hidden `Zone.Identifier` stream — the "Mark
of the Web" — and the .NET Framework flatly refuses to load an assembly that has one.
PlateNotate's window is a .NET window (pywebview → pythonnet → `Python.Runtime.dll`), so
the app died before drawing a pixel. Unzipping into `Downloads` was enough to cause it;
right-click → Properties → **Unblock** on the zip *before* extracting would have avoided
it, which nobody knows and nobody should have to.

- **The app now unblocks itself**: on launch it deletes that mark from its own files, in
  its own folder, and nowhere else. That is the same operation as the Unblock checkbox.
- If the files cannot be rewritten (read-only media, a network share, a policy that
  re-marks them), it retries .NET in a private AppDomain that is allowed to load
  "remote" assemblies, and then falls through to .NET Core.
- **pywebview's own fallback for this could never work**, which is why the traceback
  showed the same error twice. It sets `PYTHONNET_RUNTIME=coreclr` and re-imports `clr`,
  but `pythonnet.load()` short-circuits on the runtime object it already cached, so the
  retry re-raises the *first* runtime's error and the environment variable is never
  read. Each retry here installs a new runtime instead of re-asking the old one.

### The app no longer depends on being able to open a window

If the native window cannot be opened for **any** reason, PlateNotate now opens in your
default browser and keeps running, with a dialog that says so and quits when you close
it. It is a local web app; a browser is a perfectly good frame for it, and a running app
beats a crash dialog. This also gives Linux a working app on machines where the GTK
bindings are missing from the bundle. (In browser mode the **Browse…** button is not
available — type or paste the folder path in **📂 Open** instead.)

`PLATENOTATE_BROWSER=1` forces this mode.

### Errors you can act on, instead of "Failed to execute script"

Anything the app cannot recover from is now written to
`~/.medaka_annotator/platenotate-crash.log` and shown in a real dialog with the error in
it. PyInstaller's own crash box shows a traceback nobody can copy, and on a machine with
no console — which is every packaged build — it was the *entire* user-facing error
message for three releases.

### CI now loads the GUI toolkit, on a bundle marked as downloaded

`--selftest` exits before the window exists and the launch test stops one call short of
it, so **nothing in CI had ever loaded the toolkit behind the window** — the thing that
broke, twice. There is now a `--gui-probe` mode that imports the platform backend
without opening a window, and on Windows CI stamps `Zone.Identifier` onto every `.dll`,
`.pyd` and `.exe` in the frozen bundle first, reproducing a downloaded-and-unzipped copy
before running it. A headless runner cannot check that a window *renders*; it can check
that the toolkit will load, which is the failure that actually shipped.

The window stack (`pywebview`, `pythonnet`, `clr-loader`) is **pinned** now. Every
Windows failure this project has had came from there, and an unpinned build cannot be
reproduced after the fact.

## [1.4.1] — 2026-07-30

### The cache is DISK, and it no longer assumes your disk is big

"20 GB cache" meant 20 GB of disk in `~/.medaka_annotator/framecache/`, never memory —
but a flat 20 GB is fine on a workstation and rude on a colleague's laptop, and this app
is meant to be handed to colleagues. The disk cap now **adapts to the machine**: a tenth
of the free space, at most 20 GB, and you can still set an explicit number. Settings
shows what is held, the limit, and how much disk is free.

### Fixed — the in-memory cache was bounded by frame count, not memory

There is a second, small cache in RAM that saves a re-decode. It was capped at *1500
frames*, which is not a memory budget at all: 1500 frames is ~75 MB of 600 px JPEGs but
**~600 MB of 1024 px PNGs**. It is now bounded by **bytes** (192 MB), evicting by size,
and reported separately from the disk cache so the two are never confused again.

## [1.4.0] — 2026-07-29

### Fixed — the Windows app would not start at all

```
Failed to execute script 'desktop' due to unhandled exception:
'charmap' codec can't encode character '\u2192' in position 30
```

The launch banner printed an arrow. A Windows console is cp1252, which cannot encode
one, so `print` raised — and in a windowed build an unhandled exception is a dialog and
no app. Reproduced locally on a cp1252 stream (same character, same position 30).

- `make_console_safe()` switches the streams to UTF-8 (falling back to
  `errors="replace"`), so no message can ever stop the app again.
- The launch banner is plain ASCII, because it runs before anything has proven the
  console is writable.
- **CI now runs the GUI launch path** (`PLATENOTATE_NO_GUI=1`), on Windows under
  `chcp 1252`. `--selftest` returns long before the window is created, so it never
  touched the code that broke — which is why a green build shipped a dead app.

### The cache holds whole wells now instead of evicting them

Two problems on top of the wrong-frames bug fixed in 1.3.0:

- **A 600 px display PNG is ~183 KB — three quarters of the 243 KB source TIF.** The
  cache barely compressed anything, so a 4 GB cap held ~32 wells of a single slice and
  ran permanently full: opening a well evicted the one before it, and going back re-read
  the whole trajectory from the share. Frames are now JPEG (~51 KB measured, 3.6×
  smaller). This is a *display* cache — annotations and measurements are stored in image
  coordinates, which the encoding does not touch — and `lossless` in Settings restores
  PNG.
- **The cap is now 20 GB and configurable**, with the cache's real usage shown in
  Settings. Cached replay measured at 12 ms/frame end-to-end.

### Prefetch is tiered to what you are actually doing

- **Browsing / playing a trajectory** warms ONE plane per timepoint — the slice that
  frame is displayed at — which is ~1/nz of the reads. Whole well, ordered outward from
  the frame you are on.
- **Focus work** warms the z-stack around where you are, requested when you reach for
  the z fader or arm its record button, and after dwelling ~20 s on one well. Not up
  front for every well you glance at.
- Both share the one bounded pool and generation check from 1.3.0, so escalating cannot
  pile up on the pass before it.

Sources stay as individual crops: the DINO data loader globs `<slice>/*.tif` directly,
and `build_db`, `gen_frame_metadata`, the segmentation tools, egg-motion and the
embedding explorer all address crops by path. Packing wells into single TIFs would break
that for a first-visit gain the tiering already recovers.

## [1.3.0] — 2026-07-27

### The database belongs with the images

Opening a folder of plates now uses — or creates — the database **in that folder**.

`annotations_dir` (the folder you last saved into) used to win over everything, so it
followed you: open a fresh plate folder and you were still reading, and writing, the
previous database. It is now the fallback for a folder you *cannot write to* (a
read-only share), not an override of where the images live. A registry link, being an
explicit per-folder choice, still wins.

**A new folder now gets a genuinely new, empty database** — no inherited columns.

### Changing the annotations folder no longer copies your database into it

It used to copy silently, so that choosing a folder could never hide your existing
annotations. The cure was worse than the disease: pointing the app at a colleague's
network share wrote **a 355 MB copy of every plate** into their folder, with no prompt
and no mention. Copying is now an explicit request (`copy_db`); otherwise the client is
simply told whether the new folder already has a database or will get a fresh one.

### The frame cache was warming the wrong frames

Playback stalled on a slow share not because there was no cache, but because the cache
warmed the **middle** z-slice while the viewer asks for the slice your `slice` keyframes
forward-fill to. On any well with focus annotations the hit rate was **zero** — every
frame was re-read from the share.

- Prefetch now resolves the same z per timepoint the viewer will request.
- It warms **outward from the frame you are on**, so playback runs ahead of the reads
  instead of racing them.
- **One bounded, shared pool** (12 workers) replaces a new 20-worker pool per well
  selection with no way to stop it — clicking through a plate used to stack hundreds of
  concurrent reads onto the very share that was already the bottleneck, which is how a
  slow mount took the whole app down. Work queued for a well you have navigated away
  from is now dropped instead of run.
- A share that disappears mid-read is survivable: the prefetcher swallows the I/O error
  rather than propagating it.

## [1.2.4] — 2026-07-27

### Windows: teardown can no longer hang the build

With the log now written line by line, the Windows failure was finally legible: **every
check passed** — all six URLs, all nine imports, ffmpeg resolved to the bundled binary —
and the process then hung in *teardown*, after the last check and before the verdict.

The scratch folder still held the open SQLite file, so cleaning it up raised; and in a
**windowed** Windows build an unhandled exception opens a PyInstaller crash dialog,
which on a headless runner waits forever for a click nobody can give. Teardown is now
incapable of raising: `mkdtemp` + `rmtree(ignore_errors=True)` instead of
`TemporaryDirectory`, the database connection is closed first, each teardown step is
individually guarded, and the whole selftest body is wrapped so no exception can ever
reach a dialog. HTTP responses are read inside a `with` so no handler thread lingers.

### The release no longer depends on every platform succeeding

`release` now runs with `always()`: a tag publishes whatever **did** build, and warns in
the log about any platform that didn't. One platform failing must not withhold the
working builds from everyone else — a missing asset is visible in the release, while no
release at all just looks like the project is dead.

## [1.2.3] — 2026-07-27

### Windows: find the bundled ffmpeg by path, and log the selftest as it happens

The Windows smoke test stalled past a 4-minute bound with an empty report, so it was
impossible to say which check hung. Two changes:

- **The selftest log is written incrementally**, one line at a time, flushed. A hung
  check now leaves a log ending on the exact step that died — a report assembled only at
  the end is empty, which is how the first Windows failure hid itself twice.
- **`ffmpeg_exe()` looks inside the frozen bundle first**, by plain file lookup, before
  calling `imageio_ffmpeg.get_ffmpeg_exe()`. Inside a packaged app the binary sits right
  next to us, while the library's own lookup is free to go searching the system — which
  on a headless machine is a stall rather than an error.

## [1.2.2] — 2026-07-27

### Fixed — the Windows smoke test hung instead of failing

With `Start-Process -Wait` the windowed `.exe` never returned, stalling the job. A
headless runner has nobody to dismiss a PyInstaller crash dialog, and a GUI-subsystem
process gives PowerShell nothing to wait on. The step is now **bounded** (`WaitForExit`
with a timeout, kill on hang, `timeout-minutes` as a backstop) and judges the run by the
`selftest: PASS` line in the log the app writes itself — so a hang fails fast and says
so, instead of burning the job.

`--selftest` also now closes the server socket and exits through `os._exit`, so no
lingering thread or GUI runtime can keep the process alive after the check is done.

## [1.2.1] — 2026-07-27

### Fixed — the Windows build's smoke test could not report anything

The v1.2.0 tag built cleanly on all three platforms, but the Windows smoke test exited
non-zero with an empty report, so the release job never ran. Two causes, both about
*seeing* the failure rather than the app itself (Windows served all six URLs fine):

- A windowed Windows build has `sys.stdout is None`, and CPython's `print` **silently
  discards** output in that case — so the entire selftest report vanished.
  It now writes to stderr *and* to `platenotate-selftest.log`, which CI prints.
- A GUI-subsystem `.exe` does not block PowerShell, so `$LASTEXITCODE` was not the
  app's exit code. CI now uses `Start-Process -Wait -PassThru` and reads `.ExitCode`.

The selftest also got stricter while it was being fixed: it now imports every module
the app loads lazily (`export`, `compose`, `well_hyperstack`, `focus_cut`,
`annotations`, `build_db`, `imagecodecs`, `tifffile`, `imageio_ffmpeg`), reports a full
traceback for any that fail, and checks the bundled ffmpeg binary is really there —
which is exactly the class of breakage that only appears inside a frozen bundle.

## [1.2.0] — 2026-07-27

### A real app you download and double-click

Until now the repo only contained *instructions* for building a desktop app, and CI only
uploaded Actions artifacts — which expire after 90 days and need a GitHub login, so they
were useless as a download link. Anyone wanting to use PlateNotate still had to install
Python, `pip install pywebview`, and run a command. That is fixed:

- **Every version tag now publishes a GitHub Release** with `PlateNotate-macOS.zip`,
  `PlateNotate-Windows.zip` and `PlateNotate-Linux.tar.gz` attached. Download, unzip,
  double-click. Python, Pillow/numpy/tifffile/imagecodecs and ffmpeg are all inside.
- **`--selftest`** boots the app headless, fetches the pages a browser needs and imports
  the export engine. CI runs it against the **frozen** bundle on all three platforms, so
  a build that compiles but dies on launch can never be released. It is also the fastest
  way to check a local build: `python server.py --selftest`.
- **The packaged app no longer guesses a data root inside its own bundle.** It opens the
  folder you last used (`last_data_root`), else your home folder, and "📂 Open" takes it
  from there.
- **Check for updates works without git.** A packaged app has no checkout to compare
  against, so the check asks the GitHub Releases API and offers a download link;
  a source checkout still fast-forwards itself as before. Only on an explicit click —
  no background polling.
- Linux builds now install the GTK/WebKit packages pywebview needs.

### Logo

The PlateNotate mark is now the browser-tab favicon, the mark in the app's top bar, the
macOS/Windows app icon (`assets/icon.icns` / `icon.ico`, generated from `assets/logo.png`)
and the README header.

### Fixed

- `_serve(host, 0)` reported port `0` instead of the port the socket actually got, so
  anything asking it for a URL — including the new selftest — built an unreachable
  address. It now reads the bound port from the socket.
- Static handler serves `.png` / `.svg` / `.ico` with the right content type (the
  favicon was being sent as `application/octet-stream`), and caches images for a day
  while keeping the code uncached.

## [1.1.2] — 2026-07-25

### Fixed — `well_hyperstack.py`'s command line was broken

Both builders were called with POSITIONAL arguments, but each has optional parameters
(`channels`/`slices`, the timepoint window, and now `z_mode`/`rotate`) sitting between
`gap` and the data roots — so `--data-root` landed in `tp_start` and any CLI run died
with `'>=' not supported between instances of 'int' and 'str'`. Now called by keyword.
The app was never affected; it has always used keyword arguments.

Also exposed on the CLI: `--z-mode all|maxproj|focus|slice` and `--rotate`.

## [1.1.1] — 2026-07-25

### Fixed — dated plate folders found no annotations in the database

Rows are keyed by the **canonical** plate id (the folder name with any leading
`YYYYMMDD_` stripped), but the renderers are handed the folder name. For a dated
folder such as `20260627_AQV07_…` every database lookup therefore missed:

- keyframes and well annotations quietly came from the screening JSON instead of the
  database — the same values today, but not the source of truth, and stale the moment
  the two diverge;
- `plate_meta` and `pixel_size_um` have no JSON fallback, so montages of a dated plate
  had **no elapsed-time label and no scale bar at all**, with nothing to say why.

`annotations.plate_keys()` now tries both ids in every accessor (`image_keyframes`,
`measurements`, `well_annotations`, `plate_meta`, `pixel_size_um`). AQV07 montages now
carry `t5 · 0h40` and a 1 mm bar. Undated folders are unaffected — one lookup, as before.

## [1.1.0] — 2026-07-25

The first released version. Everything below was built in one session on top of the
1.0.0 baseline, so it ships as a single commit; from here on each version gets its own
commit and tag.

### Render options for movies and montages

Every option maps to something already saved in the app.

- **Plane per channel** — max projection, the **annotated focus track**, one z-slice, or
  the middle slice. Max projection now works for *every* channel, brightfield included,
  instead of only whatever `_SLMX` file happened to exist on disk.
- **Rotation is applied.** `rotation` keyframes are interpolated with the same rule as
  the viewer (smoothstep, shortest way round the circle) and baked into every frame.
  **This never worked before**: the "use my annotations (focus + rotation)" checkbox
  read only the focus column, so rotation was silently dropped from every export.
- **No hard failure on missing annotations.** A well with rotation keyframes but no
  focus keyframes used to abort the whole job with *"no output produced"*. Each track
  now degrades independently (focus → modal best-focus slice → middle slice;
  rotation → 0°) and the job reports what it actually used, per well, in the job dock.
- **Channel overlay** — composite the selected channels into one colour movie (screen
  blend) instead of one movie per channel.
- **Colour per channel** — gray, inverted, a tint (green/magenta/cyan/red/blue/yellow/
  orange/amber/violet/ice/sepia) or any matplotlib colormap.
- **Labels on every tile** — well, plate, developmental stage (forward-filled from the
  keyframes), timepoint and elapsed time (from the plate's cadence), angle, z, and any
  well-level annotation column; plus a **scale bar** in real µm, sized from the plate's
  own measurements. Corner, size and colour are configurable.
- **TIF hyperstacks** gained `z_mode` (`all` / `maxproj` / `focus` / `slice`) and
  rotation. Labels are deliberately *not* offered for TIF — it's quantitative data.

### Filter across plates — subsets, measurements, saved filters

- **Plate subset.** The single-plate dropdown is now a chip picker: search any
  combination of plates (no chips selected = all of them), with each plate's well count
  on its chip.
- **Measurement constraints.** Filter on a measurement (e.g. `egg_diameter`) with
  `> ≥ < ≤ between =` and a reduction over the timepoints it was measured at:
  **at every measured timepoint** (the "…always" case), at any timepoint, or the
  mean / smallest / largest / first / last.
  - A well measured at **one** timepoint satisfies "at every measured timepoint" — one
    timepoint is still every timepoint that exists. Most sizes are annotated once, and
    they must not be silently dropped.
  - `min n` demands a minimum number of measured timepoints when you *do* want more.
  - A well that was never measured never matches a measurement constraint.
- **Annotation operators.** Beyond `=`: `≠`, *is set*, *is unset*, and numeric
  `> ≥ < ≤ between` for columns holding numbers.
- **Saved filters.** Name a plate set + constraints and reload or delete it later
  (stored in `~/.medaka_annotator/settings.json`).
- Wells carrying measurements but no annotations are now filterable too.

### Version & self-update

- `VERSION` + `version.py`; the version shows in the top bar (amber when an update is
  waiting) and in Settings.
- `run.sh` fast-forwards the checkout on launch — skipped when there are local changes,
  `NO_UPDATE=1` opts out. Settings has *Check for updates* / *Update now*.
- `GET /api/version`, `POST /api/update`.

### Engine

- New `compose.py`: the annotation-aware frame composer (tracks, plane selection, tint,
  overlay, labels, scale bar, tiling).
- New shared accessors `well_annotations()`, `plate_meta()`, `pixel_size_um()` — µm/px
  derived from your own measurements, falling back to the acquisition metadata.
- Fixed: a stale vendored copy under `packaging/_deps/` could shadow the live engine.

### Repo

- Runs from a **standalone clone**: `db_store` and `export` fall back to the vendored
  modules in `packaging/_deps/`, so `import build_db` / `import compose` no longer need
  the author's full imaging tree.
- Public README, MIT `LICENSE`, `docs/` (desktop packaging, image-tool plugin API,
  design history), tightened `.gitignore` (no databases or annotations).
- `tests/compose_test.py` (33 assertions); the JS harness grew from 76 to 102.

## [1.0.0] — 2026-07-23

Baseline: the three-scope annotator (plate / well / image) with keyframed image columns,
the rotation and measurement image-tool plugins, the SQLite store, the cross-plate
filter, background TIF/MP4 export with a job dock, Settings, the desktop window
(`desktop.py`) and the PyInstaller packaging recipe.
