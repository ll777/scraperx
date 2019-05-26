import inspect
import logging
import datetime
import requests
from abc import ABC, abstractmethod

from .. import config
from ..write import Write
from ..proxies import get_proxy
from ..user_agent import get_user_agent
from ..trigger import run_task
from ..utils import DownloadValueError

logger = logging.getLogger(__name__)


class BaseDownload(ABC):

    def __init__(self, task, headers=None, proxy=None, ignore_codes=(),
                 extract_cls=None):
        # General task and config setup
        self._scraper = inspect.getmodule(self)
        self.extract_cls = extract_cls  # only needed if running locally
        self.task = task
        self._ignore_codes = ignore_codes

        # Set timestamps
        self.time_downloaded = datetime.datetime.utcnow()
        self.date_downloaded = datetime.datetime.utcnow().date()

        logger.info("Start Download",
                    extra={'task': self.task,
                           'scraper_name': config['SCRAPER_NAME'],
                           'time_started': str(self.time_downloaded),
                           })

        self._manifest = {'source_files': [],
                          'time_downloaded': str(self.time_downloaded),
                          'date_downloaded': str(self.date_downloaded),
                          }

        # Set up a requests session
        self.session = requests.Session()

        self._init_headers(headers)
        self._init_proxy(proxy)
        self._init_http_methods()

    @abstractmethod
    def download(self):
        """User created download function

        Returns:
            list|str -- Either a list or a single downloaded file

        Decorators:
            abstractmethod
        """
        pass

    def _get_proxy(self, country=None):
        """Get a proxy to use

        Use the scrapers fn, otherwise use the default

        Keyword Arguments:
            alpha2 {str} -- 2 letter country code (default: {None})
            platform {str} -- The name of the platform. Ex: Google, Macys, etc
                              (default: {None})

        Returns:
            str -- proxy string
        """
        try:
            return self.get_proxy(country=country)
        except AttributeError:
            return get_proxy(country=country)

    def _get_user_agent(self, device_type):
        """Get a User Agent

        Use the scrapers fn, otherwise use the default

        Arguments:
            device_type {str} -- The device the user agent should be for.
                                 Ex: desktop, mobile

        Returns:
            str -- User Agent string
        """
        try:
            return self.get_user_agent(device_type)
        except AttributeError:
            return get_user_agent(device_type)

    def run(self, standalone=False):
        """Start the download process

        Keyword Arguments:
            standalone {bool} -- Do not trigger the extractor if True
                                 (default: {False})
        """
        try:
            source_files = self.download()
            if not isinstance(source_files, (list, tuple)):
                # Make sure source files is a list
                source_files = [source_files]
            self._manifest['source_files'].extend(source_files)

        except requests.exceptions.HTTPError:
            # The status code was logged during the request, no need to repeat
            pass
        except DownloadValueError:
            # The status code was logged during the request, no need to repeat
            pass
        except Exception:
            logger.exception("Download Exception",
                             extra={'task': self.task,
                                    'scraper_name': config['SCRAPER_NAME']})
        else:
            if source_files:
                self._save_metadata()
                run_task(self.task,
                         task_cls=self.extract_cls,
                         download_manifest=self._manifest)
            else:
                # If it got here and there is not saved file then thats an issue
                logger.error("No source file saved",
                             extra={'task': self.task,
                                    'scraper_name': config['SCRAPER_NAME'],
                                    'manifest': self._manifest,
                                    })

        logger.info('Download finished',
                    extra={'task': self.task,
                           'scraper_name': config['SCRAPER_NAME'],
                           'time_finished': str(datetime.datetime.utcnow()),
                           })

    def _save_metadata(self):
        """Save the metadata with the download source

        Saves a file as the same name as the source with '.metadata.json'
        appended to the name
        """
        metadata = self._get_metadata()
        metadata_file = Write(metadata).write_json_lines()
        filename = metadata['download_manifest']['source_files'][0]['path']
        logger.info("Saving metadata file",
                    extra={'task': self.task,
                           'scraper_name': config['SCRAPER_NAME']})
        metadata_file.save(self, filename=filename + '_metadata.json')

    def _get_metadata(self):
        """Create the metadata dict

        Arguments:
            download_manifest {dict} -- The downloads manifest

        Returns:
            {dict} -- metadata
        """
        metadata = {'task': self.task,
                    'scraper': config['SCRAPER_NAME'],
                    'download_manifest': self._manifest,
                    }
        return metadata

    def _format_proxy(self, proxy):
        """Convert the proxy string into a dict the way requests likes it

        Arguments:
            proxy {str} -- Proxy string

        Returns:
            dict -- Format that requests wants proxies in
        """
        logger.debug(f"Setting proxy {proxy}",
                     extra={'task': self.task,
                            'scraper_name': config['SCRAPER_NAME']})
        if isinstance(proxy, dict) and 'http' in proxy and 'https' in proxy:
            # Nothing more to do
            return proxy

        return {'http': proxy,
                'https': proxy
                }

    def _init_headers(self, headers):
        """Set up the default session headers

        If no user agent is set then a default one is set

        Arguments:
            headers {dict} -- Headers passed in to the __init__
        """
        # Set headers from init, then update with task headers
        self.session.headers = {} if headers is None else headers
        self.session.headers.update(self.task.get('headers', {}))
        # Set a UA if the scraper did not set one
        if 'user-agent' not in map(str.lower, self.session.headers.keys()):
            self._set_session_ua()

    def _init_proxy(self, proxy):
        """Set the default session proxy

        If no proxy is passed in to __init__ or in the task data,
        then set one using the task `proxy_country` key.
        If they are not set then a random proxy will be choosen

        Arguments:
            proxy {str} -- Proxy passed in to the __init__
        """
        proxy_str = proxy
        if self.task.get('proxy') is not None:
            proxy_str = self.task.get('proxy')
        # If no proxy has been passed in, try and set one
        if not proxy_str:
            proxy_str = self._get_proxy(country=self.task.get('proxy_country'))
        self.session.proxies = self._format_proxy(proxy_str)

    def _init_http_methods(self):
        """Generate functions for each http method

        Makes it simpler to use
        """
        # Create http methods
        self.request_get = self._set_http_method('GET')
        self.request_post = self._set_http_method('POST')
        # Not sure if these are needed, but it doesn't hurt to have them
        self.request_head = self._set_http_method('HEAD')
        self.request_put = self._set_http_method('PUT')
        self.request_patch = self._set_http_method('PATCH')
        self.request_delete = self._set_http_method('DELETE')

    def _set_http_method(self, http_method):
        """Closure for creating the http method functions

        Arguments:
            http_method {str} -- Method to return a function for

        Returns:
            function -- the Closure

        Raises:
            ValueError -- If the max number of attempts have been met
        """
        def make_request(url, max_tries=3, _try_count=1, **kwargs):
            if max_tries < 1:
                # TODO: Find a better error to raise
                raise ValueError("max_tries must be >= 1")

            proxy_used = self.session.proxies.get('http')
            if 'proxy' in kwargs:
                # Proxy is not a valid arg to pass in, so fix it
                kwargs['proxies'] = self._format_proxy(kwargs['proxy'])
                proxy_used = kwargs['proxies'].get('http')
                del kwargs['proxy']
            elif 'proxies' in kwargs:
                # Make sure they are in the correct format
                kwargs['proxies'] = self._format_proxy(kwargs['proxies'])
                proxy_used = kwargs['proxies'].get('http')

            time_of_request = datetime.datetime.utcnow()
            try:
                r = self.session.request(http_method, url, **kwargs)

                log_extra = {'url': r.url,
                             'method': http_method,
                             'status_code': r.status_code,
                             'headers': {'request': dict(r.request.headers),
                                         'response': dict(r.headers)},
                             'response_time': r.elapsed.total_seconds(),
                             'time_of_request:': str(time_of_request),
                             'num_tries': _try_count,
                             'max_tries': max_tries,
                             'task': self.task,
                             'scraper_name': config['SCRAPER_NAME'],
                             'proxy': proxy_used}
                logger.info("Request finished", extra=log_extra)

                if r.status_code != requests.codes.ok:
                    if (_try_count < max_tries
                       and r.status_code not in self._ignore_codes):
                        kwargs = self.new_profile(**kwargs)
                        request_method = self._set_http_method(http_method)
                        return request_method(url,
                                              max_tries=max_tries,
                                              _try_count=_try_count + 1,
                                              **kwargs)
                    else:
                        logger.error("Download failed: status code",
                                     extra=log_extra)
                        r.raise_for_status()

            except requests.exceptions.HTTPError:
                raise

            except Exception as e:
                if _try_count < max_tries:
                    kwargs = self.new_profile(**kwargs)
                    request_method = self._set_http_method(http_method)
                    return request_method(url,
                                          max_tries=max_tries,
                                          _try_count=_try_count + 1,
                                          **kwargs)
                else:
                    logger.exception(f"Download failed: {str(e)}",
                                     extra={'url': url,
                                            'session_headers': self.session.headers,  # noqa E501
                                            'request_kwargs': kwargs,
                                            'num_tries': _try_count,
                                            'max_tries': max_tries,
                                            'task': self.task,
                                            'scraper_name': config['SCRAPER_NAME'],  # noqa E501
                                            'proxy': proxy_used})
                    raise DownloadValueError(f"Download failed: {str(e)}")

            return r

        return make_request

    def get_file(self, url, **kwargs):
        r = self.session.get(url, stream=True, **kwargs)
        return r

    def _set_session_ua(self):
        """Set up the session user agent

        Try and set a default user agent for the session
        """
        device_type = self.task.get('device_type', 'desktop')
        try:
            ua = self._get_user_agent(device_type)
            self.session.headers.update({'user-agent': ua})
        except ValueError:
            logger.error("Invalid device type {device_type} for UA",
                         extra={'task': self.task,
                                'scraper_name': config['SCRAPER_NAME']})

    def new_profile(self, **kwargs):
        """Set a new user agent and proxy to be used for the request

        Arguments:
            **kwargs {kwargs} -- Used when only changing the request,
                                 not the session

        Returns:
            kwargs -- The args for the new request
        """
        # Set new UA
        # TODO: make this for headers in general
        #       this sdk will only update the UA
        #       let the scraper update more
        self._set_session_ua()

        # Set new proxy
        proxy_str = self._get_proxy(country=self.task.get('proxy_country'))
        if 'proxy' in kwargs:
            # Replace the request specific
            kwargs['proxy'] = proxy_str

        elif 'proxies' in kwargs:
            # Replace the request specific
            kwargs['proxies'] = proxy_str

        else:
            # Replace the session proxy
            self.session.proxies = self._format_proxy(proxy_str)

        return kwargs
