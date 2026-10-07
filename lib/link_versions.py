# -*- coding: utf-8 -*-
"""Cloud link versions: collects version/date of the cloud links of a model,
keeps a per-model history to report what changed since the last run, and
draws a legend table (LINK NAME | VERSION | LAST MODIFIED).

Version numbers come from Desktop Connector's local metadata
(VersionUrn ...?version=N); Revit does not expose ACC version numbers.
"""
import os
import re
import json
import codecs
import datetime

from System import DateTime, DateTimeKind
from System.IO import File, FileMode, FileAccess, FileShare, MemoryStream, Directory, SearchOption
from System.Text import Encoding
from System.Text.RegularExpressions import Regex, RegexOptions
import Autodesk.Revit.DB as DB

HISTORY_PATH = os.path.join(os.environ.get('APPDATA', ''), 'pyRevit', 'ConcretoTools', 'link_versions.json')
DC_ACC_ROOT = os.path.join(os.environ.get('USERPROFILE', ''), 'DC', 'ACCDocs')
DC_DATA = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Autodesk', 'Desktop Connector', 'Data')
US_DATE = '%m/%d/%Y'


# ---------------------------------------------------------------- versions
def dc_versions(names):
    """{file name: highest ACC version found in Desktop Connector metadata}."""
    found = {}
    if not names or not os.path.isdir(DC_DATA):
        return found
    pats = {}
    for n in names:
        pats[n] = Regex(r'\\?"Name\\?"\s*:\s*\\?"' + Regex.Escape(n) + r'\\?".{0,600}?version=(\d+)',
                        RegexOptions.Singleline)
    for f in Directory.GetFiles(DC_DATA, '*', SearchOption.AllDirectories):
        ext = os.path.splitext(f)[1].lower()
        if ext not in ('.sst', '.log', '.db'):
            continue
        try:
            fs = File.Open(f, FileMode.Open, FileAccess.Read, FileShare.ReadWrite)
            ms = MemoryStream()
            fs.CopyTo(ms)
            fs.Close()
            txt = Encoding.UTF8.GetString(ms.ToArray())
        except Exception:
            continue
        for n, rx in pats.items():
            for m in rx.Matches(txt):
                v = int(m.Groups[1].Value)
                if v > found.get(n, 0):
                    found[n] = v
    return found


def _is_cloud_path(p):
    p = (p or '').replace('/', '\\').lower()
    return ('\\dc\\accdocs\\' in p or p.startswith('autodesk') or p.startswith('bim 360')
            or p.startswith('cloud:'))


def _is_cdx_reference(ref):
    """CAD linked from ACC/Forma through the cloud resource server: it has an
    empty InSessionPath and identifies the file with a CDX_FILE_ID instead."""
    try:
        return 'CDX_FILE_ID' in ref.GetReferenceInformation().Keys
    except Exception:
        return False


def _mtime(p):
    try:
        if p and os.path.exists(p):
            return datetime.datetime.fromtimestamp(os.path.getmtime(p))
    except Exception:
        pass
    return None


def _cloud_to_dc_path(cloud_path):
    """'Autodesk Forma://Account/Project/Project Files/...' -> Desktop Connector local path."""
    m = re.match(r'^[^:]+://(.+)$', cloud_path or '')
    if not m:
        return None
    return os.path.join(DC_ACC_ROOT, *m.group(1).split('/'))


def collect(doc):
    """All cloud links of doc (Revit and CAD). Each item is a dict:
    name, kind, loaded, version, date (datetime or None), stale (bool), note."""
    items = []
    # Revit links
    for lt in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkType):
        if lt.IsNestedLink:
            continue
        ld = None
        for li in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance):
            if li.GetTypeId() == lt.Id:
                ld = li.GetLinkDocument()
                break
        name = lt.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
        path = ld.PathName if ld is not None else ''
        cloud = ld is not None and (ld.IsModelInCloud or _is_cloud_path(path))
        if ld is None:
            try:
                efr = lt.GetExternalFileReference()
                path = DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(efr.GetAbsolutePath())
                cloud = _is_cloud_path(path)
            except Exception:
                cloud = False
        if not cloud:
            continue
        it = {'name': name, 'kind': 'RVT', 'loaded': ld is not None, 'version': None,
              'date': _mtime(path), 'stale': False, 'note': ''}
        if ld is None:
            it['note'] = 'Not loaded'
        elif path and os.path.exists(path):
            try:
                loaded = DB.Document.GetDocumentVersion(ld).NumberOfSaves
                disk = DB.BasicFileInfo.Extract(path).GetDocumentVersion().NumberOfSaves
                if loaded != disk:
                    it['stale'] = True
                    it['date'] = None
                    it['note'] = ('Loaded link is out of date: reload' if disk > loaded
                                  else 'Desktop Connector has not synced the loaded version')
            except Exception:
                pass
        items.append(it)
    # CAD links
    for ct in DB.FilteredElementCollector(doc).OfClass(DB.CADLinkType):
        name = ct.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
        it = None
        try:
            refs = ct.GetExternalResourceReferences()
            for k in refs.Keys:
                r = refs[k]
                if not (_is_cloud_path(r.InSessionPath) or _is_cdx_reference(r)):
                    continue
                loaded = None
                try:
                    loaded = DateTime(long(r.Version), DateTimeKind.Utc).ToLocalTime()
                    loaded = datetime.datetime(loaded.Year, loaded.Month, loaded.Day,
                                               loaded.Hour, loaded.Minute, loaded.Second)
                except Exception:
                    pass
                disk = _mtime(_cloud_to_dc_path(r.InSessionPath))
                it = {'name': name, 'kind': 'CAD', 'loaded': True, 'version': None,
                      'date': loaded or disk, 'stale': False, 'note': ''}
                # the version number from DC is only valid if it is the loaded file
                if loaded and disk and abs((disk - loaded).total_seconds()) > 2:
                    it['stale'] = True
                    it['note'] = ('Loaded link is out of date: reload' if disk > loaded
                                  else 'Desktop Connector has not synced the loaded version')
                break
        except Exception:
            pass
        if it is None and ct.IsExternalFileReference():
            try:
                efr = ct.GetExternalFileReference()
                p = DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(efr.GetAbsolutePath())
                if _is_cloud_path(p):
                    it = {'name': name, 'kind': 'CAD', 'loaded': True, 'version': None,
                          'date': _mtime(p), 'stale': False, 'note': ''}
            except Exception:
                pass
        if it is not None:
            items.append(it)
    vers = dc_versions([i['name'] for i in items])
    for i in items:
        if not i['stale']:
            i['version'] = vers.get(i['name'])
    items.sort(key=lambda i: i['name'].lower())
    return items


# ---------------------------------------------------------------- history
def _load_history():
    try:
        with codecs.open(HISTORY_PATH, 'r', 'utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _state(items):
    out = {}
    for i in items:
        out[i['name']] = {'version': i['version'],
                          'date': i['date'].strftime('%Y-%m-%d %H:%M:%S') if i['date'] else None}
    return out


def last_run(doc_key):
    return _load_history().get(doc_key)


def compare(prev, items):
    """Lines describing what changed since prev run (None = first run)."""
    if not prev:
        return None
    old = prev.get('links', {})
    now = _state(items)
    lines = []
    for n in sorted(now, key=lambda s: s.lower()):
        a, b = old.get(n), now[n]
        if a is None:
            lines.append((n, 'New link', '', fmt_version(b['version'])))
            continue
        changed = (a['version'] != b['version']) if (a['version'] and b['version']) else (a['date'] != b['date'])
        if changed:
            lines.append((n, 'Updated',
                          '%s  %s' % (fmt_version(a['version']), _us(a['date'])),
                          '%s  %s' % (fmt_version(b['version']), _us(b['date']))))
    for n in old:
        if n not in now:
            lines.append((n, 'No longer linked', '', ''))
    return lines


def save_run(doc_key, items, selected):
    hist = _load_history()
    hist[doc_key] = {'run': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                     'links': _state(items), 'selected': list(selected)}
    folder = os.path.dirname(HISTORY_PATH)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with codecs.open(HISTORY_PATH, 'w', 'utf-8') as f:
        json.dump(hist, f, indent=1, ensure_ascii=False)


def fmt_version(v):
    return ('V%d' % v) if v else u'\u2014'


def _us(iso):
    if not iso:
        return u'\u2014'
    try:
        return datetime.datetime.strptime(iso, '%Y-%m-%d %H:%M:%S').strftime(US_DATE)
    except Exception:
        return iso


def fmt_date(d):
    return d.strftime(US_DATE) if d else u'\u2014'


# ---------------------------------------------------------------- legend
def legends(doc):
    return sorted([v for v in DB.FilteredElementCollector(doc).OfClass(DB.View)
                   if v.ViewType == DB.ViewType.Legend and not v.IsTemplate], key=lambda v: v.Name)


def sheets_showing(doc, view):
    n = 0
    for s in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet):
        for vp in s.GetAllViewports():
            if doc.GetElement(vp).ViewId == view.Id:
                n += 1
                break
    return n


def _text_type(doc, preferred='01-Standard'):
    for t in DB.FilteredElementCollector(doc).OfClass(DB.TextNoteType):
        if t.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM).AsString() == preferred:
            return t
    return doc.GetElement(doc.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType))


def _line_style(doc, name='<Thin Lines>'):
    cat = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
    for sc in cat.SubCategories:
        if sc.Name == name:
            return sc.GetGraphicsStyle(DB.GraphicsStyleType.Projection)
    return None


def write_legend(doc, view, items):
    """Clears view-owned content of the legend and draws the table.
    Must run inside an open transaction."""
    owned = [e.Id for e in DB.FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
             if e.OwnerViewId == view.Id and isinstance(e, (DB.TextNote, DB.CurveElement))]
    from System.Collections.Generic import List
    if owned:
        doc.Delete(List[DB.ElementId](owned))

    tt = _text_type(doc)
    ts = tt.get_Parameter(DB.BuiltInParameter.TEXT_SIZE).AsDouble()   # paper feet
    s = float(view.Scale)
    char_w = ts * 1.1
    pad = 0.010
    head_h, row_h = 0.032, 0.02625
    rows = [('LINK NAME', 'VERSION', 'LAST MODIFIED')] + \
           [(i['name'], fmt_version(i['version']), fmt_date(i['date'])) for i in items]
    widths = [max(0.344, max(len(r[0]) for r in rows) * char_w + 2 * pad),
              max(0.110, max(len(r[1]) for r in rows) * char_w + 2 * pad),
              max(0.214, max(len(r[2]) for r in rows) * char_w + 2 * pad)]
    xs = [0.0, widths[0], widths[0] + widths[1], sum(widths)]
    top = 0.016
    ys = [top, top - head_h]
    for _ in items:
        ys.append(ys[-1] - row_h)

    opts = DB.TextNoteOptions(tt.Id)
    opts.HorizontalAlignment = DB.HorizontalTextAlignment.Left
    for ri, r in enumerate(rows):
        cell_top, cell_h = ys[ri], ys[ri] - ys[ri + 1]
        y = cell_top - (cell_h - ts * 1.2) / 2.0
        for ci, txt in enumerate(r):
            DB.TextNote.Create(doc, view.Id, DB.XYZ((xs[ci] + pad) * s, y * s, 0), txt, opts)

    style = _line_style(doc)

    def line(x0, y0, x1, y1):
        c = doc.Create.NewDetailCurve(view, DB.Line.CreateBound(DB.XYZ(x0 * s, y0 * s, 0), DB.XYZ(x1 * s, y1 * s, 0)))
        if style is not None:
            c.LineStyle = style
    for y in ys:
        line(xs[0], y, xs[-1], y)
    for x in xs:
        line(x, ys[0], x, ys[-1])
