"""
wpscan.py

Copyright 2017 jose nazario

This file is part of w3af, http://w3af.org/ .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
import json
import os
import urllib
import zipfile

from itertools import repeat, izip

import w3af.core.controllers.output_manager as om
import w3af.core.data.constants.severity as severity
import w3af.core.data.kb.knowledge_base as kb

from w3af import ROOT_PATH

from w3af.core.controllers.plugins.crawl_plugin import CrawlPlugin
from w3af.core.controllers.exceptions import RunOnce
from w3af.core.controllers.core_helpers.fingerprint_404 import is_404

from w3af.core.data.options.opt_factory import opt_factory
from w3af.core.data.options.option_list import OptionList
from w3af.core.data.options.option_types import BOOL
from w3af.core.data.fuzzer.utils import rand_alnum
from w3af.core.data.db.disk_set import DiskSet
from w3af.core.data.request.fuzzable_request import FuzzableRequest
from w3af.core.data.kb.info import Info
from w3af.core.data.kb.info_set import InfoSet
from w3af.core.data.kb.vuln import Vuln
from w3af.core.controllers.misc.decorators import runonce

class wpscan(CrawlPlugin):
    """
    Finds WordPress plugins by bruteforcing.
    
    :author: jose nazario (jose@monkey.org)
    """

    BASE_PATH = os.path.join(ROOT_PATH, 'plugins', 'crawl', 'wpscan')

    def __init__(self):
        CrawlPlugin.__init__(self)
        self._update_plugins = False
        self._plugin_list = {}
        # Internal variables
        self._exec = True
        self._already_tested = DiskSet(table_prefix='wpscan')

    @runonce(exc_class=RunOnce)
    def crawl(self, fuzzable_request):
        """
        Get the file and parse it.
        
        :param fuzzable_request: A fuzzable_request instance that contains
                               (among other things) the URL to test.
        """
        if self._update_plugins or not os.path.exists(os.path.join(self.BASE_PATH, 'plugins.json')):
            om.out.debug("Downloading the archive from wpscan and extracting plugins.json ...", )
            try:
                os.stat(BASE_PATH)
            except OSError:
                om.out.debug("%s doesn't exist, making ..." % BASE_PATH)
                os.mkdir(BASE_PATH)
                om.out.debug("done")
            with open('/tmp/data.zip', 'wb') as f:
                f.write(urllib.urlopen('https://github.com/wpscanteam/wpscan/raw/master/data.zip').read())
            _wpscan_data_zip = zipfile.ZipFile(open('/tmp/data.zip', 'rb'))
            _wpscan_data_zip.extract('data/plugins.json', os.path.join(self.BASE_PATH))
            os.rename(os.path.join(self.BASE_PATH, 'data', 'plugins.json'), 
                      os.path.join(self.BASE_PATH, 'plugins.json'))
            om.out.debug('done')
        self._plugin_list = json.load(open(os.path.join(self.BASE_PATH, 'plugins.json'), 'r'))
        if not self._exec:
            raise RunOnce()
        else:
            domain_path = fuzzable_request.get_url().get_domain_path()
            if domain_path not in self._already_tested:
                self._already_tested.add(domain_path)
                self._bruteforce_plugins(domain_path)

    def _dir_name_generator(self, base_path):
        """
        Simple generator that returns the names of the plugins to test.
        
        @yields: (A string with the directory,
                  a URL object with the dir name)
        """
        for directory_name in self._plugin_list.keys():
            directory_name = "wp-content/plugins/" + directory_name.strip()
            try:
                dir_url = base_path.url_join(directory_name + '/')
            except ValueError, ve:
                msg = 'The "%s" line at "%s" generated an ' \
                      'invalid URL: %s'
                om.out.debug(msg % (directory_name, 
                                    os.path.join(self.BASE_PATH, 'plugins.json'), 
                                    ve))
            else:
                yield directory_name, dir_url

    def _send_and_check(self, base_path, (directory_name, dir_url)):
        """
        Performs a GET and verifies that the response is a 200.
        
        :return: None, data is stored in self.output_queue
        """
        try:
            http_response = self._uri_opener.GET(dir_url, cache=False)
        except:
            pass
        else:
            if not http_response.get_code() == 200:
                return
            #
            #   Looking good, but lets see if this is a false positive
            #   or not...
            #
            dir_url = base_path.url_join(directory_name + rand_alnum(5) + '/')
            invalid_http_response = self._uri_opener.GET(dir_url,
                                                         cache=False)
            if is_404(invalid_http_response):
                #
                #    Good, the directory_name + rand_alnum(5) return a
                #    404, the original directory_name is not a false positive.
                #
                fr = FuzzableRequest.from_http_response(http_response)
                self.output_queue.put(fr)
                msg = ('wpscan plugin found "%s" at URL %s with HTTP response '
                       'code %s and Content-Length: %s.')
                plugin_name = directory_name.split('/')[-1]
                om.out.information(msg % (plugin_name,
                                          http_response.get_url(),
                                          http_response.get_code(),
                                          len(http_response.get_body())))
                plugin_info = self._plugin_list.get(plugin_name, {"vulnerabilities": []})
                #
                #   if it has known vulns, we want to mark that. maybe it's interesting.
                #
                if len(plugin_info["vulnerabilities"]) > 0:
                    desc = ('The WordPress plugin "%s" was found at: %s. ')
                    desc %= (plugin_name, http_response.get_url())
                    v = Vuln('WordPress plugin found', desc, severity.MEDIUM,
                             http_response.id, self.get_name())
                    v.set_url(http_response.get_url())
                    kb.kb.append(self, 'wpscan', v)
                    om.out.vulnerability(v.get_desc(), severity=v.get_severity())
                else:
                    desc = 'Found plugin: "%s"' % plugin_name
                    i = Info('WordPress plugin', desc, http_response.id,
                             self.get_name())
                    i.set_uri(http_response.get_uri())
                    i['content'] = plugin_name
                    i['where'] = http_response.get_url()
                    self.kb_append_uniq_group(self, 'wordpress-plugin', i,
                                              group_klass=WordpressPluginInfoSet)

    def _bruteforce_plugins(self, base_path):
        """
        :param base_path: The base path to use in the bruteforcing process,
                          can be something like http://host.tld/ or
                          http://host.tld/images/ .
        
        :return: None, the data is stored in self.output_queue
        """
        dir_name_generator = self._dir_name_generator(base_path)
        base_path_repeater = repeat(base_path)
        arg_iter = izip(base_path_repeater, dir_name_generator)
        self.worker_pool.map_multi_args(self._send_and_check, arg_iter,
                                        chunksize=20)

    def end(self):
        self._already_tested.cleanup()

    def get_options(self):
        """
        :return: A list of option objects for this plugin.
        """
        ol = OptionList()
        d = 'Update the WPScan plugins database.'
        o = opt_factory('update_plugins', self._update_plugins, d, BOOL)
        ol.add(o)
        return ol

    def set_options(self, option_list):
        """
        This method sets all the options that are configured using the user interface
        generated by the framework using the result of get_options().
        
        :param OptionList: A dictionary with the options for the plugin.
        
        :return: No value is returned.
        """
        self._update_plugins = option_list['update_plugins'].get_value()

    def get_long_desc(self):
        """
        :return: A DETAILED description of the plugin functions and features.
        """

        return """
        This plugin finds WordPress plugins.
        While it is not possible to fingerprint the plugin version automatically,
        if the plugin contains known vulnerabilities they are listed as
        vulnerabilities in the resulting table, otherwise they are informational
        findings.
        """

class WordpressPluginInfoSet(InfoSet):
    ITAG = 'wordpress_plugin'
    TEMPLATE = (
        'The application has a WordPress plugin {{ content }} located'
        ' at "{{ where }}" which looks interesting and should be manually'
        ' reviewed.'
    )
