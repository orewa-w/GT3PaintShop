# GT3 Paint Shop

A Python tool that adds a **Paint Shop** to GT Auto in *Gran Turismo 3: A-Spec*. It builds modified MBL and PMB menu files using the game's existing car viewer and colour-selection functions, without patching the game executable or car databases.

## Features

- Adds **Paint Shop** to the main GT Auto menu.
- Previews the current car in its available factory colours as you scroll.
- Shows the selected colour's name.
- Uses existing game functions rather than a custom car model or purchase system.

## Requirements

- **Python 3.10 or newer**. No third-party Python packages are required.
- Your own extracted *Gran Turismo 3* menu files.
- A game setup capable of loading modified menu files from an extracted data directory (for example, an external-ELF/HostFS setup in PCSX2).

The tool is designed for the **PAL version (SCES-50294)** with the **BR** menu files. Other game revisions and language folders are not confirmed compatible.

### Required files

```text
menu/
  gt_mode.mbl
  BR/
    gt_mode310.pmb
    gt_mode601.pmb
    gt_mode610.pmb
```

Use `menu/gt_mode.mbl`, **not** the separate `menu/BR/gt_mode.mbl` file. You can also place all four input files together in a single folder.

## Usage

Save `GT3PaintShop.py` somewhere on your computer and open a terminal in that directory.

**Check the input files:**

```powershell
py -3 .\GT3PaintShop.py --source "C:\GT3\data" --check-only
```

**Generate the Paint Shop menu files:**

```powershell
py -3 .\GT3PaintShop.py --source "C:\GT3\data" --output "C:\GT3PaintShop_Output"
```

Replace the example paths with your own. `--source` can point to an extracted game data directory, its parent folder, or a folder containing the four input files. `--output` must be a new or empty folder **outside** the source directory.

### Output

```text
GT3PaintShop_Output/
  build_report.json
  menu/
    gt_mode.mbl
    BR/
      gt_mode601.pmb
      gt_mode610.pmb
```

`gt_mode310.pmb` is used to obtain the existing dealership colour controls; it is not modified or included in the output.

## Installation

1. Back up your original menu files, then close PCSX2.
2. Copy the generated files into the corresponding paths in the **extracted game data directory** used by your game setup.
3. Start the game from a fresh boot and load your normal save rather than an older savestate.
4. Go to **GT Auto → Paint Shop**. Scroll to preview the available colours, then use **CHANGE PAINT** to request the selected colour or **BACK** to leave.


## Notes

- The script generates menu files only. It does not modify the ELF, car databases, or create PNACH files.
- The Paint Shop uses the existing owned-car viewer and native colour-handling functions. It does not add the game's car-purchase action.
- The GT Auto Paint Shop entry currently uses a simple text button rather than a custom service icon.
- Original game assets are **not included**. You must provide your own compatible files.
