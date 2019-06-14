import os
import re
from pathlib import Path
from . import TEMPLATE_PATH
from . import logger as logging
from .tools import shell
from .version import __version__
from .depends import suggest_name, search_python_deps
from .entrypoints import run_install_scripts

logger = logging.getLogger(__name__)

COPYRIGHT_RE = re.compile(
    r'(?:copyrights?|\s*©|\s*\(c\))[\s:|,]*'
    r'((?=.*[a-z])\d{2,4}(?:(?!all\srights).)+)', re.IGNORECASE)

DPKG_SHLIBS_RE = re.compile(r'find library (.+\.so[.\d]*) needed')

APT_FILE_RE = re.compile(r'(.*lib.+):\s(?:/usr/lib/|/lib/)')

EXTENDED_DESC = 'This package was generated by wheel2deb.py v'+__version__

DEBIAN_VERS_ARCH = {
    'manylinux1_x86_64': 'amd64',
    'manylinux1_i686': 'i686',
    'linux_armv7l': 'armhf',
    'linux_armv6l': 'armhf',
    'linux_x86_64': 'amd64',
    'linux_i686': 'amd64',
    'any': 'all'
}


class SourcePackage:
    """
    Create a debian source package that can be built
    with dpkg-buildpackage from a python wheel
    """
    def __init__(self, ctx, wheel, extras=None):
        self.wheel = wheel
        self.ctx = ctx
        self.pyvers = ctx.python_version

        # root directory of the debian source package
        self.root = wheel.extract_path.parent
        # relative path to wheel.extract_path from self.root
        # contains the files extracted from the wheel
        self.src = Path(wheel.extract_path.name)
        # debian directory path
        # holds the package config files
        self.debian = self.root / 'debian'

        # debian package name
        self.name = suggest_name(ctx, wheel.name)

        # debian package version
        self.version = '%s-%s~w2d%s' % (wheel.version, ctx.revision,
                                        __version__.split('.')[0])
        if ctx.epoch:
            self.version = '%s:%s' % (ctx.epoch, self.version)

        # debian package homepage
        self.homepage = wheel.metadata.home_page
        # debian package description
        self.description = wheel.metadata.summary
        # debian package extended description
        self.extended_desc = EXTENDED_DESC
        # upstream license
        self.license = wheel.metadata.license or 'custom'

        # debian package architecture
        if wheel.arch_tag in DEBIAN_VERS_ARCH:
            self.arch = DEBIAN_VERS_ARCH[wheel.arch_tag]
        else:
            logger.error('unknown platform tag, assuming arch=all')
            self.arch = 'all'

        # debian package full filename
        self.filename = '%s_%s_%s.deb' % (self.name, self.version, self.arch)

        self.interpreter = 'python' if self.pyvers.major == 2 else 'python3'

        # compute package run dependencies
        self.depends = ['%s:any' % self.interpreter]
        if wheel.version_range(self.pyvers):
            vrange = wheel.version_range(self.pyvers)
            if vrange.max:
                self.depends.append(
                    '%s (<< %s)' % (self.interpreter, vrange.max))
            if vrange.min:
                self.depends.append(
                    '%s (>= %s~)' % (self.interpreter, vrange.min))

        deps, missing = search_python_deps(ctx, wheel, extras)
        self.depends.extend(deps)
        self.depends.extend(ctx.depends)

        self.build_deps = {'debhelper'}

        # write unsatisfied requirements in missing.txt
        with open(str(self.root / 'missing.txt'), 'w') as f:
            f.write('\n'.join(missing)+'\n')

        # wheel modules install path
        if self.pyvers.major == 2:
            self.install_path = '/usr/lib/python2.7/dist-packages/'
        else:
            self.install_path = '/usr/lib/python3/dist-packages/'

    def control(self):
        """
        Generate debian/control
        """
        self.dump_tpl('control.j2', self.debian / 'control')

    def compat(self):
        """
        Generate debian/compat
        """
        with (self.debian / 'compat').open(mode='w') as f:
            f.write('9')

    def changelog(self):
        """
        Generate debian/changelog
        """
        self.dump_tpl('changelog.j2', self.debian / 'changelog')

    def install(self):
        """
        Generate debian/install
        """
        install = set()

        for d in os.listdir(str(self.wheel.extract_path)):
            if not d.endswith('.data'):
                install.add(str(self.src / d) + ' ' + self.install_path)
            else:
                purelib = self.wheel.extract_path / d / 'purelib'
                if purelib.exists():
                    install.add(str(self.src / d / 'purelib' / '*')
                                + ' ' + self.install_path)

        if self.wheel.entrypoints and not self.ctx.ignore_entry_points:
            run_install_scripts(self.wheel, self.pyvers, self.root)
            install.add('entrypoints/* /usr/bin/')

        for script in self.wheel.record.scripts:
            install.add(str(self.src / script) + ' /usr/bin/')

        with (self.debian / 'install').open('w') as f:
            f.write('\n'.join(install))

    def rules(self):
        """
        Generate debian/rules
        """
        self.dump_tpl(
            'rules.j2',
            file=self.debian / 'rules',
            shlibdeps_params=''.join(
                [' -l' + str(self.src / x)
                 for x in self.wheel.record.lib_dirs]))

    def copyright(self):
        """
        Generate debian/copyright
        https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
        """
        licenses = self.wheel.record.licenses
        license_file = None
        license_content = ''
        copyrights = set()

        if not licenses:
            logger.warning('no license found !')
            return

        # gather copyrights from all licenses
        for lic in licenses:
            content = (self.wheel.extract_path / lic).read_text()
            copyrights.update(set(re.findall(COPYRIGHT_RE, content)))

        copyrights = sorted(list(copyrights))

        logger.debug('found the following copyrights: %s', copyrights)

        for file in licenses:
            if 'dist-info' in file:
                license_file = file
        if not license_file:
            license_file = licenses[0]

        with (self.wheel.extract_path / license_file).open() as f:
            for line in f.readlines():
                license_content += ' ' + line

        if license_content:
            file = self.debian / 'copyright'
            self.dump_tpl('copyright.j2', file,
                          license=self.license,
                          license_content=license_content,
                          copyrights=copyrights)
        else:
            logger.warning('license not found !')

        # FIXME: licenses should not be copied by the install script

    def postinst(self):
        """
        Generate debian/postinst script
        """
        self.dump_tpl('postinst.j2',
                      file=self.debian / 'postinst')

    def prerm(self):
        """
        Generate debian/prerm script
        """
        self.dump_tpl('prerm.j2', file=self.debian / 'prerm')

    def create(self):

        if not self.debian.exists():
            self.debian.mkdir(parents=True)

        self.fix_shebangs()
        self.control()
        self.compat()
        self.changelog()
        self.install()
        self.rules()
        self.copyright()
        self.postinst()
        self.prerm()

        # dpkg-shlibdeps won't work without debian/control
        self.search_shlibs_deps()
        self.control()

    def dump_tpl(self, tpl_name, file, **kwargs):
        from jinja2 import Template
        tpl = (Path(TEMPLATE_PATH) / tpl_name).read_text()
        Template(tpl) \
            .stream(package=self, ctx=self.ctx, **kwargs) \
            .dump(str(file))

    def fix_shebangs(self):
        files = [self.wheel.extract_path / x
                 for x in self.wheel.record.scripts]
        for file in files:
            content = file.read_text()
            shebang = '#!/usr/bin/env python%s' % \
                      self.ctx.python_version.major
            if not content.startswith(shebang):
                content = content.split('\n')
                content[0] = shebang
                content = '\n'.join(content)
                with file.open('w') as g:
                    g.write(content)

    def search_shlibs_deps(self):
        """
        Search packages providing shared libs dependencies
        :return: List of packages providing those libs
        """
        shlibdeps = set()
        missing_libs = set()

        def parse_substvars():
            if (self.debian / 'substvars').is_file():
                subsvars = (self.debian / 'substvars').read_text()[15:]
                m = re.findall(r'([^=\s,()]+)\s?(?:\([^)]+\))?', subsvars)
                shlibdeps.update(m)

        # dpkg-shlibdeps may have already successfully been run
        parse_substvars()

        if self.wheel.record.lib_dirs and not shlibdeps:
            args = ['dpkg-shlibdeps'] \
                   + ['-l'+str(self.src/x)
                      for x in self.wheel.record.lib_dirs] \
                   + [str(self.src/x) for x in self.wheel.record.libs]
            output = shell(args, cwd=self.root)[0]
            missing_libs.update(DPKG_SHLIBS_RE.findall(output, re.MULTILINE))

            parse_substvars()

        if missing_libs:
            logger.info('dpkg-shlibdeps reported the following missing '
                        'shared libs dependencies: %s', missing_libs)

            # search packages providing those libs
            for lib in missing_libs:
                output = shell(['apt-file', 'search', lib])[0]
                packages = set(APT_FILE_RE.findall(output))

                # remove dbg packages
                packages = [p for p in packages if p[-3:] != 'dbg']

                if not len(packages):
                    logger.warning("did not find a package providing %s", lib)
                else:
                    # we pick the package with the shortest name
                    packages = sorted(packages, key=len)
                    shlibdeps.add(packages[0])

                if len(packages) > 1:
                    logger.warning(
                        'several packages providing %s: %s, picking %s, '
                        'edit debian/control to use another one.',
                        lib, packages, packages[0])

            if shlibdeps:
                logger.info("detected dependencies: %s", shlibdeps)

        self.build_deps.update({p+':'+self.arch for p in shlibdeps})
