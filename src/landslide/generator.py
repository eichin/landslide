# -*- coding: utf-8 -*-

#  Copyright 2010 Adam Zapletal
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import os
import re
import codecs
import inspect
import jinja2
import tempfile
import utils
import ConfigParser

from subprocess import Popen

import macro as macro_module
from parser import Parser


BASE_DIR = os.path.dirname(__file__)
THEMES_DIR = os.path.join(BASE_DIR, 'themes')
TOC_MAX_LEVEL = 2


class Generator(object):
    """The Generator class takes and processes presentation source as a file, a
       folder or a configuration file and provides methods to render them as a
       presentation.
    """
    DEFAULT_DESTINATION = 'presentation.html'
    default_macros = [
        macro_module.CodeHighlightingMacro,
        macro_module.EmbedImagesMacro,
        macro_module.FixImagePathsMacro,
        macro_module.FxMacro,
        macro_module.NotesMacro,
        macro_module.QRMacro,
    ]
    user_css = []
    user_js = []

    def __init__(self, source, **kwargs):
        """ Configures the generator. Available ``args`` are:
            - ``source``: source file or directory
            Available ``kwargs`` are:
            - ``destination_file``: path to html or PDF destination file
            - ``theme``: path to the them to use for this presentation
            - ``direct``: enables direct rendering presentation to stdout
            - ``debug``: enables debug mode
            - ``verbose``: enables verbose output
            - ``embed``: generates a standalone document, with embedded assets
            - ``encoding``: the encoding to use for this presentation
            - ``logger``: a logger lambda to use for logging
            - ``extensions``: Comma separated list of markdown extensions
        """
        self.debug = kwargs.get('debug', False)
        self.destination_file = kwargs.get('direct', 'presentation.html')
        self.direct = kwargs.get('direct', False)
        self.embed = kwargs.get('embed', False)
        self.encoding = kwargs.get('encoding', 'utf8')
        self.extensions = kwargs.get('extensions', None)
        self.logger = kwargs.get('logger', None)
        self.theme = kwargs.get('theme', 'default')
        self.verbose = kwargs.get('verbose', False)
        self.num_slides = 0
        self.__toc = []

        # macros registering
        self.macros = []
        self.register_macro(*self.default_macros)

        if self.direct:
            self.verbose = True

        if not source or not os.path.exists(source):
            raise IOError(u"Source file/directory %s does not exist"
                          % source)

        self.source_base_dir = os.path.split(os.path.abspath(source))[0]
        if source.endswith('.cfg'):
            config = self.parse_config(source)
            self.source = config.get('source')
            if not self.source:
                raise IOError('unable to fetch a valid source from config')
            self.theme = config.get('theme', 'default')
            self.destination_file = config.get('destination',
                self.DEFAULT_DESTINATION)
            self.embed = config.get('embed', False)
            self.add_user_css(config.get('css', []))
            self.add_user_js(config.get('js', []))
        else:
            self.source = source

        if (os.path.exists(self.destination_file)
            and not os.path.isfile(self.destination_file)):
            raise IOError(u"Destination %s exists and is not a file"
                          % self.destination_file)

        if self.destination_file.endswith('.html'):
            self.file_type = 'html'
        elif self.destination_file.endswith('.pdf'):
            self.file_type = 'pdf'
            self.embed = True
        else:
            raise IOError(u"This program can only write html or pdf files. "
                           "Please use one of these file extensions in the "
                           "destination")

        self.theme_dir = self.find_theme_dir(self.theme)
        self.template_file = self.get_template_file()

    def add_user_css(self, css_list):
        """ Adds supplementary user css files to the presentation.
        """
        for css_path in css_list:
            if css_path and not css_path in self.user_css:
                if not os.path.exists(css_path):
                    raise IOError('%s user css file not found' % (css_path,))
                self.user_css.append({
                    'path_url': utils.get_abs_path_url(css_path),
                    'contents': open(css_path).read(),
                })

    def add_user_js(self, js_list):
        """ Adds supplementary user javascript files to the presentation.
        """
        for js_path in js_list:
            if js_path and not js_path in self.user_js:
                if not os.path.exists(js_path):
                    raise IOError('%s user js file not found' % (js_path,))
                self.user_js.append({
                    'path_url': utils.get_abs_path_url(js_path),
                    'contents': open(js_path).read(),
                })

    def add_toc_entry(self, title, level, slide_number):
        """ Adds a new entry to current presentation Table of Contents.
        """
        self.__toc.append({'title': title, 'number': slide_number,
                           'level': level})

    def get_toc(self):
        """ Smart getter for Table of Content list.
        """
        toc = []
        stack = [toc]
        for entry in self.__toc:
            entry['sub'] = []
            while entry['level'] < len(stack):
                stack.pop()
            while entry['level'] > len(stack):
                stack.append(stack[-1][-1]['sub'])
            stack[-1].append(entry)
        return toc

    def set_toc(self, value):
        raise ValueError("toc is read-only")

    toc = property(get_toc, set_toc)

    def execute(self):
        """ Execute this generator regarding its current configuration.
        """
        if self.direct:
            if self.file_type == 'pdf':
                raise IOError(u"Direct output mode is not available for PDF "
                               "export")
            else:
                print self.render()
        else:
            self.write()
            self.log(u"Generated file: %s" % self.destination_file)

    def get_template_file(self):
        """ Retrieves Jinja2 template file path.
        """
        if os.path.exists(os.path.join(self.theme_dir, 'base.html')):
            return os.path.join(self.theme_dir, 'base.html')
        default_dir = os.path.join(THEMES_DIR, 'default')
        if not os.path.exists(os.path.join(default_dir, 'base.html')):
            raise IOError(u"Cannot find base.html in default theme")
        return os.path.join(default_dir, 'base.html')

    def fetch_contents(self, source):
        """ Recursively fetches Markdown contents from a single file or
            directory containing itself Markdown files.
        """
        slides = []

        if type(source) is list:
            for entry in source:
                slides.extend(self.fetch_contents(entry))
        elif os.path.isdir(source):
            self.log(u"Entering %s" % source)
            entries = os.listdir(source)
            entries.sort()
            for entry in entries:
                slides.extend(self.fetch_contents(os.path.join(source, entry)))
        else:
            try:
                parser = Parser(os.path.splitext(source)[1], self.encoding,
                    self.extensions)
            except NotImplementedError:
                return slides

            self.log(u"Adding   %s (%s)" % (source, parser.format))

            try:
                file = codecs.open(source, encoding=self.encoding)
                file_contents = file.read()
            except UnicodeDecodeError:
                self.log(u"Unable to decode source %s: skipping" % source,
                         'warning')
            else:
                inner_slides = re.split(r'<hr.+>', parser.parse(file_contents))
                for inner_slide in inner_slides:
                    slides.append(self.get_slide_vars(inner_slide, source))

        if not slides:
            self.log(u"Exiting  %s: no contents found" % source, 'notice')

        return slides

    def find_theme_dir(self, theme):
        """ Finds them dir path from its name.
        """
        if os.path.exists(theme):
            self.theme_dir = theme
        elif os.path.exists(os.path.join(THEMES_DIR, theme)):
            self.theme_dir = os.path.join(THEMES_DIR, theme)
        else:
            raise IOError(u"Theme %s not found or invalid" % theme)
        return self.theme_dir

    def get_css(self):
        """ Fetches and returns stylesheet file path or contents, for both
            print and screen contexts, depending if we want a standalone
            presentation or not.
        """
        css = {}

        print_css = os.path.join(self.theme_dir, 'css', 'print.css')
        if not os.path.exists(print_css):
            # Fall back to default theme
            print_css = os.path.join(THEMES_DIR, 'default', 'css', 'print.css')

            if not os.path.exists(print_css):
                raise IOError(u"Cannot find css/print.css in default theme")

        css['print'] = {'path_url': utils.get_abs_path_url(print_css),
                        'contents': open(print_css).read()}

        screen_css = os.path.join(self.theme_dir, 'css', 'screen.css')
        if (os.path.exists(screen_css)):
            css['screen'] = {'path_url': utils.get_abs_path_url(screen_css),
                             'contents': open(screen_css).read()}
        else:
            self.log(u"No screen stylesheet provided in current theme",
                      'warning')

        return css

    def get_js(self):
        """ Fetches and returns javascript file path or contents, depending if
            we want a standalone presentation or not.
        """
        js_file = os.path.join(self.theme_dir, 'js', 'slides.js')

        if not os.path.exists(js_file):
            js_file = os.path.join(THEMES_DIR, 'default', 'js', 'slides.js')

            if not os.path.exists(js_file):
                raise IOError(u"Cannot find slides.js in default theme")

        return {'path_url': utils.get_abs_path_url(js_file),
                'contents': open(js_file).read()}

    def get_slide_vars(self, slide_src, source=None):
        """ Computes a single slide template vars from its html source code.
            Also extracts slide informations for the table of contents.
        """
        find = re.search(r'(<h(\d+?).*?>(.+?)</h\d>)\s?(.+)?', slide_src,
                         re.DOTALL | re.UNICODE)
        if not find:
            header = level = title = None
            content = slide_src.strip()
        else:
            header = find.group(1)
            level = int(find.group(2))
            title = find.group(3)
            content = find.group(4).strip() if find.group(4) else find.group(4)

        slide_classes = []

        if content:
            content, slide_classes = self.process_macros(content, source)

        source_dict = {}

        if source:
            source_dict = {'rel_path': source,
                           'abs_path': os.path.abspath(source)}

        if header or content:
            return {'header': header, 'title': title, 'level': level,
                    'content': content, 'classes': slide_classes,
                    'source': source_dict}

    def get_template_vars(self, slides):
        """ Computes template vars from slides html source code.
        """
        try:
            head_title = slides[0]['title']
        except (IndexError, TypeError):
            head_title = "Untitled Presentation"

        for slide_index, slide_vars in enumerate(slides):
            if not slide_vars:
                continue
            self.num_slides += 1
            slide_number = slide_vars['number'] = self.num_slides
            if slide_vars['level'] and slide_vars['level'] <= TOC_MAX_LEVEL:
                self.add_toc_entry(slide_vars['title'], slide_vars['level'],
                                   slide_number)

        return {'head_title': head_title, 'num_slides': str(self.num_slides),
                'slides': slides, 'toc': self.toc, 'embed': self.embed,
                'css': self.get_css(), 'js': self.get_js(),
                'user_css': self.user_css, 'user_js': self.user_js}

    def log(self, message, type='notice'):
        """ Logs a message (eventually, override to do something more clever).
        """
        if self.logger and not callable(self.logger):
            raise ValueError(u"Invalid logger set, must be a callable")
        if self.verbose and self.logger:
            self.logger(message, type)

    def parse_config(self, config_source):
        """ Parses a landslide configuration file and returns a normalized
            python dict.
        """
        self.log(u"Config   %s" % config_source)
        try:
            raw_config = ConfigParser.RawConfigParser()
            raw_config.read(config_source)
        except Exception, e:
            raise RuntimeError(u"Invalid configuration file: %s" % e)
        config = {}
        config['source'] = raw_config.get('landslide', 'source')\
            .replace('\r', '').split('\n')
        if raw_config.has_option('landslide', 'theme'):
            config['theme'] = raw_config.get('landslide', 'theme')
            self.log(u"Using    configured theme %s" % config['theme'])
        if raw_config.has_option('landslide', 'destination'):
            config['destination'] = raw_config.get('landslide', 'destination')
        if raw_config.has_option('landslide', 'embed'):
            config['embed'] = raw_config.getboolean('landslide', 'embed')
        if raw_config.has_option('landslide', 'css'):
            config['css'] = raw_config.get('landslide', 'css')\
                .replace('\r', '').split('\n')
        if raw_config.has_option('landslide', 'js'):
            config['js'] = raw_config.get('landslide', 'js')\
                .replace('\r', '').split('\n')
        return config

    def process_macros(self, content, source=None):
        """ Processed all macros.
        """
        classes = []
        for macro_class in self.macros:
            try:
                macro = macro_class(logger=self.logger, embed=self.embed)
                content, add_classes = macro.process(content, source)
                if add_classes:
                    classes += add_classes
            except Exception, e:
                self.log(u"%s processing failed in %s: %s"
                         % (macro, source, e))
        return content, classes

    def register_macro(self, *macros):
        """ Registers macro classes passed a method arguments.
        """
        for m in macros:
            if (inspect.isclass(m) and issubclass(m, macro_module.Macro)):
                self.macros.append(m)
            else:
                raise TypeError("Coundn't register macro; a macro must inherit"
                                " from macro.Macro")

    def render(self):
        """ Returns generated html code.
        """
        template_src = codecs.open(self.template_file, encoding=self.encoding)
        template = jinja2.Template(template_src.read())
        slides = self.fetch_contents(self.source)
        return template.render(self.get_template_vars(slides))

    def write(self):
        """ Writes generated presentation code into the destination file.
        """
        html = self.render()

        if self.file_type == 'pdf':
            self.write_pdf(html)
        else:
            outfile = codecs.open(self.destination_file, 'w',
                                  encoding='utf_8')
            outfile.write(html)

    def write_pdf(self, html):
        """ Tries to write a PDF export from the command line using PrinceXML
            if available.
        """
        try:
            f = tempfile.NamedTemporaryFile(delete=False, suffix='.html')
            f.write(html.encode('utf_8', 'xmlcharrefreplace'))
            f.close()
        except Exception:
            raise IOError(u"Unable to create temporary file, aborting")

        dummy_fh = open(os.path.devnull, 'w')

        try:
            command = ["prince", f.name, self.destination_file]

            Popen(command, stderr=dummy_fh).communicate()
        except Exception:
            raise EnvironmentError(u"Unable to generate PDF file using "
                                    "prince. Is it installed and available?")
        finally:
            dummy_fh.close()
