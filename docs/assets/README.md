# Screenshots and demo media

This folder holds the images used by the [README](../../README.md). `logo.png` is the only file here at the moment.
The screenshots and the demo GIF still have to be captured from the real application. Each README already contains a
commented-out `<img>` block for each one (search for `SLOT`), so adding a file is two steps:

1. Save the capture here under the exact name in the table below.
2. In both `README.md` (Turkish, the one GitHub shows) and `README.en.md` (English), delete the `<!--` and `-->` lines around that slot's `<img>`.

Neither README points at a file that does not exist yet.

## Shot list

| File | Where it goes | What to show | Size |
|---|---|---|---|
| `hero.png` | Hero | CIRANA's Home page next to a few tiled application windows, on a clean desktop | 1600 px wide |
| `dashboard.png` | Features → Launch | Home, with two or three workspace cards and the category tiles | 1600 px wide |
| `adaptive-layout.png` | Features → Adaptive tiling | Windows page with *Tile all windows* on and *Automatic (adaptive)* selected, three or four windows tiled behind it | 1600 px wide |
| `side-by-side.png` | Features → Tiling | Four or more different applications tiled on one monitor, focus border visible on one of them | 1600 px wide |
| `workspaces.png` | Features → Workspaces | Workspaces page: one workspace selected, its rows, the layout picker and the live preview | 1600 px wide |
| `monocle.png` | Features → BSP, stacks and monocle | A monocle window filling the work area (thick border), or a stack with its tabbed border | 1600 px wide |
| `settings.png` | Customization | Settings page: *Keyboard and focus* and *Animations* cards | 1600 px wide |
| `demo.gif` | See it in action | A window opens and tiles, two windows swap, an animated re-tile, monocle on and off | 1280 px wide, 12–15 fps, under 5 MB |

Use real captures only. Do not mock up or compose the interface.

## Taking clean captures

**Use a separate data folder** so your own settings, workspaces and application list are not touched and do not show up:

```powershell
$env:CIRANA_HOME = "$env:TEMP\cirana-demo"
python -m cirana
```

An empty `CIRANA_HOME` is a first run, which is also what a new user sees: all-window tiling is on and the layout is
*Automatic (adaptive)*. Leave *Focus follows the mouse* off, because it changes a Windows setting while it is on.

**Prepare the desktop.**

- Close CIRANA if it is already running, and any other tiling tool. Two instances arrange the same windows against each other.
- Tiling manages every application window on the current virtual desktop. Start a new virtual desktop (`Win+Ctrl+D`) so
  only the demo windows exist, or close and minimize everything else.
- Pick applications that do not restore personal state: File Explorer on an empty folder, Calculator, Paint, a new
  Windows Terminal window. Browsers and chat applications restore tabs, accounts and conversations.
- Check window titles, the taskbar and notifications for anything personal before capturing. Windows page rows show every window title.
- 1920×1080 at 100 % scale is a good baseline. Cover the work area only, without the taskbar.

**Screenshots.** Capture the CIRANA window with `Alt+PrintScreen` or the Snipping Tool, and tiled layouts with
`Win+Shift+S` (rectangle over the work area). Save as PNG, then scale to about 1600 px wide and shrink the file:

```powershell
ffmpeg -i capture.png -vf scale=1600:-1:flags=lanczos hero.png
```

**Demo GIF.** Record the work area with ffmpeg's `gdigrab` (adjust the size to your monitor's work area), then convert
the recording with a generated palette:

```powershell
ffmpeg -f gdigrab -framerate 30 -offset_x 0 -offset_y 0 -video_size 1920x1040 -i desktop -t 12 raw.mp4
ffmpeg -i raw.mp4 -vf "fps=15,scale=1280:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5" demo.gif
```

Keep it to a few seconds and under 5 MB. Suggested script, using the default shortcuts from [shortcuts.md](../shortcuts.md):

1. Open three applications one after another and let them tile themselves (animated).
2. Swap two tiles with `Ctrl+Alt+Shift+Left` / `Right`.
3. Toggle monocle with `Ctrl+Alt+Enter`, then back.

A `.mp4` placed in the repository does not play inline in a README, so use a GIF there. A video can be linked instead.
