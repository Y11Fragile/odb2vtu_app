# Abaqus ODB to VTU Converter

A lightweight Windows GUI and command-line tool for converting Abaqus `.odb` results into ParaView-compatible `.vtu` files.

Features:
- Select output variables, analysis steps, frames, and instances.
- Export nodal fields as PointData and element/integration-point fields as CellData.
- Binary/ASCII VTU, optional PVD time series, and optional displaced geometry.
- Supports many standard Abaqus element types and custom result fields.
- No third-party packages required by the Abaqus-side conversion worker.

## Requirements

- Abaqus with `odbAccess` (tested with Abaqus 2022)
- Python 3.8 or newer with `tkinter` for the GUI
- ParaView (optional, for viewing exports)

## Getting started (Windows)

1. Clone/download this repository.
2. Double-click `启动.bat` (or `调试启动.bat` to see diagnostic output).
3. Select an ODB, scan its contents, choose variables and frames, then convert.
4. Open the exported VTU/PVD in ParaView.

A small sample Abaqus ODB is packaged in `sample/cube_sample.zip` (extract before use). A [GUI screenshot](docs/screenshot.jpg) is included.

## CLI

Run with Abaqus Python:

```bat
SMAPython.exe worker\odb2vtu_worker.py scan case.odb scan.json
SMAPython.exe worker\odb2vtu_worker.py convert spec.json
```

## Repository hygiene

Local settings (`odb2vtu_config.json`) and generated simulation/conversion results are ignored. The sample ODB is retained as a small reproduction fixture.

## License

No open-source license has been selected yet. All rights reserved unless a license is added.