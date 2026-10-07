# -*- coding: utf-8 -*-
"""Link health check for Revit links and CAD (DWG/DXF/...) links and imports.

Each finding is (severity, text) with severity 'error', 'warning' or 'info'.
"""
import Autodesk.Revit.DB as DB

import link_versions as lv

ERROR, WARNING, INFO = 'error', 'warning', 'info'
DEFAULT_WORKSETS = ('Workset1', 'Shared Levels and Grids')


def _name(e):
    try:
        return DB.Element.Name.GetValue(e)
    except Exception:
        return getattr(e, 'Name', '') or ''


def _type_name(t):
    p = t.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
    return p.AsString() if p is not None else _name(t)


def _xf_close(a, b, tol=1e-4):
    return (a.Origin.DistanceTo(b.Origin) < tol and a.BasisX.DistanceTo(b.BasisX) < 1e-6
            and a.BasisY.DistanceTo(b.BasisY) < 1e-6 and a.BasisZ.DistanceTo(b.BasisZ) < 1e-6)


def _shared_ok(doc, inst):
    """True when the link instance sits where shared coordinates put it, False when
    not, None when it cannot be told (link not loaded).
    ProjectLocation.GetTotalTransform maps shared -> internal coordinates, so a link
    placed by shared coordinates has transform host_S * link_S^-1."""
    ld = inst.GetLinkDocument()
    if ld is None:
        return None
    try:
        host_s = doc.ActiveProjectLocation.GetTotalTransform()
        link_s = ld.ActiveProjectLocation.GetTotalTransform()
        return _xf_close(inst.GetTotalTransform(), host_s.Multiply(link_s.Inverse), 1e-3)
    except Exception:
        return None


def host_notes(doc):
    """Model-level findings."""
    out = []
    try:
        if doc.ActiveProjectLocation.GetTotalTransform().IsIdentity:
            out.append((WARNING, u'This model has no shared coordinates (they equal its internal origin): '
                                 u'links whose shared coordinates differ can only be placed origin to origin.'))
    except Exception:
        pass
    return out


class _Worksets(object):
    """Workset findings: default worksets, and worksets shared by links and model."""

    def __init__(self, doc, link_instances):
        self.doc = doc
        self.on = doc.IsWorkshared
        self.links_per_ws = {}
        if self.on:
            for i in link_instances:
                self.links_per_ws.setdefault(i.WorksetId.IntegerValue if hasattr(i.WorksetId, 'IntegerValue')
                                             else i.WorksetId, []).append(i)
        self._others = {}

    def _key(self, wid):
        return wid.IntegerValue if hasattr(wid, 'IntegerValue') else wid

    def check(self, inst):
        if not self.on:
            return []
        wid = inst.WorksetId
        ws = self.doc.GetWorksetTable().GetWorkset(wid)
        out = [(INFO, u'Workset: %s' % ws.Name)]
        if ws.Name in DEFAULT_WORKSETS:
            out.append((WARNING, u'On the default workset "%s" (give links their own workset)' % ws.Name))
            return out
        k = self._key(wid)
        if k not in self._others:
            total = (DB.FilteredElementCollector(self.doc).WherePasses(DB.ElementWorksetFilter(wid))
                     .WhereElementIsNotElementType().GetElementCount())
            self._others[k] = total - len(self.links_per_ws.get(k, []))
        if self._others[k] > 0:
            out.append((WARNING, u'Workset "%s" also holds %d model element(s)' % (ws.Name, self._others[k])))
        return out


def check(doc):
    """[{'name', 'kind', 'status', 'findings': [(severity, text)]}] for every link."""
    try:
        versions = dict((i['name'], i) for i in lv.collect(doc))
    except Exception:
        versions = {}
    rvt_inst = list(DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance))
    cad_inst = list(DB.FilteredElementCollector(doc).OfClass(DB.ImportInstance))
    wsets = _Worksets(doc, rvt_inst + cad_inst)
    results = []

    # ---- Revit links
    for lt in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkType):
        name = _type_name(lt)
        f = []
        insts = [i for i in rvt_inst if i.GetTypeId() == lt.Id]
        status = 'Loaded' if DB.RevitLinkType.IsLoaded(doc, lt.Id) else 'Not loaded'
        try:
            efr = lt.GetExternalFileReference()
            fs = efr.GetLinkedFileStatus()
            if fs == DB.LinkedFileStatus.NotFound:
                status = 'Not found'
            elif fs == DB.LinkedFileStatus.Unloaded:
                status = 'Unloaded'
            f.append((INFO, u'Path: %s' % efr.PathType))
        except Exception:
            pass
        if status != 'Loaded':
            f.append((ERROR, u'Link is %s' % status.lower()))
        if lt.IsNestedLink:
            f.append((INFO, u'Nested link (comes from another link)'))
        elif lt.AttachmentType == DB.AttachmentType.Attachment:
            f.append((INFO, u'Attachment: its links come along when this model is linked (Overlay is usual)'))
        v = versions.get(name)
        if v is not None:
            f.append((INFO, u'Version %s · %s' % (lv.fmt_version(v['version']), lv.fmt_date(v['date']))))
            if v['note']:
                f.append((WARNING, v['note']))
        if not lt.IsNestedLink:
            if not insts:
                f.append((WARNING, u'Loaded but not placed (no instance in the model)'))
            elif len(insts) > 1:
                f.append((WARNING, u'%d instances placed' % len(insts)))
            for i in insts:
                tag = u' (%s)' % i.Name.split(' : ')[-1] if len(insts) > 1 else u''
                if not i.Pinned:
                    f.append((WARNING, u'Not pinned%s' % tag))
                ok = _shared_ok(doc, i)
                if ok is False:
                    f.append((WARNING, u'Shared coordinates do not match this model%s' % tag))
                f.extend(wsets.check(i))
        results.append({'name': name, 'kind': 'RVT', 'status': status, 'findings': f,
                        'ids': [i.Id for i in insts] or [lt.Id]})

    # ---- CAD links and imports (grouped by type)
    by_type = {}
    for i in cad_inst:
        by_type.setdefault(i.GetTypeId().ToString(), []).append(i)
    cad_types = dict((t.Id.ToString(), t) for t in DB.FilteredElementCollector(doc).OfClass(DB.CADLinkType))
    for tid in set(by_type) | set(cad_types):
        insts = by_type.get(tid, [])
        t = cad_types.get(tid) or (doc.GetElement(insts[0].GetTypeId()) if insts else None)
        if t is None:
            continue
        name = _type_name(t)
        f = []
        linked = any(i.IsLinked for i in insts) or (not insts and isinstance(t, DB.CADLinkType))
        status = 'Linked' if linked else 'Imported'
        if insts and not linked:
            f.append((ERROR, u'Imported, not linked: it will not update (link it instead)'))
        if linked:
            try:
                if t.IsExternalFileReference():
                    fs = t.GetExternalFileReference().GetLinkedFileStatus()
                    if fs != DB.LinkedFileStatus.Loaded:
                        status = str(fs)
                        f.append((ERROR, u'Link status: %s' % fs))
            except Exception:
                pass
            v = versions.get(name)
            if v is not None:
                f.append((INFO, u'Version %s · %s' % (lv.fmt_version(v['version']), lv.fmt_date(v['date']))))
                if v['note']:
                    f.append((WARNING, v['note']))
        if not insts:
            f.append((WARNING, u'Loaded but not placed (no instance in the model)'))
        elif len(insts) > 1:
            f.append((WARNING, u'%d instances placed' % len(insts)))
        for i in insts:
            if i.ViewSpecific:
                ov = doc.GetElement(i.OwnerViewId)
                f.append((INFO, u'Current view only: %s' % (_name(ov) if ov else '?')))
            if not i.Pinned:
                f.append((WARNING, u'Not pinned'))
            f.extend(wsets.check(i))
        results.append({'name': name, 'kind': 'CAD', 'status': status, 'findings': f,
                        'ids': [i.Id for i in insts] or [t.Id]})

    results.sort(key=lambda r: (r['kind'] != 'RVT', r['name'].lower()))
    return results


def worst(findings):
    sev = [s for s, _ in findings]
    return ERROR if ERROR in sev else WARNING if WARNING in sev else 'ok'
