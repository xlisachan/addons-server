import collections
import os
import shutil
import time
import zipfile

from django import forms
from django.conf import settings

import rdflib
from tower import ugettext as _

import amo
from applications.models import AppVersion


class WorkingZipFile(zipfile.ZipFile):
    def _extract_member(self, member, targetpath, pwd):
        """Extract the ZipInfo object 'member' to a physical
           file on the path targetpath.
        """
        # build the destination pathname, replacing
        # forward slashes to platform specific separators.
        if targetpath[-1:] in (os.path.sep, os.path.altsep):
            targetpath = targetpath[:-1]

        # don't include leading "/" from file name if present
        if member.filename[0] == '/':
            targetpath = os.path.join(targetpath, member.filename[1:])
        else:
            targetpath = os.path.join(targetpath, member.filename)

        targetpath = os.path.normpath(targetpath)

        # Create all upper directories if necessary.
        upperdirs = os.path.dirname(targetpath)
        if upperdirs and not os.path.exists(upperdirs):
            os.makedirs(upperdirs)

        if member.filename[-1] == '/':
            os.mkdir(targetpath)
            return targetpath

        source = self.open(member, pwd=pwd)
        target = file(targetpath, "wb")
        shutil.copyfileobj(source, target)
        source.close()
        target.close()

        return targetpath


class Extractor(object):
    """Extract add-on info from an install.rdf."""
    TYPES = {'2': amo.ADDON_EXTENSION, '4': amo.ADDON_THEME,
             '8': amo.ADDON_LPADDON}
    App = collections.namedtuple('App', 'appdata id min max')
    manifest = u'urn:mozilla:install-manifest'

    def __init__(self, install_rdf):
        self.rdf = rdflib.Graph().parse(open(install_rdf))
        self.find_root()
        self.data = {
            'guid': self.find('id'),
            'type': self.TYPES.get(self.find('type'), amo.ADDON_EXTENSION),
            'name': self.find('name'),
            'version': self.find('version'),
            'homepage': self.find('homepageURL'),
            'description': self.find('description'),
            'apps': self.apps(),
        }

    @classmethod
    def parse(cls, install_rdf):
        return cls(install_rdf).data

    def uri(self, name):
        namespace = 'http://www.mozilla.org/2004/em-rdf'
        return rdflib.term.URIRef('%s#%s' % (namespace, name))

    def find_root(self):
        # If the install-manifest root is well-defined, it'll show up when we
        # search for triples with it.  If not, we have to find the context that
        # defines the manifest and use that as our root.
        # http://www.w3.org/TR/rdf-concepts/#section-triples
        manifest = rdflib.term.URIRef(self.manifest)
        if list(self.rdf.triples((manifest, None, None))):
            self.root = manifest
        else:
            self.root = self.rdf.subjects(None, self.manifest).next()

    def find(self, name, ctx=None):
        # ctx is like the context in a jquery selector.
        if ctx is None:
            ctx = self.root
        # predicate is like the css selector; it maps to <em:{name}>.
        match = list(self.rdf.objects(ctx, predicate=self.uri(name)))
        # These come back as rdflib.Literal, which subclasses unicode.
        if match:
            return unicode(match[0])

    def apps(self):
        rv = []
        for ctx in self.rdf.objects(None, self.uri('targetApplication')):
            app = amo.APP_GUIDS.get(self.find('id', ctx))
            if not app:
                continue
            try:
                qs = AppVersion.objects.filter(application=app.id)
                min = qs.get(version=self.find('minVersion', ctx))
                max = qs.get(version=self.find('maxVersion', ctx))
            except AppVersion.DoesNotExist:
                continue
            rv.append(self.App(appdata=app, id=app.id, min=min, max=max))
        return rv


def parse_xpi(xpi, addon=None):
    """Extract and parse an XPI."""
    from addons.models import Addon
    # Extract to /tmp
    path = os.path.join(settings.TMP_PATH, str(time.time()))
    os.makedirs(path)

    # Validating that we have no member files that try to break out of
    # the destination path.  NOTE: This will be obsolete when this bug is
    # fixed: http://bugs.python.org/issue4710 (it was fixed in Python 2.6.2)
    zip = WorkingZipFile(xpi)

    for f in zip.namelist():
        if '..' in f or f.startswith('/'):
            raise forms.ValidationError(_('Invalid archive.'))

    zip.extractall(path)

    rdf = Extractor.parse(os.path.join(path, 'install.rdf'))

    if addon and addon.guid != rdf['guid']:
        raise forms.ValidationError(_("UUID doesn't match add-on"))
    if not addon and Addon.objects.filter(guid=rdf['guid']):
        raise forms.ValidationError(_('Duplicate UUID found.'))

    if addon and addon.type != rdf['type']:
        raise forms.ValidationError(
            _("<em:type> doesn't match add-on"))

    shutil.rmtree(path)
    return rdf
