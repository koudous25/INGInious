# -*- coding: utf-8 -*-
#
# This file is part of INGInious. See the LICENSE and the COPYRIGHTS files for
# more information about the licensing of this file.
import inspect
import os, time, gettext, re
import os.path
from copy import deepcopy
import hashlib
from web import utils
import web
from web.session import SessionExpired


class CookieLessCompatibleApplication(web.application):
    def __init__(self, session_storage):
        """
        :param session_storage: a Storage object, where sessions will be saved
        """
        super(CookieLessCompatibleApplication, self).__init__((), globals(), autoreload=False)
        self._session = CookieLessCompatibleSession(self, session_storage)
        self._translations = {}

        # hacky fix until web.py is fixed
        self.processors = [self.fix_unloadhook(x) for x in self.processors]

    def fix_unloadhook(self, orig_func):
        """ Fix web.py that raises StopIterations everywhere.

            The bug in web.py lies (partly) on line 574 of application.py:

                def build_result(result):
                    for r in result:
                        if PY2:
                            yield utils.safestr(r)

            The for loop "r in result" fails as result is a generator that raise sometimes StopIteration.
            This is difficult to fix directly without modifying webpy in a lot of place, so we prefer fixing the symptoms.

            When you do next(x) on a generator, and that this generator raise a StopIteration, next() catches the
            StopIteration and raise in return a RuntimeError(("generator raised StopIteration",)).

            That's what we catch here.

            The generator is then given to another one, then to another one, etc, until it reaches the function
            "unloadhook", that is a preprocessor, and is init by the constructor of the web.application (i.e. the super
            constructor of this class) and is put inside the self.processors array.

            We apply the fix on all processors as we can't find the one that is actually the one created by unloadhook
            by inspection. This should not change anything.
        """
        def fix_generator(orig_generator):
            try:
                yield from orig_generator
            except RuntimeError as e:
                if e.args != ("generator raised StopIteration",):
                    raise

        def fix(x):
            y = orig_func(x)
            # the wsgi process thingy differentiates things that are a generator from things that are not one
            # we need to fix only the generators
            if y and hasattr(y, "__next__"): # web.py uses this to check for generators. A more "safe" way to do it would be to use
                           # inspect.isgenerator(y) or inspect.isgeneratorfunction(y), but like this we ensure
                           # we mimic the behavior of web.py
                return fix_generator(y)
            return y
        return fix

    def add_translation(self, lang, translation):
        self._translations[lang] = translation

    def get_translation_obj(self, lang=None):
        if lang is None:
            lang = self._session.get("language", "")
        return self._translations.get(lang, gettext.NullTranslations())

    def gettext(self, *args, **kwargs):
        return self.get_translation_obj().gettext(*args, **kwargs)

    def get_session(self):
        return self._session

    def init_mapping(self, mapping):
        # The following method is copied from the web/utils.py file in order to fix a problem with python3.7+
        # Due to PEP 479 (https://www.python.org/dev/peps/pep-0479/), Python3.7+ won't accept anymore generators raising
        # StopIteration instead returning.
        def group(seq, size):
            """
            Returns an iterator over a series of lists of length size from iterable.
                >>> list(group([1,2,3,4], 2))
                [[1, 2], [3, 4]]
                >>> list(group([1,2,3,4,5], 2))
                [[1, 2], [3, 4], [5]]
            """
            def take(seq, n):
                for i in range(n):
                    # The except clause is the added part to this method
                    try:
                        yield next(seq)
                    except StopIteration:
                        break

            if not hasattr(seq, 'next'):
                seq = iter(seq)
            while True:
                x = list(take(seq, size))
                if x:
                    yield x
                else:
                    break

        self.mapping = [(r"(/@[a-f0-9A-F_]*@)?" +a, b) for a,b in group(mapping, 2)]

    def add_mapping(self, pattern, classname):
        self.mapping.append((r"(/@[a-f0-9A-F_]*@)?" + pattern, classname))

    def _delegate(self, f, fvars, args=None):
        if args is None:
            args = [None]

        # load session
        if args[0] == "/@@":
            self._session.load('') # creates a new session
            raise web.redirect("/@" + self._session.session_id + "@"+web.ctx.fullpath[3:]) # redirect to the same page, with the new
            # session id
        elif args[0] is None:
            self._session.load(None)
        else:
            self._session.load(args[0][2:len(args[0])-1])

        # Switch language if specified
        input_data = web.input()
        if "lang" in input_data:
            self._session.language = input_data["lang"]
        elif "language" not in self._session:
            for lang in re.split("[,;]+", web.ctx.environ.get("HTTP_ACCEPT_LANGUAGE", "")):
                if lang in self._translations.keys():
                    self._session.language = lang
                    break

        return super(CookieLessCompatibleApplication, self)._delegate(f, fvars, args[1:])

    def get_homepath(self, ignore_session=False, force_cookieless=False):
        """
        :param ignore_session: Ignore the cookieless session_id that should be put in the URL
        :param force_cookieless: Force the cookieless session; the link will include the session_creator if needed.
        """
        if not ignore_session and self._session.get("session_id") is not None and self._session.get("cookieless", False):
            return web.ctx.homepath + "/@" + self._session.get("session_id") + "@"
        elif not ignore_session and force_cookieless:
            return web.ctx.homepath + "/@@"
        else:
            return web.ctx.homepath


class AvoidCreatingSession(Exception):
    """
        allow specific pages (such as SAML auth) to avoid creating a new session.
        this is particularly useful when received cross-site POST request, to which the cookies are not sent...
    """
    def __init__(self, elem):
        self.elem = elem


class CookieLessCompatibleSession:
    """ A session that can either store its session id in a Cookie or directly in the webpage URL.
        The load(session_id) function must be called manually, in order for the session to be loaded.
        This is usually done by the CookieLessCompatibleApplication.

        Original code from web.py (public domain)
    """

    __slots__ = [
        "store", "_initializer", "_last_cleanup_time", "_config", "_data", "_origdata", "_session_id_regex",
        "__getitem__", "__setitem__", "__delitem__"
    ]

    def __init__(self, app, store, initializer=None):
        self.store = store
        self._initializer = initializer
        self._last_cleanup_time = 0
        self._config = utils.storage(web.config.session_parameters)
        self._data = utils.threadeddict()
        self._origdata = utils.threadeddict()
        self._session_id_regex = utils.re_compile('^[0-9a-fA-F]+$')

        self.__getitem__ = self._data.__getitem__
        self.__setitem__ = self._data.__setitem__
        self.__delitem__ = self._data.__delitem__

        if app:
            app.add_processor(self._processor)

    def __contains__(self, name):
        return name in self._data

    def __getattr__(self, name):
        return getattr(self._data, name)

    def __setattr__(self, name, value):
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._data, name, value)

    def __delattr__(self, name):
        delattr(self._data, name)

    def _processor(self, handler):
        """Application processor to setup session for every request"""

        self._cleanup()

        avoid_save = False
        try:
            x = handler()
            if isinstance(x, AvoidCreatingSession):
                avoid_save = True
                return x.elem
            return x
        except web.HTTPError as x:
            if isinstance(x.data, AvoidCreatingSession):
                avoid_save = True
                x.data = x.data.elem
                raise x from x
            raise
        finally:
            if not avoid_save:
                self.save()
            self._data.clear()

    def load(self, session_id=None):
        """ Load the session from the store.
        session_id can be:
        - None: load from cookie
        - '': create a new cookieless session_id
        - a string which is the session_id to be used.
        """

        cookieless = False

        if session_id is None:
            cookie_name = self._config.cookie_name
            self._data["session_id"] = web.cookies().get(cookie_name)
            cookieless = False
        else:
            if session_id == '':
                self._data["session_id"] = None  # will be created
            else:
                self._data["session_id"] = session_id
            cookieless = True

        # protection against session_id tampering
        if self._data["session_id"] and not self._valid_session_id(self._data["session_id"]):
            self._data["session_id"] = None

        self._check_expiry()
        if self._data["session_id"]:
            d = self.store[self._data["session_id"]]
            self._data.update(d)
            self._validate_ip()

        if not self._data["session_id"]:
            self._data.clear() # may have expired. In that case, check that the session is empty.
            self._data["session_id"] = self._generate_session_id()

            if self._initializer:
                if isinstance(self._initializer, dict):
                    self.update(deepcopy(self._initializer))
                elif hasattr(self._initializer, '__call__'):
                    self._initializer()

        self._data["ip"] = web.ctx.ip
        self._data["cookieless"] = cookieless
        self._origdata.update(deepcopy(self._data.__dict__))

    def _check_expiry(self):
        # check for expiry
        if self._data["session_id"] and self._data["session_id"] not in self.store:
            self._data["session_id"] = None

    def _validate_ip(self):
        # check for change of IP
        if self._data["session_id"] and self.get('ip', None) != web.ctx.ip:
            if not self._config.ignore_change_ip or self._data["cookieless"] is True:
                self._data["session_id"] = None

    def save(self):
        cookieless = self._data.get("cookieless", False)
        if not self._data.get('_killed'):
            if self._needs_store_update():
                self.store[self._data["session_id"]] = dict(self._data)
            if not cookieless:
                self._setcookie(self._data["session_id"])
        elif not cookieless:
            self._setcookie(self._data["session_id"], expires=-1)

    def _needs_store_update(self):
        """ Returns whether the data in the session have changed. Prevents (most of the) race conditions """
        return self._data.__dict__ != self._origdata.__dict__

    def _setcookie(self, session_id, expires=None, **kw):
        cookie_name = self._config.cookie_name
        cookie_domain = self._config.cookie_domain
        cookie_path = self._config.cookie_path
        httponly = self._config.httponly
        secure = self._config.secure
        samesite = self._config.samesite
        if expires is None:
            expires = self._config.timeout
        web.setcookie(cookie_name, session_id, expires=expires, domain=cookie_domain, httponly=httponly, secure=secure, path=cookie_path, samesite=samesite)

    def _generate_session_id(self):
        """Generate a random id for session"""

        while True:
            rand = os.urandom(16)
            now = time.time()
            secret_key = self._config.secret_key
            session_id = hashlib.sha1(("%s%s%s%s" % (rand, now, utils.safestr(web.ctx.ip), secret_key)).encode("utf-8"))
            session_id = session_id.hexdigest()
            if session_id not in self.store:
                break
        return session_id

    def _valid_session_id(self, session_id):
        return self._session_id_regex.match(session_id)

    def _cleanup(self):
        """Cleanup the stored sessions"""
        current_time = time.time()
        timeout = self._config.timeout
        if current_time - self._last_cleanup_time > timeout:
            self.store.cleanup(timeout)
            self._last_cleanup_time = current_time

    def expired(self):
        """Called when an expired session is atime"""
        self._data["_killed"] = True
        self.save()
        raise SessionExpired(self._config.expired_message)

    def kill(self):
        """Kill the session, make it no longer available"""
        del self.store[self.session_id]
        self._data["_killed"] = True
