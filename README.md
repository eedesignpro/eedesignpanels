# eedesignpanels

eedesignpanels builds a fabrication panel from a KiCad 10 board. This
repository holds two parts:

- **The engine**, the Python package `eedesignpanels` at the top of this
  repository. It reads a board file and a settings file, and writes the panel.
  It runs headless from a command line. It also serves a browser page in
  which you set up, check, and save a panel.
- **The KiCad plugin**, in the folder `kicad_plugin/`. It adds a toolbar
  button to the KiCad PCB Editor. The button opens the engine on the board
  that is open.

The panel holds the step-and-repeat copies of the board, the rails, the
router tool path, the cut and v-score lines, the tooling holes, the
fiducials, the rail copper, the panel text, and the panel dimensions. The
tooling holes, panel v-scores, rail copper, fiducials, dimensions, and panel
text each have their own `enabled` setting.

The engine reads and writes every board through
[`eedesigncore`](https://github.com/eedesignpro/eedesigncore).

| Document | Content |
|---|---|
| [docs/how-it-works.md](docs/how-it-works.md) | the two steps, the build order, the layers the engine uses, and the design choices |
| [docs/settings.md](docs/settings.md) | every setting in `panel.json`, with its type, default, and meaning |
| [docs/panel-fingerprint-api.md](docs/panel-fingerprint-api.md) | the check that tells whether a saved panel still matches its inputs |

**Provided as is, with no support.** Nobody is assigned to answer issues or
pull requests.

## Requirements

| Item | Value |
|---|---|
| KiCad | 10.0 or later |
| Python for the engine | 3.11 or later. The plugin uses the Python that comes with KiCad. |
| Tested platform | Windows |
| Plugin first run | an internet connection, or a local folder of wheel files |
| Administrator rights | not needed |

The engine and the plugin have one version number. Release `v0.1.0` holds
engine `0.1.0` and plugin `0.1.0`.

## Install the KiCad plugin

The plugin is one KiCad package file, a ZIP archive named
`com.github.eedesignpro.eedesignpanels-<version>.zip`. Do not unpack it.

1. Download the package file from the release page of
   `https://github.com/eedesignpro/eedesignpanels`.
2. In the KiCad Project Manager, open the Plugin and Content Manager.
3. Click **Install from File...** and select the package file.
4. Open the PCB Editor. The button **eedesignpanels: build a panel** is on
   the toolbar and under **Tools > External Plugins**.

If the button does not show, click **Tools > External Plugins > Refresh
Plugins**, or close and open the PCB Editor.

## How the plugin installs the engine

The package file holds only the KiCad menu entry. The engine and
`eedesigncore` are separate Python packages. On the first run, the plugin
installs them for the current user with the Python that comes with KiCad.
This takes about a minute and happens once.

| Part | Source |
|---|---|
| `eedesignpanels` engine | the source archive of this repository at the release tag of the plugin, for example `v0.1.0` |
| `eedesigncore` | the source archive of `https://github.com/eedesignpro/eedesigncore` at tag `v0.38.0` |
| Other Python packages, for example `pydantic` | the Python package index that pip uses, PyPI by default |

After the download, the plugin compares the engine version with the version
it expects, and refuses a mismatch.

On Windows, the installed parts go to this folder:

```
%LOCALAPPDATA%\eedesignpro\eedesignpanels\runtime
```

The folder has one subfolder for each KiCad version. A new plugin release, or
a KiCad update that changes the Python version, makes the plugin build a new
copy on the next run. To remove the installed parts, delete the folder. The
log of the last install is `last-fill.log` in the folder above `runtime`.

## Install with no internet connection

The plugin can take every part from a local folder of wheel files and use no
network. A wheel file is a ready-built Python package.

Make the folder on a Windows computer that has an internet connection and the
same KiCad version. Run these commands in a Command Prompt:

1. Build the wheel of `eedesigncore` from its release tag:

```
"C:\Program Files\KiCad\10.0\bin\python.exe" -m pip wheel --no-deps -w eedesignpanels-wheels "eedesigncore @ https://github.com/eedesignpro/eedesigncore/archive/refs/tags/v0.38.0.zip"
```

2. Build the wheel of the engine from the release tag of the plugin:

```
"C:\Program Files\KiCad\10.0\bin\python.exe" -m pip wheel --no-deps -w eedesignpanels-wheels "eedesignpanels @ https://github.com/eedesignpro/eedesignpanels/archive/refs/tags/v0.1.0.zip"
```

3. Download the wheels of every other package that the two need:

```
"C:\Program Files\KiCad\10.0\bin\python.exe" -m pip download --only-binary :all: --find-links eedesignpanels-wheels -d eedesignpanels-wheels eedesigncore==0.38.0 eedesignpanels==0.1.0
```

Copy the folder to the computer with no internet connection. Then:

1. Set the user environment variable `EEDESIGNPANELS_WHEEL_FOLDER` to the
   full path of the folder:

```
setx EEDESIGNPANELS_WHEEL_FOLDER "D:\eedesignpanels-wheels"
```

2. Close KiCad and open it again, so that KiCad reads the variable.
3. Run the plugin. It installs from the folder and uses no network.

The tag and the two versions in these commands are the ones that plugin
release `0.1.0` expects. Each plugin release states them in
`kicad_plugin/plugins/runtime.py`, as `RELEASE_TAG`, `ENGINE_VERSION`, and
`CORE_VERSION`.

## Use the engine from a command line

The engine runs headless, with no KiCad window, on any system with Python
3.11 or later. Install `eedesigncore` first from its release tag, because the
engine pins that exact version:

```
python -m pip install "eedesigncore @ git+https://github.com/eedesignpro/eedesigncore@v0.38.0"
python -m pip install "eedesignpanels @ git+https://github.com/eedesignpro/eedesignpanels@v0.1.0"
```

Then:

| Command | What it does |
|---|---|
| `eedesignpanels --version` | Shows the engine version. |
| `python -m eedesignpanels.preview <project>/panel/panel.json --open` | Opens the browser page. If there is no settings file, it writes one. |
| `python -m eedesignpanels.prep <project>/panel/panel.json` | Draws the router tool path on the source board and writes the board. Add `--preview` to leave the board unchanged. |
| `eedesignpanels <project>/panel/panel.json` | Builds the panel into the `panel/` folder. |
| `python -m eedesignpanels.fingerprints check ...` | Checks a saved panel against its inputs. See [docs/panel-fingerprint-api.md](docs/panel-fingerprint-api.md). |

The settings file `panel.json` names the board and every panel setting.
[docs/settings.md](docs/settings.md) lists every setting. The command-line
build needs an existing settings file. The browser page writes one on its
first start.

The build runs twice and compares the two results. It exits with 0 when
every check passes, 1 when a check fails, and 2 when the settings file is
invalid. A build from the same board and the same settings file gives the
same bytes.

The `stage` setting selects what a build does:

| `stage` | What it does |
|---|---|
| `passthrough` (default) | Loads the board and writes it back, byte for byte. |
| `build_panel` | Steps and repeats the board into a grid, then adds the rails and the other panel items. A settings file that the browser page writes has this stage. |

The `verify` extra adds the gerber comparison tools, for development:

```
python -m pip install "eedesignpanels[verify] @ git+https://github.com/eedesignpro/eedesignpanels@v0.1.0"
```

## Where it writes

The plugin and the engine write beside the board, in a folder named `panel`:

| Path | Content |
|---|---|
| `panel/panel.json` | the panel settings |
| `panel/panel.kicad_pcb` | the panel board |
| `panel/panel.kicad_pro` | the panel project, made from the project file of the design |
| `panel/logs/` | a log of each start from KiCad, and one folder for each browser session |
| `panel/review/` | the review pages of the command-line prep |
| `panel/bug_reports/` | the archives that **Report a Problem** writes |

**Save to Board** on the first page writes over the board file of the
design. It first keeps the previous file beside it as `<board file>-bak`. By
default, it also locks every board item that KiCad can lock. The setting
`lock_board_on_save` turns this off.

**Report a Problem** writes one ZIP archive and one summary file to
`bug_reports/` in the panel folder. The environment variable
`EEDESIGNPANELS_BUG_REPORT_DIR` names another local folder. A network folder
or a web address is refused. Nothing is sent anywhere. The archive holds the
board files, the settings, file paths, and the session log. Read the archive
before you give it to anyone.

## KiCad on this computer

Rendering and copper fill use `kicad-cli`. The engine looks for it in this
order: beside the running Python, then the KiCad install folders of the
operating system, newest version first, then `PATH`. The KiCad settings
folder follows the rule of KiCad, and `KICAD_CONFIG_HOME` overrides it.

Without `kicad-cli`, the browser page draws graphics only and does not fill
copper. A command-line build with rail copper switched on stops with an
error, because it cannot fill the rail copper. With rail copper off, the
command-line build does not need `kicad-cli`.

By default, fiducials come from `Fiducial`, the footprint library that KiCad
installs. A settings file can name any library in the KiCad library table.

## Standalone launcher

`kicad_plugin/launcher/eedesignpanels.cmd` opens the engine without KiCad
running. Double-click it and select a board, or drop a board file on it. It
is not part of the package file. It uses the same install folder and the same
sources as the plugin.

## Known limits

- **Windows only is tested.** On Linux and macOS, the plugin does not find
  the Python that comes with KiCad. It stops with a message and installs
  nothing. The engine itself runs from a command line on those systems.
- **The plugin uses the SWIG action plugin interface of KiCad.** KiCad has
  marked that interface for removal in a future version.
- **Fixed user layers.** The engine reads and writes `User.36` for the
  router path, `User.38` for v-scores, `User.8` for stop lines, `User.39`
  for edge plating, `User.16` for the panel dimensions, and `User.24` and
  `User.37` for rigid-flex regions. A board that keeps other drawings on
  these layers gets them read as panel instructions. Only the dimension layer
  is a setting. See [docs/how-it-works.md](docs/how-it-works.md#layers).
- **Mirror is refused.** `grid.mirror` needs a layer-flip transform that
  `eedesigncore` does not have.
- **The gerber comparison is not exact with released gerbonara.** It needs a
  gerbonara build with finer SVG number formats, and no release has that
  change. With gerbonara 1.6.3, each comparison run prints a warning, and the
  one test that checks the number format is marked as an expected failure.
- **macOS settings folder.** On macOS, `eedesigncore` looks for the KiCad
  settings in `~/.config/kicad`. KiCad itself uses
  `~/Library/Preferences/kicad`.

## Tests

From the top folder of this repository, with the engine installed:

```
python tests/run_fast_suite.py
```

The fast suite leaves out the tests that start a real KiCad. The full suite
is `python -m unittest discover -s tests`. Some tests need a folder of real
boards, and they skip unless `EEDESIGNPANELS_CORPUS` names one.

To build the KiCad package file from `kicad_plugin/`:

```
python kicad_plugin/tools/build_package_file.py
```

## License

Copyright (C) 2026 SeeScan, Inc.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. The full text is in `LICENSE`.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
