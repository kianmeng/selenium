# Licensed to the Software Freedom Conservancy (SFC) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The SFC licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""The WebDriver implementation."""

import copy
from importlib import import_module

import pkgutil

import sys
from typing import Dict, List, Optional, Union

import warnings

from abc import ABCMeta
from base64 import b64decode
from contextlib import asynccontextmanager, contextmanager

from .bidi_connection import BidiConnection
from .command import Command
from .errorhandler import ErrorHandler
from .file_detector import FileDetector, LocalFileDetector
from .mobile import Mobile
from .remote_connection import RemoteConnection
from .script_key import ScriptKey
from .shadowroot import ShadowRoot
from .switch_to import SwitchTo
from .webelement import WebElement

from selenium.common.exceptions import (InvalidArgumentException,
                                        JavascriptException,
                                        WebDriverException,
                                        NoSuchCookieException)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.options import BaseOptions
from selenium.webdriver.common.print_page_options import PrintOptions
from selenium.webdriver.common.timeouts import Timeouts
from selenium.webdriver.common.html5.application_cache import ApplicationCache
from selenium.webdriver.support.relative_locator import RelativeBy


_W3C_CAPABILITY_NAMES = frozenset([
    'acceptInsecureCerts',
    'browserName',
    'browserVersion',
    'pageLoadStrategy',
    'platformName',
    'proxy',
    'setWindowRect',
    'strictFileInteractability',
    'timeouts',
    'unhandledPromptBehavior',
    'webSocketUrl'
])

_OSS_W3C_CONVERSION = {
    'acceptSslCerts': 'acceptInsecureCerts',
    'version': 'browserVersion',
    'platform': 'platformName'
}


cdp = None


def import_cdp():
    global cdp
    if not cdp:
        cdp = import_module("selenium.webdriver.common.bidi.cdp")


def _make_w3c_caps(caps):
    """Makes a W3C alwaysMatch capabilities object.

    Filters out capability names that are not in the W3C spec. Spec-compliant
    drivers will reject requests containing unknown capability names.

    Moves the Firefox profile, if present, from the old location to the new Firefox
    options object.

    :Args:
     - caps - A dictionary of capabilities requested by the caller.
    """
    caps = copy.deepcopy(caps)
    profile = caps.get('firefox_profile')
    always_match = {}
    if caps.get('proxy') and caps['proxy'].get('proxyType'):
        caps['proxy']['proxyType'] = caps['proxy']['proxyType'].lower()
    for k, v in caps.items():
        if v and k in _OSS_W3C_CONVERSION:
            always_match[_OSS_W3C_CONVERSION[k]] = v.lower() if k == 'platform' else v
        if k in _W3C_CAPABILITY_NAMES or ':' in k:
            always_match[k] = v
    if profile:
        moz_opts = always_match.get('moz:firefoxOptions', {})
        # If it's already present, assume the caller did that intentionally.
        if 'profile' not in moz_opts:
            # Don't mutate the original capabilities.
            new_opts = copy.deepcopy(moz_opts)
            new_opts['profile'] = profile
            always_match['moz:firefoxOptions'] = new_opts
    return {"firstMatch": [{}], "alwaysMatch": always_match}


def get_remote_connection(capabilities, command_executor, keep_alive, ignore_local_proxy=False):
    from selenium.webdriver.chromium.remote_connection import ChromiumRemoteConnection
    from selenium.webdriver.safari.remote_connection import SafariRemoteConnection
    from selenium.webdriver.firefox.remote_connection import FirefoxRemoteConnection

    candidates = [RemoteConnection] + [ChromiumRemoteConnection, SafariRemoteConnection, FirefoxRemoteConnection]
    handler = next(
        (c for c in candidates if c.browser_name == capabilities.get('browserName')),
        RemoteConnection
    )

    return handler(command_executor, keep_alive=keep_alive, ignore_proxy=ignore_local_proxy)


def create_matches(options: List[BaseOptions]) -> Dict:
    capabilities = {"capabilities": {}}
    opts = []
    for opt in options:
        opts.append(opt.to_capabilities())
    opts_size = len(opts)
    samesies = {}

    # Can not use bitwise operations on the dicts or lists due to
    # https://bugs.python.org/issue38210
    for i in range(opts_size):
        min_index = i
        if i + 1 < opts_size:
            first_keys = opts[min_index].keys()

            for kys in first_keys:
                if kys in opts[i + 1].keys():
                    if opts[min_index][kys] == opts[i + 1][kys]:
                        samesies.update({kys: opts[min_index][kys]})

    always = {}
    for k, v in samesies.items():
        always[k] = v

    for i in opts:
        for k in always.keys():
            del i[k]

    capabilities["capabilities"]["alwaysMatch"] = always
    capabilities["capabilities"]["firstMatch"] = opts

    return capabilities


class BaseWebDriver(metaclass=ABCMeta):
    """
    Abstract Base Class for all Webdriver subtypes.
    ABC's allow custom implementations of Webdriver to be registered so that isinstance type checks
    will succeed.
    """


class WebDriver(BaseWebDriver):
    """
    Controls a browser by sending commands to a remote server.
    This server is expected to be running the WebDriver wire protocol
    as defined at
    https://github.com/SeleniumHQ/selenium/wiki/JsonWireProtocol

    :Attributes:
     - session_id - String ID of the browser session started and controlled by this WebDriver.
     - capabilities - Dictionary of effective capabilities of this browser session as returned
         by the remote server. See https://github.com/SeleniumHQ/selenium/wiki/DesiredCapabilities
     - command_executor - remote_connection.RemoteConnection object used to execute commands.
     - error_handler - errorhandler.ErrorHandler object used to handle errors.
    """

    _web_element_cls = WebElement
    _shadowroot_cls = ShadowRoot

    def __init__(self, command_executor='http://127.0.0.1:4444',
                 desired_capabilities=None, browser_profile=None, proxy=None,
                 keep_alive=True, file_detector=None, options: Union[BaseOptions, List[BaseOptions]] = None):
        """
        Create a new driver that will issue commands using the wire protocol.

        :Args:
         - command_executor - Either a string representing URL of the remote server or a custom
             remote_connection.RemoteConnection object. Defaults to 'http://127.0.0.1:4444/wd/hub'.
         - desired_capabilities - A dictionary of capabilities to request when
             starting the browser session. Required parameter.
         - browser_profile - A selenium.webdriver.firefox.firefox_profile.FirefoxProfile object.
             Only used if Firefox is requested. Optional.
         - proxy - A selenium.webdriver.common.proxy.Proxy object. The browser session will
             be started with given proxy settings, if possible. Optional.
         - keep_alive - Whether to configure remote_connection.RemoteConnection to use
             HTTP keep-alive. Defaults to True.
         - file_detector - Pass custom file detector object during instantiation. If None,
             then default LocalFileDetector() will be used.
         - options - instance of a driver options.Options class
        """
        if desired_capabilities:
            warnings.warn(
                "desired_capabilities has been deprecated, please pass in an Options object with options kwarg",
                DeprecationWarning,
                stacklevel=2
            )
        if browser_profile:
            warnings.warn(
                "browser_profile has been deprecated, please pass in an Firefox Options object with options kwarg",
                DeprecationWarning,
                stacklevel=2
            )
        if proxy:
            warnings.warn(
                "proxy has been deprecated, please pass in an Options object with options kwarg",
                DeprecationWarning,
                stacklevel=2
            )
        if not keep_alive:
            warnings.warn(
                "keep_alive has been deprecated. We will be using True as the default value as we start removing it.",
                DeprecationWarning,
                stacklevel=2
            )
        capabilities = {}
        # If we get a list we can assume that no capabilities
        # have been passed in
        if isinstance(options, list):
            capabilities = create_matches(options)
        else:
            _ignore_local_proxy = False
            if options:
                capabilities = options.to_capabilities()
                _ignore_local_proxy = options._ignore_local_proxy
            if desired_capabilities:
                if not isinstance(desired_capabilities, dict):
                    raise WebDriverException("Desired Capabilities must be a dictionary")
                else:
                    capabilities.update(desired_capabilities)
        self.command_executor = command_executor
        if isinstance(self.command_executor, (str, bytes)):
            self.command_executor = get_remote_connection(capabilities, command_executor=command_executor,
                                                          keep_alive=keep_alive,
                                                          ignore_local_proxy=_ignore_local_proxy)
        self._is_remote = True
        self.session_id = None
        self.caps = {}
        self.pinned_scripts = {}
        self.error_handler = ErrorHandler()
        self._switch_to = SwitchTo(self)
        self._mobile = Mobile(self)
        self.file_detector = file_detector or LocalFileDetector()
        self.start_client()
        self.start_session(capabilities, browser_profile)

    def __repr__(self):
        return '<{0.__module__}.{0.__name__} (session="{1}")>'.format(
            type(self), self.session_id)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.quit()

    @contextmanager
    def file_detector_context(self, file_detector_class, *args, **kwargs):
        """
        Overrides the current file detector (if necessary) in limited context.
        Ensures the original file detector is set afterwards.

        Example:

        with webdriver.file_detector_context(UselessFileDetector):
            someinput.send_keys('/etc/hosts')

        :Args:
         - file_detector_class - Class of the desired file detector. If the class is different
             from the current file_detector, then the class is instantiated with args and kwargs
             and used as a file detector during the duration of the context manager.
         - args - Optional arguments that get passed to the file detector class during
             instantiation.
         - kwargs - Keyword arguments, passed the same way as args.
        """
        last_detector = None
        if not isinstance(self.file_detector, file_detector_class):
            last_detector = self.file_detector
            self.file_detector = file_detector_class(*args, **kwargs)
        try:
            yield
        finally:
            if last_detector:
                self.file_detector = last_detector

    @property
    def mobile(self):
        return self._mobile

    @property
    def name(self) -> str:
        """Returns the name of the underlying browser for this instance.

        :Usage:
            ::

                name = driver.name
        """
        if 'browserName' in self.caps:
            return self.caps['browserName']
        else:
            raise KeyError('browserName not specified in session capabilities')

    def start_client(self):
        """
        Called before starting a new session. This method may be overridden
        to define custom startup behavior.
        """
        pass

    def stop_client(self):
        """
        Called after executing a quit command. This method may be overridden
        to define custom shutdown behavior.
        """
        pass

    def start_session(self, capabilities: dict, browser_profile=None) -> None:
        """
        Creates a new session with the desired capabilities.

        :Args:
         - capabilities - a capabilities dict to start the session with.
         - browser_profile - A selenium.webdriver.firefox.firefox_profile.FirefoxProfile object. Only used if Firefox is requested.
        """
        if not isinstance(capabilities, dict):
            raise InvalidArgumentException("Capabilities must be a dictionary")
        if browser_profile:
            if "moz:firefoxOptions" in capabilities:
                capabilities["moz:firefoxOptions"]["profile"] = browser_profile.encoded
            else:
                capabilities.update({'firefox_profile': browser_profile.encoded})
        w3c_caps = _make_w3c_caps(capabilities)
        parameters = {"capabilities": w3c_caps,
                      "desiredCapabilities": capabilities}
        response = self.execute(Command.NEW_SESSION, parameters)
        if 'sessionId' not in response:
            response = response['value']
        self.session_id = response['sessionId']
        self.caps = response.get('value')

        # if capabilities is none we are probably speaking to
        # a W3C endpoint
        if not self.caps:
            self.caps = response.get('capabilities')

    def _wrap_value(self, value):
        if isinstance(value, dict):
            converted = {}
            for key, val in value.items():
                converted[key] = self._wrap_value(val)
            return converted
        elif isinstance(value, self._web_element_cls):
            return {'element-6066-11e4-a52e-4f735466cecf': value.id}
        elif isinstance(value, self._shadowroot_cls):
            return {'shadow-6066-11e4-a52e-4f735466cecf': value.id}
        elif isinstance(value, list):
            return list(self._wrap_value(item) for item in value)
        else:
            return value

    def create_web_element(self, element_id: str) -> WebElement:
        """Creates a web element with the specified `element_id`."""
        return self._web_element_cls(self, element_id)

    def _unwrap_value(self, value):
        if isinstance(value, dict):
            if 'element-6066-11e4-a52e-4f735466cecf' in value:
                return self.create_web_element(value['element-6066-11e4-a52e-4f735466cecf'])
            elif 'shadow-6066-11e4-a52e-4f735466cecf' in value:
                return self._shadowroot_cls(self, value['shadow-6066-11e4-a52e-4f735466cecf'])
            else:
                for key, val in value.items():
                    value[key] = self._unwrap_value(val)
                return value
        elif isinstance(value, list):
            return list(self._unwrap_value(item) for item in value)
        else:
            return value

    def execute(self, driver_command: str, params: dict = None) -> dict:
        """
        Sends a command to be executed by a command.CommandExecutor.

        :Args:
         - driver_command: The name of the command to execute as a string.
         - params: A dictionary of named parameters to send with the command.

        :Returns:
          The command's JSON response loaded into a dictionary object.
        """
        if self.session_id:
            if not params:
                params = {'sessionId': self.session_id}
            elif 'sessionId' not in params:
                params['sessionId'] = self.session_id

        params = self._wrap_value(params)
        response = self.command_executor.execute(driver_command, params)
        if response:
            self.error_handler.check_response(response)
            response['value'] = self._unwrap_value(
                response.get('value', None))
            return response
        # If the server doesn't send a response, assume the command was
        # a success
        return {'success': 0, 'value': None, 'sessionId': self.session_id}

    def get(self, url: str) -> None:
        """
        Loads a web page in the current browser session.
        """
        self.execute(Command.GET, {'url': url})

    @property
    def title(self) -> str:
        """Returns the title of the current page.

        :Usage:
            ::

                title = driver.title
        """
        resp = self.execute(Command.GET_TITLE)
        return resp['value'] if resp['value'] else ""

    def find_element_by_id(self, id_) -> WebElement:
        """Finds an element by id.

        :Args:
         - id\\_ - The id of the element to be found.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_id('foo')
        """
        warnings.warn(
            "find_element_by_* commands are deprecated. Please use find_element() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_element(by=By.ID, value=id_)

    def find_elements_by_id(self, id_) -> WebElement:
        """
        Finds multiple elements by id.

        :Args:
         - id\\_ - The id of the elements to be found.

        :Returns:
         - list of WebElement - a list with elements if any was found.  An
           empty list if not

        :Usage:
            ::

                elements = driver.find_elements_by_id('foo')
        """
        warnings.warn(
            "find_elements_by_* commands are deprecated. Please use find_elements() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_elements(by=By.ID, value=id_)

    def find_element_by_xpath(self, xpath) -> WebElement:
        """
        Finds an element by xpath.

        :Args:
         - xpath - The xpath locator of the element to find.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_xpath('//div/td[1]')
        """
        warnings.warn(
            "find_element_by_* commands are deprecated. Please use find_element() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_element(by=By.XPATH, value=xpath)

    def find_elements_by_xpath(self, xpath) -> WebElement:
        """
        Finds multiple elements by xpath.

        :Args:
         - xpath - The xpath locator of the elements to be found.

        :Returns:
         - list of WebElement - a list with elements if any was found.  An
           empty list if not

        :Usage:
            ::

                elements = driver.find_elements_by_xpath("//div[contains(@class, 'foo')]")
        """
        warnings.warn(
            "find_elements_by_* commands are deprecated. Please use find_elements() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_elements(by=By.XPATH, value=xpath)

    def find_element_by_link_text(self, link_text) -> WebElement:
        """
        Finds an element by link text.

        :Args:
         - link_text: The text of the element to be found.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_link_text('Sign In')
        """
        warnings.warn(
            "find_element_by_* commands are deprecated. Please use find_element() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_element(by=By.LINK_TEXT, value=link_text)

    def find_elements_by_link_text(self, text) -> WebElement:
        """
        Finds elements by link text.

        :Args:
         - link_text: The text of the elements to be found.

        :Returns:
         - list of webelement - a list with elements if any was found.  an
           empty list if not

        :Usage:
            ::

                elements = driver.find_elements_by_link_text('Sign In')
        """
        warnings.warn(
            "find_elements_by_* commands are deprecated. Please use find_elements() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_elements(by=By.LINK_TEXT, value=text)

    def find_element_by_partial_link_text(self, link_text) -> WebElement:
        """
        Finds an element by a partial match of its link text.

        :Args:
         - link_text: The text of the element to partially match on.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_partial_link_text('Sign')
        """
        warnings.warn(
            "find_element_by_* commands are deprecated. Please use find_element() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_element(by=By.PARTIAL_LINK_TEXT, value=link_text)

    def find_elements_by_partial_link_text(self, link_text) -> WebElement:
        """
        Finds elements by a partial match of their link text.

        :Args:
         - link_text: The text of the element to partial match on.

        :Returns:
         - list of webelement - a list with elements if any was found.  an
           empty list if not

        :Usage:
            ::

                elements = driver.find_elements_by_partial_link_text('Sign')
        """
        warnings.warn(
            "find_elements_by_* commands are deprecated. Please use find_elements() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_elements(by=By.PARTIAL_LINK_TEXT, value=link_text)

    def find_element_by_name(self, name) -> WebElement:
        """
        Finds an element by name.

        :Args:
         - name: The name of the element to find.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_name('foo')
        """
        warnings.warn(
            "find_element_by_* commands are deprecated. Please use find_element() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_element(by=By.NAME, value=name)

    def find_elements_by_name(self, name) -> WebElement:
        """
        Finds elements by name.

        :Args:
         - name: The name of the elements to find.

        :Returns:
         - list of webelement - a list with elements if any was found.  an
           empty list if not

        :Usage:
            ::

                elements = driver.find_elements_by_name('foo')
        """
        warnings.warn(
            "find_elements_by_* commands are deprecated. Please use find_elements() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_elements(by=By.NAME, value=name)

    def find_element_by_tag_name(self, name) -> WebElement:
        """
        Finds an element by tag name.

        :Args:
         - name - name of html tag (eg: h1, a, span)

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_tag_name('h1')
        """
        warnings.warn(
            "find_element_by_* commands are deprecated. Please use find_element() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_element(by=By.TAG_NAME, value=name)

    def find_elements_by_tag_name(self, name) -> WebElement:
        """
        Finds elements by tag name.

        :Args:
         - name - name of html tag (eg: h1, a, span)

        :Returns:
         - list of WebElement - a list with elements if any was found.  An
           empty list if not

        :Usage:
            ::

                elements = driver.find_elements_by_tag_name('h1')
        """
        warnings.warn(
            "find_elements_by_* commands are deprecated. Please use find_elements() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_elements(by=By.TAG_NAME, value=name)

    def find_element_by_class_name(self, name) -> WebElement:
        """
        Finds an element by class name.

        :Args:
         - name: The class name of the element to find.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_class_name('foo')
        """
        warnings.warn(
            "find_element_by_* commands are deprecated. Please use find_element() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_element(by=By.CLASS_NAME, value=name)

    def find_elements_by_class_name(self, name) -> WebElement:
        """
        Finds elements by class name.

        :Args:
         - name: The class name of the elements to find.

        :Returns:
         - list of WebElement - a list with elements if any was found.  An
           empty list if not

        :Usage:
            ::

                elements = driver.find_elements_by_class_name('foo')
        """
        warnings.warn(
            "find_elements_by_* commands are deprecated. Please use find_elements() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_elements(by=By.CLASS_NAME, value=name)

    def find_element_by_css_selector(self, css_selector) -> WebElement:
        """
        Finds an element by css selector.

        :Args:
         - css_selector - CSS selector string, ex: 'a.nav#home'

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_css_selector('#foo')
        """
        warnings.warn(
            "find_element_by_* commands are deprecated. Please use find_element() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_element(by=By.CSS_SELECTOR, value=css_selector)

    def find_elements_by_css_selector(self, css_selector) -> WebElement:
        """
        Finds elements by css selector.

        :Args:
         - css_selector - CSS selector string, ex: 'a.nav#home'

        :Returns:
         - list of WebElement - a list with elements if any was found.  An
           empty list if not

        :Usage:
            ::

                elements = driver.find_elements_by_css_selector('.foo')
        """
        warnings.warn(
            "find_elements_by_* commands are deprecated. Please use find_elements() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_elements(by=By.CSS_SELECTOR, value=css_selector)

    def pin_script(self, script, script_key=None) -> ScriptKey:
        """

        """
        if not script_key:
            _script_key = ScriptKey()
        else:
            _script_key = ScriptKey(script_key)
        self.pinned_scripts[_script_key.id] = script
        return _script_key

    def unpin(self, script_key) -> None:
        """

        """
        self.pinned_scripts.pop(script_key.id)

    def get_pinned_scripts(self) -> List[str]:
        """

        """
        return list(self.pinned_scripts.keys())

    def execute_script(self, script, *args):
        """
        Synchronously Executes JavaScript in the current window/frame.

        :Args:
         - script: The JavaScript to execute.
         - \\*args: Any applicable arguments for your JavaScript.

        :Usage:
            ::

                driver.execute_script('return document.title;')
        """
        if isinstance(script, ScriptKey):
            try:
                script = self.pinned_scripts[script.id]
            except KeyError:
                raise JavascriptException("Pinned script could not be found")

        converted_args = list(args)
        command = Command.W3C_EXECUTE_SCRIPT

        return self.execute(command, {
            'script': script,
            'args': converted_args})['value']

    def execute_async_script(self, script: str, *args):
        """
        Asynchronously Executes JavaScript in the current window/frame.

        :Args:
         - script: The JavaScript to execute.
         - \\*args: Any applicable arguments for your JavaScript.

        :Usage:
            ::

                script = "var callback = arguments[arguments.length - 1]; " \\
                         "window.setTimeout(function(){ callback('timeout') }, 3000);"
                driver.execute_async_script(script)
        """
        converted_args = list(args)
        command = Command.W3C_EXECUTE_SCRIPT_ASYNC

        return self.execute(command, {
            'script': script,
            'args': converted_args})['value']

    @property
    def current_url(self) -> str:
        """
        Gets the URL of the current page.

        :Usage:
            ::

                driver.current_url
        """
        return self.execute(Command.GET_CURRENT_URL)['value']

    @property
    def page_source(self) -> str:
        """
        Gets the source of the current page.

        :Usage:
            ::

                driver.page_source
        """
        return self.execute(Command.GET_PAGE_SOURCE)['value']

    def close(self) -> None:
        """
        Closes the current window.

        :Usage:
            ::

                driver.close()
        """
        self.execute(Command.CLOSE)

    def quit(self) -> None:
        """
        Quits the driver and closes every associated window.

        :Usage:
            ::

                driver.quit()
        """
        try:
            self.execute(Command.QUIT)
        finally:
            self.stop_client()
            self.command_executor.close()

    @property
    def current_window_handle(self) -> str:
        """
        Returns the handle of the current window.

        :Usage:
            ::

                driver.current_window_handle
        """
        return self.execute(Command.W3C_GET_CURRENT_WINDOW_HANDLE)['value']

    @property
    def window_handles(self) -> List[str]:
        """
        Returns the handles of all windows within the current session.

        :Usage:
            ::

                driver.window_handles
        """
        return self.execute(Command.W3C_GET_WINDOW_HANDLES)['value']

    def maximize_window(self) -> None:
        """
        Maximizes the current window that webdriver is using
        """
        params = None
        command = Command.W3C_MAXIMIZE_WINDOW
        self.execute(command, params)

    def fullscreen_window(self) -> None:
        """
        Invokes the window manager-specific 'full screen' operation
        """
        self.execute(Command.FULLSCREEN_WINDOW)

    def minimize_window(self) -> None:
        """
        Invokes the window manager-specific 'minimize' operation
        """
        self.execute(Command.MINIMIZE_WINDOW)

    def print_page(self, print_options: Optional[PrintOptions] = None) -> str:
        """
        Takes PDF of the current page.
        The driver makes a best effort to return a PDF based on the provided parameters.
        """
        options = {}
        if print_options:
            options = print_options.to_dict()

        return self.execute(Command.PRINT_PAGE, options)['value']

    @property
    def switch_to(self) -> SwitchTo:
        """
        :Returns:
            - SwitchTo: an object containing all options to switch focus into

        :Usage:
            ::

                element = driver.switch_to.active_element
                alert = driver.switch_to.alert
                driver.switch_to.default_content()
                driver.switch_to.frame('frame_name')
                driver.switch_to.frame(1)
                driver.switch_to.frame(driver.find_elements_by_tag_name("iframe")[0])
                driver.switch_to.parent_frame()
                driver.switch_to.window('main')
        """
        return self._switch_to

    # Navigation
    def back(self) -> None:
        """
        Goes one step backward in the browser history.

        :Usage:
            ::

                driver.back()
        """
        self.execute(Command.GO_BACK)

    def forward(self) -> None:
        """
        Goes one step forward in the browser history.

        :Usage:
            ::

                driver.forward()
        """
        self.execute(Command.GO_FORWARD)

    def refresh(self) -> None:
        """
        Refreshes the current page.

        :Usage:
            ::

                driver.refresh()
        """
        self.execute(Command.REFRESH)

    # Options
    def get_cookies(self) -> List[dict]:
        """
        Returns a set of dictionaries, corresponding to cookies visible in the current session.

        :Usage:
            ::

                driver.get_cookies()
        """
        return self.execute(Command.GET_ALL_COOKIES)['value']

    def get_cookie(self, name) -> dict:
        """
        Get a single cookie by name. Returns the cookie if found, None if not.

        :Usage:
            ::

                driver.get_cookie('my_cookie')
        """
        try:
            return self.execute(Command.GET_COOKIE, {'name': name})['value']
        except NoSuchCookieException:
            return None

    def delete_cookie(self, name) -> None:
        """
        Deletes a single cookie with the given name.

        :Usage:
            ::

                driver.delete_cookie('my_cookie')
        """
        self.execute(Command.DELETE_COOKIE, {'name': name})

    def delete_all_cookies(self) -> None:
        """
        Delete all cookies in the scope of the session.

        :Usage:
            ::

                driver.delete_all_cookies()
        """
        self.execute(Command.DELETE_ALL_COOKIES)

    def add_cookie(self, cookie_dict) -> None:
        """
        Adds a cookie to your current session.

        :Args:
         - cookie_dict: A dictionary object, with required keys - "name" and "value";
            optional keys - "path", "domain", "secure", "expiry", "sameSite"

        Usage:
            driver.add_cookie({'name' : 'foo', 'value' : 'bar'})
            driver.add_cookie({'name' : 'foo', 'value' : 'bar', 'path' : '/'})
            driver.add_cookie({'name' : 'foo', 'value' : 'bar', 'path' : '/', 'secure':True})
            driver.add_cookie({'name': 'foo', 'value': 'bar', 'sameSite': 'Strict'})

        """
        if 'sameSite' in cookie_dict:
            assert cookie_dict['sameSite'] in ['Strict', 'Lax', 'None']
            self.execute(Command.ADD_COOKIE, {'cookie': cookie_dict})
        else:
            self.execute(Command.ADD_COOKIE, {'cookie': cookie_dict})

    # Timeouts
    def implicitly_wait(self, time_to_wait) -> None:
        """
        Sets a sticky timeout to implicitly wait for an element to be found,
           or a command to complete. This method only needs to be called one
           time per session. To set the timeout for calls to
           execute_async_script, see set_script_timeout.

        :Args:
         - time_to_wait: Amount of time to wait (in seconds)

        :Usage:
            ::

                driver.implicitly_wait(30)
        """
        self.execute(Command.SET_TIMEOUTS, {
            'implicit': int(float(time_to_wait) * 1000)})

    def set_script_timeout(self, time_to_wait) -> None:
        """
        Set the amount of time that the script should wait during an
           execute_async_script call before throwing an error.

        :Args:
         - time_to_wait: The amount of time to wait (in seconds)

        :Usage:
            ::

                driver.set_script_timeout(30)
        """
        self.execute(Command.SET_TIMEOUTS, {
            'script': int(float(time_to_wait) * 1000)})

    def set_page_load_timeout(self, time_to_wait) -> None:
        """
        Set the amount of time to wait for a page load to complete
           before throwing an error.

        :Args:
         - time_to_wait: The amount of time to wait

        :Usage:
            ::

                driver.set_page_load_timeout(30)
        """
        try:
            self.execute(Command.SET_TIMEOUTS, {
                'pageLoad': int(float(time_to_wait) * 1000)})
        except WebDriverException:
            self.execute(Command.SET_TIMEOUTS, {
                'ms': float(time_to_wait) * 1000,
                'type': 'page load'})

    @property
    def timeouts(self) -> Timeouts:
        """
        Get all the timeouts that have been set on the current session

        :Usage:
            ::
                driver.timeouts
        :rtype: Timeout
        """
        timeouts = self.execute(Command.GET_TIMEOUTS)['value']
        timeouts["implicit_wait"] = timeouts.pop("implicit") / 1000
        timeouts["page_load"] = timeouts.pop("pageLoad") / 1000
        timeouts["script"] = timeouts.pop("script") / 1000
        return Timeouts(**timeouts)

    @timeouts.setter
    def timeouts(self, timeouts) -> None:
        """
        Set all timeouts for the session. This will override any previously
        set timeouts.

        :Usage:
            ::
                my_timeouts = Timeouts()
                my_timeouts.implicit_wait = 10
                driver.timeouts = my_timeouts
        """
        self.execute(Command.SET_TIMEOUTS, timeouts._to_json())['value']

    def find_element(self, by=By.ID, value=None) -> WebElement:
        """
        Find an element given a By strategy and locator.

        :Usage:
            ::

                element = driver.find_element(By.ID, 'foo')

        :rtype: WebElement
        """
        if isinstance(by, RelativeBy):
            return self.find_elements(by=by, value=value)[0]

        if by == By.ID:
            by = By.CSS_SELECTOR
            value = '[id="%s"]' % value
        elif by == By.TAG_NAME:
            by = By.CSS_SELECTOR
        elif by == By.CLASS_NAME:
            by = By.CSS_SELECTOR
            value = ".%s" % value
        elif by == By.NAME:
            by = By.CSS_SELECTOR
            value = '[name="%s"]' % value

        return self.execute(Command.FIND_ELEMENT, {
            'using': by,
            'value': value})['value']

    def find_elements(self, by=By.ID, value=None) -> List[WebElement]:
        """
        Find elements given a By strategy and locator.

        :Usage:
            ::

                elements = driver.find_elements(By.CLASS_NAME, 'foo')

        :rtype: list of WebElement
        """
        if isinstance(by, RelativeBy):
            _pkg = '.'.join(__name__.split('.')[:-1])
            raw_function = pkgutil.get_data(_pkg, 'findElements.js').decode('utf8')
            find_element_js = "return ({}).apply(null, arguments);".format(raw_function)
            return self.execute_script(find_element_js, by.to_dict())

        if by == By.ID:
            by = By.CSS_SELECTOR
            value = '[id="%s"]' % value
        elif by == By.TAG_NAME:
            by = By.CSS_SELECTOR
        elif by == By.CLASS_NAME:
            by = By.CSS_SELECTOR
            value = ".%s" % value
        elif by == By.NAME:
            by = By.CSS_SELECTOR
            value = '[name="%s"]' % value

        # Return empty list if driver returns null
        # See https://github.com/SeleniumHQ/selenium/issues/4555
        return self.execute(Command.FIND_ELEMENTS, {
            'using': by,
            'value': value})['value'] or []

    @property
    def desired_capabilities(self) -> dict:
        """
        returns the drivers current desired capabilities being used
        """
        warnings.warn("desired_capabilities is deprecated. Please call capabilities.",
                      DeprecationWarning, stacklevel=2)
        return self.caps

    @property
    def capabilities(self) -> dict:
        """
        returns the drivers current capabilities being used.
        """
        return self.caps

    def get_screenshot_as_file(self, filename) -> bool:
        """
        Saves a screenshot of the current window to a PNG image file. Returns
           False if there is any IOError, else returns True. Use full paths in
           your filename.

        :Args:
         - filename: The full path you wish to save your screenshot to. This
           should end with a `.png` extension.

        :Usage:
            ::

                driver.get_screenshot_as_file('/Screenshots/foo.png')
        """
        if not filename.lower().endswith('.png'):
            warnings.warn("name used for saved screenshot does not match file "
                          "type. It should end with a `.png` extension", UserWarning)
        png = self.get_screenshot_as_png()
        try:
            with open(filename, 'wb') as f:
                f.write(png)
        except IOError:
            return False
        finally:
            del png
        return True

    def save_screenshot(self, filename) -> bool:
        """
        Saves a screenshot of the current window to a PNG image file. Returns
           False if there is any IOError, else returns True. Use full paths in
           your filename.

        :Args:
         - filename: The full path you wish to save your screenshot to. This
           should end with a `.png` extension.

        :Usage:
            ::

                driver.save_screenshot('/Screenshots/foo.png')
        """
        return self.get_screenshot_as_file(filename)

    def get_screenshot_as_png(self) -> str:
        """
        Gets the screenshot of the current window as a binary data.

        :Usage:
            ::

                driver.get_screenshot_as_png()
        """
        return b64decode(self.get_screenshot_as_base64().encode('ascii'))

    def get_screenshot_as_base64(self) -> str:
        """
        Gets the screenshot of the current window as a base64 encoded string
           which is useful in embedded images in HTML.

        :Usage:
            ::

                driver.get_screenshot_as_base64()
        """
        return self.execute(Command.SCREENSHOT)['value']

    def set_window_size(self, width, height, windowHandle='current') -> dict:
        """
        Sets the width and height of the current window. (window.resizeTo)

        :Args:
         - width: the width in pixels to set the window to
         - height: the height in pixels to set the window to

        :Usage:
            ::

                driver.set_window_size(800,600)
        """
        if windowHandle != 'current':
            warnings.warn("Only 'current' window is supported for W3C compatibile browsers.")
        self.set_window_rect(width=int(width), height=int(height))

    def get_window_size(self, windowHandle='current') -> dict:
        """
        Gets the width and height of the current window.

        :Usage:
            ::

                driver.get_window_size()
        """

        if windowHandle != 'current':
            warnings.warn("Only 'current' window is supported for W3C compatibile browsers.")
        size = self.get_window_rect()

        if size.get('value', None):
            size = size['value']

        return {k: size[k] for k in ('width', 'height')}

    def set_window_position(self, x, y, windowHandle='current') -> dict:
        """
        Sets the x,y position of the current window. (window.moveTo)

        :Args:
         - x: the x-coordinate in pixels to set the window position
         - y: the y-coordinate in pixels to set the window position

        :Usage:
            ::

                driver.set_window_position(0,0)
        """
        if windowHandle != 'current':
            warnings.warn("Only 'current' window is supported for W3C compatibile browsers.")
        return self.set_window_rect(x=int(x), y=int(y))

    def get_window_position(self, windowHandle='current') -> dict:
        """
        Gets the x,y position of the current window.

        :Usage:
            ::

                driver.get_window_position()
        """

        if windowHandle != 'current':
            warnings.warn("Only 'current' window is supported for W3C compatibile browsers.")
        position = self.get_window_rect()

        return {k: position[k] for k in ('x', 'y')}

    def get_window_rect(self) -> dict:
        """
        Gets the x, y coordinates of the window as well as height and width of
        the current window.

        :Usage:
            ::

                driver.get_window_rect()
        """
        return self.execute(Command.GET_WINDOW_RECT)['value']

    def set_window_rect(self, x=None, y=None, width=None, height=None) -> dict:
        """
        Sets the x, y coordinates of the window as well as height and width of
        the current window. This method is only supported for W3C compatible
        browsers; other browsers should use `set_window_position` and
        `set_window_size`.

        :Usage:
            ::

                driver.set_window_rect(x=10, y=10)
                driver.set_window_rect(width=100, height=200)
                driver.set_window_rect(x=10, y=10, width=100, height=200)
        """

        if (x is None and y is None) and (not height and not width):
            raise InvalidArgumentException("x and y or height and width need values")

        return self.execute(Command.SET_WINDOW_RECT, {"x": x, "y": y,
                                                      "width": width,
                                                      "height": height})['value']

    @property
    def file_detector(self):
        return self._file_detector

    @file_detector.setter
    def file_detector(self, detector):
        """
        Set the file detector to be used when sending keyboard input.
        By default, this is set to a file detector that does nothing.

        see FileDetector
        see LocalFileDetector
        see UselessFileDetector

        :Args:
         - detector: The detector to use. Must not be None.
        """
        if not detector:
            raise WebDriverException("You may not set a file detector that is null")
        if not isinstance(detector, FileDetector):
            raise WebDriverException("Detector has to be instance of FileDetector")
        self._file_detector = detector

    @property
    def orientation(self):
        """
        Gets the current orientation of the device

        :Usage:
            ::

                orientation = driver.orientation
        """
        return self.execute(Command.GET_SCREEN_ORIENTATION)['value']

    @orientation.setter
    def orientation(self, value):
        """
        Sets the current orientation of the device

        :Args:
         - value: orientation to set it to.

        :Usage:
            ::

                driver.orientation = 'landscape'
        """
        allowed_values = ['LANDSCAPE', 'PORTRAIT']
        if value.upper() in allowed_values:
            self.execute(Command.SET_SCREEN_ORIENTATION, {'orientation': value})
        else:
            raise WebDriverException("You can only set the orientation to 'LANDSCAPE' and 'PORTRAIT'")

    @property
    def application_cache(self):
        """ Returns a ApplicationCache Object to interact with the browser app cache"""
        return ApplicationCache(self)

    @property
    def log_types(self):
        """
        Gets a list of the available log types. This only works with w3c compliant browsers.

        :Usage:
            ::

                driver.log_types
        """
        return self.execute(Command.GET_AVAILABLE_LOG_TYPES)['value']

    def get_log(self, log_type):
        """
        Gets the log for a given log type

        :Args:
         - log_type: type of log that which will be returned

        :Usage:
            ::

                driver.get_log('browser')
                driver.get_log('driver')
                driver.get_log('client')
                driver.get_log('server')
        """
        return self.execute(Command.GET_LOG, {'type': log_type})['value']

    @asynccontextmanager
    async def bidi_connection(self):
        assert sys.version_info >= (3, 7)
        global cdp
        import_cdp()
        ws_url = None
        if self.caps.get("se:cdp"):
            ws_url = self.caps.get("se:cdp")
            version = self.caps.get("se:cdpVersion").split(".")[0]
        else:
            version, ws_url = self._get_cdp_details()

        if not ws_url:
            raise WebDriverException("Unable to find url to connect to from capabilities")

        cdp.import_devtools(version)

        devtools = import_module("selenium.webdriver.common.devtools.v{}".format(version))
        async with cdp.open_cdp(ws_url) as conn:
            targets = await conn.execute(devtools.target.get_targets())
            target_id = targets[0].target_id
            async with conn.open_session(target_id) as session:
                yield BidiConnection(session, cdp, devtools)

    def _get_cdp_details(self):
        import json
        import urllib3

        http = urllib3.PoolManager()
        _firefox = False
        if self.caps.get("browserName") == "chrome":
            debugger_address = self.caps.get(f"{self.vendor_prefix}:{self.caps.get('browserName')}Options").get("debuggerAddress")
        else:
            _firefox = True
            debugger_address = self.caps.get("moz:debuggerAddress")
        res = http.request('GET', f"http://{debugger_address}/json/version")
        data = json.loads(res.data)

        browser_version = data.get("Browser")
        websocket_url = data.get("webSocketDebuggerUrl")

        import re
        if _firefox:
            # Mozilla Automation Team asked to only support 85
            # until WebDriver Bidi is available.
            version = 85
        else:
            version = re.search(r".*/(\d+)\.", browser_version).group(1)

        return version, websocket_url
