"""odb2vtu worker -- runs inside the Abaqus Python interpreter (2.7 or 3.x).

Commands
--------
  odb2vtu_worker.py scan    <odb_path> <json_out>
  odb2vtu_worker.py convert <spec_json>

The GUI launches this script with the Abaqus python executable, because only
that interpreter can import ``odbAccess``.  All console text is ASCII; the GUI
parses the machine readable lines:

  ##PROGRESS <done> <total> <text>
  ##INFO <text>
  ##WARN <text>
  ##DONE <json>
  ##ERROR <text>

Exit code 0 means the command finished; anything else is a failure.
"""

from __future__ import print_function

import base64
import json
import os
import struct
import sys
import traceback

import numpy as np

# --------------------------------------------------------------------------
# Abaqus element type -> VTK cell type
# --------------------------------------------------------------------------
# VTK ids: 3=line 5=triangle 9=quad 10=tetra 12=hex 13=wedge 14=pyramid
#          21=quadratic edge 22=quadratic triangle 23=quadratic quad
#          24=quadratic tetra 25=quadratic hex 26=quadratic wedge 29=tri-quadratic hex

TYPE_MAP = {
    # solids
    'C3D4': 10, 'C3D4H': 10, 'C3D10': 24, 'C3D10M': 24, 'C3D10MH': 24,
    'C3D8': 12, 'C3D8R': 12, 'C3D8I': 12, 'C3D8H': 12, 'C3D8RH': 12,
    'C3D8T': 12, 'C3D8RT': 12,
    'C3D20': 25, 'C3D20R': 25, 'C3D20H': 25, 'C3D20RH': 25,
    'C3D6': 13, 'C3D15': 26, 'C3D5': 14,
    # shells
    'S3': 5, 'S3R': 5, 'S3RS': 5, 'STRI3': 5,
    'S4': 9, 'S4R': 9, 'S4RS': 9, 'S4RSW': 9, 'S4T': 9, 'S4RT': 9,
    'S6': 22, 'S8': 23, 'S8R': 23, 'S8RT': 23,
    # continuum / plane / axisymmetric
    'CPS3': 5, 'CPS4': 9, 'CPS4R': 9, 'CPS6': 22, 'CPS8': 23, 'CPS8R': 23,
    'CPE3': 5, 'CPE4': 9, 'CPE4R': 9, 'CPE4H': 9, 'CPE6': 22, 'CPE8': 23,
    'CPE8R': 23, 'CPE8H': 23,
    'CAX3': 5, 'CAX4': 9, 'CAX4R': 9, 'CAX4H': 9, 'CAX6': 22, 'CAX8': 23,
    'CAX8R': 23, 'CAX8H': 23,
    'CIN3D8': 12, 'CIN3D6': 13, 'CIN3D4': 10,
    # trusses / beams / membranes
    'T3D2': 3, 'T3D3': 21, 'T2D2': 3, 'B31': 3, 'B31H': 3, 'B32': 21, 'B32H': 21,
    'M3D3': 5, 'M3D4': 9, 'M3D4R': 9, 'M3D6': 22, 'M3D8': 23, 'M3D8R': 23,
    # rigid / cohesive / connectors
    'R3D3': 5, 'R3D4': 9, 'R2D2': 3,
    'COH3D6': 13, 'COH3D8': 12, 'COH2D4': 9, 'CONN3D2': 3,
}

# Fallback when the type string is unknown: guess from the node count.
NODE_FALLBACK = {2: 3, 3: 5, 4: 10, 5: 14, 6: 13, 8: 12, 10: 24, 15: 26, 20: 25, 27: 29}
SHELL_PREFIXES = ('S', 'M', 'R2D', 'R3D', 'CPS', 'CPE', 'CAX', 'COH2D')

NODAL_POSITIONS = ('NODAL', 'ELEMENT_NODAL')
ELEMENT_POSITIONS = ('INTEGRATION_POINT', 'CENTROID')


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def log(msg):
    print(msg)
    sys.stdout.flush()


def info(msg):
    log('##INFO ' + msg)


def warn(msg):
    log('##WARN ' + msg)


def progress(done, total, text=''):
    log('##PROGRESS %d %d %s' % (done, total, text))


def sym(value):
    """SymbolicConstant -> plain upper case name."""
    try:
        return str(value).split('.')[-1].upper()
    except Exception:
        return ''


def native(value):
    """Path / name that the local filesystem and ODB dictionaries accept."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        try:
            return value.encode(sys.getfilesystemencoding() or 'mbcs')
        except Exception:
            return value.encode('utf-8', 'replace')
    return value


def write_text(path, text):
    stream = open(native(path), 'wb')
    try:
        stream.write(text.encode('utf-8'))
    finally:
        stream.close()


def write_json(path, data):
    write_text(path, json.dumps(data, indent=2, sort_keys=True))


def as_components(data):
    try:
        return [float(v) for v in data]
    except Exception:
        try:
            return [float(data)]
        except Exception:
            return []


def safe_name(text):
    out = []
    for ch in str(text):
        if ch.isalnum() or ch in '._-':
            out.append(ch)
        else:
            out.append('_')
    name = ''.join(out).strip('_')
    return name or 'step'


def guess_vtk_type(abaqus_type, node_count, warnings):
    key = str(abaqus_type or '').upper().strip()
    if key in TYPE_MAP:
        return TYPE_MAP[key]
    if key and key[0] in SHELL_PREFIXES and node_count in (3, 4, 6, 8):
        mapping = {3: 5, 4: 9, 6: 22, 8: 23}
        warnings.append('Element type %s is not in the table; guessed VTK %d from %d nodes.'
                        % (abaqus_type, mapping[node_count], node_count))
        return mapping[node_count]
    if node_count in NODE_FALLBACK:
        warnings.append('Element type %s is not in the table; guessed VTK %d from %d nodes.'
                        % (abaqus_type, NODE_FALLBACK[node_count], node_count))
        return NODE_FALLBACK[node_count]
    warnings.append('Element type %s (%d nodes) cannot be mapped to VTK and was dropped.'
                    % (abaqus_type, node_count))
    return None


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------
def field_position(fo):
    """'NODAL' / 'INTEGRATION_POINT' / ... -- lives on the FieldValue."""
    try:
        values = fo.values
        if len(values):
            return sym(values[0].position)
    except Exception:
        pass
    return ''


def open_odb(path):
    from odbAccess import openOdb
    return openOdb(path=native(path), readOnly=True)


def close_quietly(odb):
    """odb.close() drags in Abaqus' ctypes hook, which an isolated SMAPython
    cannot satisfy.  The OS releases the read-only handle when we exit."""
    try:
        odb.close()
    except Exception:
        pass


def scan(odb_path, json_out):
    odb = open_odb(odb_path)
    try:
        abaqus_version = 'unknown'
        root = odb.rootAssembly
        instances = []
        for name in root.instances.keys():
            inst = root.instances[name]
            types = {}
            for el in inst.elements:
                key = str(el.type)
                types[key] = types.get(key, 0) + 1
            instances.append(dict(name=name, nodes=len(inst.nodes), elements=len(inst.elements),
                                  element_types=types,
                                  node_sets=list(inst.nodeSets.keys()),
                                  element_sets=list(inst.elementSets.keys())))
        steps = []
        for step_name in odb.steps.keys():
            step = odb.steps[step_name]
            frames = list(step.frames)
            variables = {}
            for frame_index, frame in enumerate(frames):
                for var_name in list(frame.fieldOutputs.keys()):
                    rec = variables.get(var_name)
                    if rec is None:
                        rec = variables[var_name] = dict(name=var_name, onsets=[], count=0,
                                                         position='', type='', components=[])
                    rec['onsets'].append(frame_index)
                    rec['count'] = len(frame.fieldOutputs[var_name].values)
            for var_name, rec in variables.items():
                fo = frames[rec['onsets'][-1]].fieldOutputs[var_name]
                rec['position'] = field_position(fo)
                rec['type'] = sym(fo.type)
                rec['components'] = [str(c) for c in (getattr(fo, 'componentLabels', ()) or ())]
                rec['location'] = 'node' if rec['position'] in NODAL_POSITIONS else 'element'
                present = []
                for name in root.instances.keys():
                    try:
                        if len(fo.getSubset(region=root.instances[name]).values):
                            present.append(name)
                    except Exception:
                        pass
                rec['instances'] = present
            step_vars = sorted(variables.values(), key=lambda r: (r['location'], r['name']))
            frame_meta = []
            for frame_index, frame in enumerate(frames):
                frame_meta.append(dict(index=frame_index,
                                       value=float(frame.frameValue),
                                       increment=int(getattr(frame, 'incrementNumber',
                                                             frame_index)),
                                       description=str(getattr(frame, 'description', ''))[:300]))
            steps.append(dict(name=step_name, domain=sym(step.domain), frames=len(frames),
                              frame_meta=frame_meta, variables=step_vars))
        payload = dict(ok=True, odb=odb_path, abaqus_version=str(abaqus_version),
                       instances=instances, steps=steps)
    finally:
        close_quietly(odb)
    write_json(json_out, payload)


# --------------------------------------------------------------------------
# mesh geometry (frame independent)
# --------------------------------------------------------------------------
class Geometry(object):
    def __init__(self):
        self.points = np.zeros((0, 3))
        self.conn = []
        self.types = []
        self.cell_instance = []
        self.cell_label = []
        self.node_index = {}
        self.cell_index = {}
        self.warnings = []

    @property
    def ncells(self):
        return len(self.conn)

    @property
    def npoints(self):
        return self.points.shape[0]


def build_geometry(odb, only=None):
    geom = Geometry()
    root = odb.rootAssembly
    blocks = []
    offset = 0
    names = [n for n in root.instances.keys() if not only or n in only]
    if not names:
        raise RuntimeError('No instance selected for export.')
    for name in names:
        inst = root.instances[name]
        local = np.zeros((len(inst.nodes), 3), dtype=np.float64)
        labels = []
        for i, nd in enumerate(inst.nodes):
            c = nd.coordinates
            local[i, 0] = float(c[0])
            local[i, 1] = float(c[1])
            local[i, 2] = float(c[2])
            labels.append(int(nd.label))
        blocks.append(local)
        geom.node_index[name] = dict((label, offset + i) for i, label in enumerate(labels))
        # Abaqus stores element connectivity as 1-based *node labels*, not as
        # zero-based indices, so every entry has to go through the label map.
        # Older / foreign ODBs have been seen with zero-based indices, hence the
        # convention is detected once per instance and the other one is tried
        # as a fall back.
        index = geom.node_index[name]
        cell_map = {}
        mode = None
        dropped = 0
        for el in inst.elements:
            raw = [int(c) for c in el.connectivity]
            if not raw:
                continue
            if mode is None:
                if all(c in index for c in raw):
                    mode = 'label'
                elif all(0 <= c < len(labels) for c in raw):
                    mode = 'zero'
                else:
                    mode = 'label'
            conn = []
            for c in raw:
                if mode == 'label':
                    position = index.get(c)
                elif 0 <= c < len(labels):
                    position = offset + c
                else:
                    position = None
                if position is None:
                    conn = None
                    break
                conn.append(position)
            if conn is None:
                dropped += 1
                continue
            vtk_type = guess_vtk_type(el.type, len(conn), geom.warnings)
            if vtk_type is None:
                continue
            cell_map[int(el.label)] = len(geom.conn)
            geom.conn.append(conn)
            geom.types.append(vtk_type)
            geom.cell_instance.append(name)
            geom.cell_label.append(int(el.label))
        if dropped:
            geom.warnings.append(
                '%d element(s) of %s pointed at nodes that do not exist in the instance '
                'and were dropped.' % (dropped, name))
        geom.cell_index[name] = cell_map
        offset += len(labels)
    geom.points = np.vstack(blocks) if blocks else np.zeros((0, 3))
    return geom


def validate_geometry(geom):
    """Reject a mesh whose connectivity would corrupt the downstream reader.

    A cell index outside ``[0, npoints)`` makes ParaView allocate cells with
    bogus point ids.  VTK then writes past its own arrays and the process dies
    with a heap corruption (0xc0000374 in ntdll) instead of reporting an error,
    which is exactly the crash that was traced back to the first release.
    """
    if not geom.conn:
        return
    low = min(min(cell) for cell in geom.conn)
    high = max(max(cell) for cell in geom.conn)
    if low < 0 or high >= geom.npoints:
        raise RuntimeError(
            'internal error: cell connectivity spans %d..%d but the mesh has %d points'
            % (low, high, geom.npoints))
    for cell in geom.conn:
        if len(set(cell)) != len(cell):
            geom.warnings.append('Some cells repeat a node id; their shape is degenerate.')
            break


# --------------------------------------------------------------------------
# field extraction
# --------------------------------------------------------------------------
def point_field(odb, frame, geom, var_name, ncomp):
    """Nodal values of one field output, averaged onto the merged point list."""
    out = np.zeros((geom.npoints, ncomp), dtype=np.float64)
    counts = np.zeros(geom.npoints, dtype=np.float64)
    fo = frame.fieldOutputs[var_name]
    root = odb.rootAssembly
    for iname in geom.node_index:
        try:
            subset = fo.getSubset(region=root.instances[iname])
        except Exception:
            continue
        index = geom.node_index[iname]
        for value in subset.values:
            label = getattr(value, 'nodeLabel', None)
            if label is None:
                continue
            idx = index.get(int(label))
            if idx is None:
                continue
            data = as_components(value.data)
            n = min(ncomp, len(data))
            if n <= 0:
                continue
            out[idx, :n] += np.asarray(data[:n])
            counts[idx] += 1.0
    return out, counts


def cell_field(odb, frame, geom, var_name, ncomp):
    """Element values of one field output (integration points averaged per element)."""
    out = np.zeros((geom.ncells, ncomp), dtype=np.float64)
    counts = np.zeros(geom.ncells, dtype=np.float64)
    fo = frame.fieldOutputs[var_name]
    root = odb.rootAssembly
    for iname in geom.cell_index:
        try:
            subset = fo.getSubset(region=root.instances[iname])
        except Exception:
            continue
        index = geom.cell_index[iname]
        for value in subset.values:
            label = getattr(value, 'elementLabel', None)
            if label is None:
                continue
            idx = index.get(int(label))
            if idx is None:
                continue
            data = as_components(value.data)
            n = min(ncomp, len(data))
            if n <= 0:
                continue
            out[idx, :n] += np.asarray(data[:n])
            counts[idx] += 1.0
    return out, counts


def average(values, counts):
    result = values.copy()
    mask = counts > 1.0
    if np.any(mask):
        result[mask] = values[mask] / counts[mask][:, None]
    missing = counts == 0.0
    if np.any(missing):
        result[missing] = np.nan
    return result


def mises_components(labels):
    """Index map for a symmetric tensor if the classic six labels are present."""
    clean = {}
    for i, label in enumerate(labels):
        text = str(label).upper()
        for key in ('11', '22', '33', '12', '13', '23', '23'):
            if text.endswith(key) or text == key:
                clean.setdefault(key, i)
                break
    if not all(k in clean for k in ('11', '22', '33')):
        return None
    return clean


def mises(array, mapping):
    s11 = array[:, mapping['11']]
    s22 = array[:, mapping['22']]
    s33 = array[:, mapping['33']]
    s12 = array[:, mapping['12']] if '12' in mapping else 0.0
    s13 = array[:, mapping['13']] if '13' in mapping else 0.0
    s23 = array[:, mapping['23']] if '23' in mapping else 0.0
    return np.sqrt(0.5 * ((s11 - s22) ** 2 + (s22 - s33) ** 2 + (s33 - s11) ** 2)
                   + 3.0 * (s12 ** 2 + s13 ** 2 + s23 ** 2))


def component_label(var_name, labels, index, ncomp):
    if labels and index < len(labels) and labels[index]:
        label = str(labels[index])
        if label.upper().startswith(var_name.upper()):
            return label
        return var_name + '_' + label
    if ncomp == 1:
        return var_name
    return '%s_%d' % (var_name, index + 1)


def add_field(arrays, var_name, values, labels, field_type, location):
    """Split a field into ParaView friendly arrays (vectors stay vectors)."""
    values = np.asarray(values)
    ncomp = 1 if values.ndim == 1 else values.shape[1]
    if ncomp == 1:
        arrays[var_name] = (values.reshape(-1, 1) if values.ndim == 1 else values, 'scalar')
        return
    if ncomp == 3 and field_type == 'VECTOR':
        arrays[var_name] = (values, 'vector')
        arrays[var_name + '_Magnitude'] = (
            np.sqrt((values ** 2).sum(axis=1)).reshape(-1, 1), 'scalar')
        return
    for i in range(ncomp):
        name = component_label(var_name, labels, i, ncomp)
        arrays[name] = (values[:, i:i + 1], 'scalar')
    if ncomp in (4, 6) and var_name.upper() == 'S':
        mapping = mises_components(labels)
        if mapping:
            arrays[var_name + '_Mises'] = (mises(values, mapping).reshape(-1, 1), 'scalar')


# --------------------------------------------------------------------------
# VTK XML output
# --------------------------------------------------------------------------
def xml_escape(text):
    return (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def encode(array, dtype, binary, precision, integer=False):
    data = np.ascontiguousarray(array, dtype=dtype)
    if binary:
        raw = data.tobytes() if hasattr(data, 'tobytes') else data.tostring()
        payload = struct.pack('<I', len(raw)) + raw
        return base64.b64encode(payload).decode('ascii')
    flat = data.ravel()
    if integer:
        return ' '.join([str(int(v)) for v in flat])
    # VTK's ascii reader aborts on "nan"/"inf" tokens, so the caller sanitizes.
    return ' '.join([precision % float(v) for v in flat])


def has_non_finite(arrays):
    for name in arrays:
        values = np.asarray(arrays[name][0], dtype=np.float64)
        if not np.all(np.isfinite(values)):
            return True
    return False


def sanitize(arrays):
    clean = {}
    for name in arrays:
        values = np.asarray(arrays[name][0], dtype=np.float64)
        clean[name] = (np.where(np.isfinite(values), values, 0.0), arrays[name][1])
    return clean



def write_vtu(path, points, conn, types, point_arrays, cell_arrays, binary=False,
              precision='%.12g'):
    if conn:
        high = max(max(cell) for cell in conn)
        low = min(min(cell) for cell in conn)
        if low < 0 or high >= len(points):
            raise RuntimeError('refusing to write %s: point index %d..%d out of range for %d '
                               'points (ParaView would crash on such a file)'
                               % (path, low, high, len(points)))
    sanitized = False
    if not binary:
        if has_non_finite(point_arrays) or has_non_finite(cell_arrays):
            point_arrays = sanitize(point_arrays)
            cell_arrays = sanitize(cell_arrays)
            sanitized = True
    fmt = 'binary' if binary else 'ascii'
    parts = ['<?xml version="1.0"?>\n',
             '<VTKFile type="UnstructuredGrid" version="1.0" byte_order="LittleEndian"',
             ' header_type="UInt32">\n<UnstructuredGrid>\n',
             '<Piece NumberOfPoints="%d" NumberOfCells="%d">\n' % (len(points), len(conn)),
             '<Points><DataArray type="Float64" NumberOfComponents="3" format="%s">' % fmt,
             encode(points, np.float64, binary, precision),
             '</DataArray></Points>\n<Cells>\n']

    flat = np.concatenate([np.asarray(c, dtype=np.int64) for c in conn]) if conn \
        else np.zeros(0, dtype=np.int64)
    offsets = np.cumsum(np.asarray([len(c) for c in conn], dtype=np.int64))
    parts += ['<DataArray type="Int64" Name="connectivity" format="%s">' % fmt,
              encode(flat, np.int64, binary, precision, integer=True),
              '</DataArray>\n<DataArray type="Int64" Name="offsets" format="%s">' % fmt,
              encode(offsets, np.int64, binary, precision, integer=True),
              '</DataArray>\n<DataArray type="UInt8" Name="types" format="%s">' % fmt,
              encode(np.asarray(types, dtype=np.uint8), np.uint8, binary, precision,
                     integer=True),
              '</DataArray>\n</Cells>\n']

    def block(tag, arrays):
        parts.append('<%s>\n' % tag)
        for name in sorted(arrays):
            values = np.asarray(arrays[name][0])
            ncomp = 1 if values.ndim == 1 else values.shape[1]
            parts.append('<DataArray type="Float64" Name="%s" NumberOfComponents="%d"'
                         ' format="%s">' % (xml_escape(name), ncomp, fmt))
            parts.append(encode(values, np.float64, binary, precision))
            parts.append('</DataArray>\n')
        parts.append('</%s>\n' % tag)

    block('PointData', point_arrays)
    block('CellData', cell_arrays)
    parts.append('</Piece>\n</UnstructuredGrid>\n</VTKFile>\n')
    write_text(path, ''.join(parts))
    return sanitized


def write_pvd(path, entries):
    parts = ['<?xml version="1.0"?>\n<VTKFile type="Collection" version="0.1"',
             ' byte_order="LittleEndian">\n<Collection>\n']
    for timestep, filename in entries:
        parts.append('<DataSet timestep="%.12g" group="" part="0" file="%s"/>\n'
                     % (timestep, xml_escape(filename)))
    parts.append('</Collection>\n</VTKFile>\n')
    write_text(path, ''.join(parts))


# --------------------------------------------------------------------------
# convert
# --------------------------------------------------------------------------
def load_spec(path):
    stream = open(native(path), 'rb')
    try:
        text = stream.read().decode('utf-8-sig')
    finally:
        stream.close()
    return json.loads(text.lstrip(u'\ufeff'))


def convert(spec_path):
    spec = load_spec(spec_path)
    odb_path = native(spec['odb'])
    outdir = native(spec['outdir'])
    prefix = safe_name(spec.get('prefix') or os.path.splitext(os.path.basename(odb_path))[0])
    variables = [native(v) for v in spec.get('variables') or []]
    deform = bool(spec.get('deform'))
    scale = float(spec.get('deform_scale', 1.0) or 1.0)
    binary = bool(spec.get('binary'))
    precision = spec.get('precision') or '%.12g'
    want_pvd = bool(spec.get('pvd', True))
    wanted_steps = spec.get('steps') or []

    if not os.path.isdir(outdir):
        os.makedirs(outdir)

    odb = open_odb(odb_path)
    written = []
    warnings = []
    try:
        info('Building mesh from ODB instances...')
        only = spec.get('instances')
        if only:
            only = set([native(n) for n in only])
        geom = build_geometry(odb, only)
        warnings.extend(geom.warnings)
        info('Mesh: %d points, %d cells' % (geom.npoints, geom.ncells))
        validate_geometry(geom)

        jobs = []
        for step_spec in wanted_steps:
            step_name = native(step_spec['name'])
            if step_name not in odb.steps:
                warnings.append('Step %s not found in ODB.' % step_name)
                continue
            step = odb.steps[step_name]
            frames = list(step.frames)
            selection = step_spec.get('frames', 'last')
            if selection == 'all':
                indices = list(range(len(frames)))
            elif selection == 'last':
                indices = [len(frames) - 1] if frames else []
            else:
                indices = [int(i) for i in selection if 0 <= int(i) < len(frames)]
            if not indices:
                warnings.append('Step %s has no frame to export.' % step_name)
                continue
            jobs.append((step_name, frames, indices, len(indices) > 1 and want_pvd))

        total = sum(len(job[2]) for job in jobs)
        done = 0
        for step_name, frames, indices, needs_pvd in jobs:
            pvd_entries = []
            for frame_index in indices:
                frame = frames[frame_index]
                done += 1
                progress(done, total, '%s frame %d' % (step_name, frame_index))

                points = geom.points
                point_arrays = {}
                cell_arrays = {}

                u_values = None
                if 'U' in frame.fieldOutputs and (deform or 'U' in variables):
                    u_values, u_counts = point_field(odb, frame, geom, 'U', 3)
                    u_values = average(u_values, u_counts)
                if deform:
                    points = geom.points.copy()
                    if u_values is not None:
                        points = points + scale * u_values
                    else:
                        warn('No U field in this frame; writing undeformed coordinates.')

                exported = []
                for var_name in variables:
                    if var_name not in frame.fieldOutputs:
                        continue
                    fo = frame.fieldOutputs[var_name]
                    position = field_position(fo)
                    labels = [str(c) for c in (getattr(fo, 'componentLabels', ()) or ())]
                    ftype = sym(fo.type)
                    if position in NODAL_POSITIONS:
                        values, counts = point_field(odb, frame, geom, var_name,
                                                     max(1, len(labels) or 1))
                        values = average(values, counts)
                        add_field(point_arrays, var_name, values, labels, ftype, 'node')
                    else:
                        ncomp = max(1, len(labels) or 1)
                        values, counts = cell_field(odb, frame, geom, var_name, ncomp)
                        values = average(values, counts)
                        add_field(cell_arrays, var_name, values, labels, ftype, 'cell')
                    exported.append(var_name)

                filename = '%s_%s_f%04d.vtu' % (prefix, safe_name(step_name), frame_index)
                path = os.path.join(outdir, filename)
                if write_vtu(path, points, geom.conn, geom.types, point_arrays,
                             cell_arrays, binary=binary, precision=precision):
                    warnings.append('ASCII output replaces NaN values (points covered by no '
                                    'instance) with 0. Use binary output to keep them.')
                size = os.path.getsize(path)
                written.append(dict(file=filename, path=path, step=step_name,
                                    frame=frame_index, frame_value=float(frame.frameValue),
                                    variables=exported, bytes=size))
                info('wrote %s (%.1f MB, %d point arrays, %d cell arrays)'
                     % (filename, size / 1048576.0, len(point_arrays), len(cell_arrays)))
                pvd_entries.append((float(frame.frameValue), filename))
            if needs_pvd and pvd_entries:
                pvd_name = '%s_%s.pvd' % (prefix, safe_name(step_name))
                write_pvd(os.path.join(outdir, pvd_name), pvd_entries)
                written.append(dict(file=pvd_name, path=os.path.join(outdir, pvd_name),
                                    kind='collection'))
                info('wrote %s (%d timesteps)' % (pvd_name, len(pvd_entries)))
    finally:
        close_quietly(odb)

    summary = dict(ok=True, odb=spec['odb'], outdir=spec['outdir'], prefix=prefix,
                   variables=[v.decode('mbcs', 'replace') if isinstance(v, bytes) else v
                              for v in variables],
                   files=written, warnings=sorted(set(warnings)))
    summary_path = os.path.join(outdir, '%s_odb2vtu_summary.json' % prefix)
    write_json(summary_path, summary)
    log('##DONE ' + json.dumps(dict(ok=True, outdir=summary['outdir'], prefix=prefix,
                                    files=[w['file'] for w in written],
                                    warnings=summary['warnings'])))


# --------------------------------------------------------------------------
def main(argv):
    try:
        if len(argv) < 2:
            raise SystemExit('usage: odb2vtu_worker.py scan|convert <args>')
        command = argv[1]
        if command == 'scan':
            if len(argv) < 4:
                raise SystemExit('usage: odb2vtu_worker.py scan <odb> <json_out>')
            scan(argv[2], argv[3])
        elif command == 'convert':
            if len(argv) < 3:
                raise SystemExit('usage: odb2vtu_worker.py convert <spec.json>')
            convert(argv[2])
        else:
            raise SystemExit('unknown command: %s' % command)
    except SystemExit:
        raise
    except Exception:
        text = traceback.format_exc()
        log(text)
        last = text.strip().splitlines()[-1] if text.strip() else 'unknown error'
        log('##ERROR ' + last)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))