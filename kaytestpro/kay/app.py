# -*- coding: utf-8 -*-

"""
Kay application.

:Copyright: (c) 2009 Accense Technology, Inc. 
                     Takashi Matsuo <tmatsuo@candit.jp>,
                     All rights reserved.
:license: BSD, see LICENSE for more details.
"""

import sys
import os
import logging

from werkzeug import (
  Request, ClosingIterator, DispatcherMiddleware,
)
from werkzeug.exceptions import (
  HTTPException, InternalServerError, NotFound
)
from werkzeug import (
  Response, redirect
)
from werkzeug.routing import (
  Map, Submount, RequestRedirect, EndpointPrefix
)
from werkzeug.utils import import_string
from werkzeug.urls import url_encode
from jinja2 import (
  Environment, Undefined,
)
from google.appengine.runtime.apiproxy_errors import CapabilityDisabledError

import kay
from kay.utils import (
  local, local_manager, reverse, render_to_string, render_error,
)
from kay import (
  utils, exceptions, mail,
)

from kay.conf import settings, _settings, LazySettings

translations_cache = {}
hook_installed = False

def get_application(settings=_settings):
  global hook_installed
  hook_installed = False
  application = KayApp(settings)
  submount_apps = {}
  for app_name in settings.SUBMOUNT_APPS:
    app = KayApp(LazySettings('%s.settings' % app_name))
    submount_apps['/%s' % app_name] = app
  if settings.JSONRPC2_MOUNT_POINT:
    app_name = settings.JSONRPC2_MOUNT_POINT
    import kay.ext.jsonrpc2 as jsonrpc2
    app = jsonrpc2.make_application(getattr(settings, "JSONRPC2_METHODS", {}))
    submount_apps['/%s' % app_name] = app
  application = DispatcherMiddleware(application, submount_apps)
  return application


class NullUndefined(Undefined):
  """
  Do nothing except for logging when the specified variable doesn't exist.
  """
  __slots__ = ()
  def __int__(self):
    return 0
  def __float__(self):
    return 0.0
  def __getattr__(self, value):
    return u''
  def __html__(self):
    self.warn()
    return u''
  def warn(self, warning_template="%s: %s is undefined."):
    f = sys._getframe(1)
    while not 'templates' in f.f_code.co_filename:
      f = f.f_back
    logging.warn(warning_template %
                 (f.f_code.co_filename, self._undefined_name))


def get_app_tailname(app):
  dot = app.rfind('.')
  if dot >= 0:
    return app[dot+1:]
  else:
    return app


class KayApp(object):

  def __init__(self, app_settings):
    self.app_settings = app_settings
    self.url_map = None
    self.views = None
    self._request_middleware = self._response_middleware = \
        self._view_middleware = self._exception_middleware = None
    self.auth_backend = None
    self.use_ereporter = 'kay.ext.ereporter' in app_settings.INSTALLED_APPS
    self.init_ereporter()

  @property
  def jinja2_env(self):
    if not hasattr(self, '_jinja2_env'):
      self.init_jinja2_environ()

    if hasattr(self, 'active_translations'):
      if self.app_settings.USE_I18N:
        self._jinja2_env.install_gettext_translations(self.active_translations)
      else:
        self._jinja2_env.globals.update(
          _=lambda x: x,
          gettext=lambda x: x,
          ngettext=lambda s, p, n: (n != 1 and (p,) or (s,))[0]
        )
    return self._jinja2_env

  def get_mount_point(self, app):
    if app == 'kay._internal':
      return '/_kay'
    if self.app_settings.APP_MOUNT_POINTS.has_key(app):
      return self.app_settings.APP_MOUNT_POINTS.get(app)
    else:
      try:
        app_mod = import_string(app)
        return app_mod.mount_point
      except Exception:
        return '/%s' % get_app_tailname(app)

  def get_installed_apps(self):
    return self.app_settings.INSTALLED_APPS+['kay._internal']

  def init_url_map(self, url_module):
    self.has_error_on_init_url_map = False
    mod = import_string(url_module)
    if hasattr(mod, 'view_groups'):
      base_rules = []
      views = {}
      for view_group in getattr(mod, 'view_groups'):
        try:
          base_rules = base_rules + view_group.get_rules()
          views.update(view_group.get_views())
        except Exception, e:
          logging.error("Failed to mount ViewGroup: %s", e)
          raise
      import copy
      url_map = Map(copy.deepcopy(base_rules))
    else:
      make_url = getattr(mod, 'make_url')
      all_views = getattr(mod, 'all_views')
      views = all_views
      url_map = make_url()
    for app in self.get_installed_apps():
      mountpoint = self.get_mount_point(app)
      if mountpoint is None:
        logging.debug("Mountpoint for app '%s' is set to None explicitly,"
                      " skipped." % app)
        continue
      try:
        url_mod = import_string("%s.%s" % (app, url_module))
      except (ImportError, AttributeError):
        try:
          url_mod = import_string("%s.urls" % app)
        except (ImportError, AttributeError):
          logging.error("Failed to import app '%s.urls'." % app)
          logging.debug("Reason:\n%s" % self._get_traceback(sys.exc_info()))
          raise
      rules = []
      if hasattr(url_mod, 'view_groups'):
        for view_group in getattr(url_mod, 'view_groups'):
          try:
            endpoint_prefix = app.split(".")[-1]
            rules = rules + view_group.get_rules(endpoint_prefix)
            views.update(view_group.get_views(endpoint_prefix))
          except Exception, e:
            logging.error("Failed to mount ViewGroup: %s", e)
            raise
      else:
        make_rules = getattr(url_mod, 'make_rules', None)
        if make_rules:
          rules = make_rules()
        all_views = getattr(url_mod, 'all_views', None)
        if all_views:
          views.update(all_views)
      url_map.add(Submount(mountpoint, rules))
    # TODO move the block bellow to somewhere else
    if 'kay.auth.middleware.AuthenticationMiddleware' in \
          self.app_settings.MIDDLEWARE_CLASSES:
      try:
        klass = import_string(self.app_settings.AUTH_USER_BACKEND)
      except (AttributeError, ImportError), e:
        raise exceptions.ImproperlyConfigured, \
            'Failed to import %s: "%s".' %\
            (self.app_settings.AUTH_USER_BACKEND, e)
      self.auth_backend = klass()
    self.views = views
    self.url_map = url_map

  def init_ereporter(self):
    if self.use_ereporter:
      import logging
      from google.appengine.ext import ereporter

      # Logging handlers are global so we need to make sure the logger
      # isn't registered already.
      logger = logging.getLogger()
      for handler in logger.handlers:
        if isinstance(handler, ereporter.ExceptionRecordingHandler):
          return
      ereporter.register_logger()

  def init_jinja2_environ(self):
    """
    Initialize the environment for jinja2.
    """
    from jinja2 import (FileSystemLoader, ChoiceLoader, PrefixLoader,)
    per_app_loaders = {}
    for app in self.get_installed_apps():
      try:
        mod = import_string(app)
      except (ImportError, AttributeError):
        logging.warning("Failed to import app '%s', skipped." % app)
        continue
      try:
        app_key = getattr(mod, 'template_loader_key')
      except AttributeError:
        app_key = get_app_tailname(app)
      per_app_loaders[app_key] = FileSystemLoader(
        os.path.join(os.path.dirname(mod.__file__), "templates"))
    loader = PrefixLoader(per_app_loaders)
    target_dirs = self.app_settings.TEMPLATE_DIRS+("kay/templates",)
    import kay
    base_loader = FileSystemLoader(
      [os.path.join(kay.PROJECT_DIR, d) for d in target_dirs])
    loader = ChoiceLoader([base_loader, loader])
    env_dict = {}
    env_dict.update(self.app_settings.JINJA2_ENVIRONMENT_KWARGS)
    jinja2_ext = []
    for ext_str in self.app_settings.JINJA2_EXTENSIONS:
      try:
        ext = import_string(ext_str)
      except (ImportError, AttributeError), e:
        logging.warn('Failed to import jinja2 extension %s: "%s", skipped.'
                     % (ext_str, e))
        continue
      jinja2_ext.append(ext)
    env_dict.update(dict(loader = loader, undefined=NullUndefined,
                         extensions=jinja2_ext))
    self._jinja2_env = Environment(**env_dict)
    for key, filter_str in self.app_settings.JINJA2_FILTERS.iteritems():
      try: 
        func = import_string(filter_str)
      except (ImportError, AttributeError):
        logging.warn('Cannot import %s.' % filter_str)
        continue
      if self._jinja2_env.filters.has_key(key):
        logging.warn('Key "%s" has already defined, skipped.' % key)
        continue
      if not callable(func):
        logging.warn('%s is not a callable.' % filter_str)
        continue
      self._jinja2_env.filters[key] = func

  def load_middleware(self):
    self._response_middleware = []
    self._view_middleware = []
    self._exception_middleware = []
    request_middleware = []
    for mw_path in self.app_settings.MIDDLEWARE_CLASSES:
      try:
        mw_class = import_string(mw_path)
      except (ImportError, AttributeError), e:
        raise exceptions.ImproperlyConfigured, \
            '%s isn\'t a valid middleware module: "%s"' % (mw_path, e)
      try:
        mw_instance = mw_class()
      except exceptions.MiddlewareNotUsed:
        continue

      if hasattr(mw_instance, 'process_request'):
        request_middleware.append(mw_instance.process_request)
      if hasattr(mw_instance, 'process_view'):
        self._view_middleware.append(mw_instance.process_view)
      if hasattr(mw_instance, 'process_response'):
        self._response_middleware.insert(0, mw_instance.process_response)
      if hasattr(mw_instance, 'process_exception'):
        self._exception_middleware.insert(0, mw_instance.process_exception)

    # We only assign to this when initialization is complete as it is used
    # as a flag for initialization being complete.
    self._request_middleware = request_middleware

  def get_response(self, request):
    global translations_cache
    if self.app_settings.USE_I18N:
      from kay.i18n import load_translations
      from kay.i18n import get_language_from_request
      lang = get_language_from_request(request)
      if not lang:
        lang = self.app_settings.DEFAULT_LANG
      translations = translations_cache.get("trans:%s:%s" %
                                            (self.app_settings.APP_NAME, lang),
                                            None)
      if translations is None:
        translations = load_translations(lang)
        translations_cache["trans:%s:%s" %
                     (self.app_settings.APP_NAME, lang)] = translations
      self.active_translations = translations
    else:
      from kay.i18n import KayNullTranslations
      lang = None
      self.active_translations = KayNullTranslations()
    request.lang = lang

    if self._request_middleware is None:
      self.load_middleware()
    try:
      try:
        endpoint, values = local.url_adapter.match()
      except RequestRedirect, e:
        if request.args:
          e.new_url += '?' + url_encode(request.args)
        raise e
      if self.app_settings.IS_MARKETPLACE_APP:
        if values.has_key(settings.MARKETPLACE_DOMAIN_NAME_KEY):
          setattr(request, settings.MARKETPLACE_DOMAIN_NAME_KEY,
                  values[settings.MARKETPLACE_DOMAIN_NAME_KEY])
      # apply request middleware
      for mw_method in self._request_middleware:
        response = mw_method(request)
        if response:
          return response
      view_func = self.views.get(endpoint, None)
      try:
        if isinstance(view_func, tuple):
          view_classname, args, kwargs = view_func
          view_cls = import_string(view_classname)
          view_func = view_cls(*args, **kwargs)
        elif isinstance(view_func, basestring):
          view_func = import_string(view_func)
        assert(callable(view_func))
      except StandardError, e:
          logging.error(self._get_traceback(sys.exc_info()))
          raise InternalServerError(e)
      for mw_method in self._view_middleware:
        response = mw_method(request, view_func, **values)
        if response:
          return response
      try:
        response = view_func(request, **values)
      except Exception, e:
        # If the view raised an exception, run it through exception
        # middleware, and if the exception middleware returns a
        # response, use that. Otherwise, reraise the exception.
        for middleware_method in self._exception_middleware:
          response = middleware_method(request, e)
          if response:
            return response
        raise
    except RequestRedirect, e:
      response = e.get_response(None)
    except HTTPException, e:
      logging.warning(e)
      response = render_error(e)
    except SystemExit:
      # Allow sys.exit() to actually exit.
      raise
    except CapabilityDisabledError, e:
      from kay.i18n import gettext as _
      logging.error(e)
      # Saving session will also fail.
      if hasattr(request, 'session'):
        del(request.session)
      return Response(
        render_to_string(
          "_internal/maintenance.html",
          {"message": _('Appengine might be under maintenance.')}),
        content_type="text/html; charset=utf-8",
        status=503)
    except Exception:
      # Handle everything else, including SuspiciousOperation, etc.
      # Get the exception info now, in case another exception is thrown later.
      exc_info = sys.exc_info()
      return self.handle_uncaught_exception(request, exc_info)
    return response

  def handle_uncaught_exception(self, request, exc_info):
    import os
    if 'SERVER_SOFTWARE' in os.environ and \
          os.environ['SERVER_SOFTWARE'].startswith('Dev') and \
          self.app_settings.DEBUG:
      # It's intended to invoke werkzeug's debugger
      raise
    else:
      try:
        from kay.utils import repr
        request_repr = repr.dump(request)
      except Exception, e:
        request_repr = "Request repr() unavailable"
      message = "%s\n\n%s" % (self._get_traceback(exc_info), request_repr)

      if self.use_ereporter:
        logging.exception("An Unhandled Exception Occurred.")
      else:
        logging.error(message)

      if self.app_settings.DEBUG:
        error = InternalServerError(message.replace("\n", "<br/>\n"))
        return error.get_response(request.environ)
      else:
        if not self.use_ereporter:
          subject = 'Error %s: %s' % (request.remote_addr, request.path)
          mail.mail_admins(subject, message, fail_silently=True)
        # TODO: Return an HttpResponse that displays a friendly error message.
        return InternalServerError().get_response(request.environ)

  def _get_traceback(self, exc_info):
    "Helper function to return the traceback as a string"
    import traceback
    ret = '\n'.join(traceback.format_exception(*(exc_info or sys.exc_info())))
    try:
      return ret.decode('utf-8')
    except UnicodeDecodeError:
      return ret

  def _prepare(self, environ):
    kay.setup_syspath()
    if _settings.USE_DB_HOOK:
      global hook_installed
      if not hook_installed:
        from google.appengine.api import apiproxy_stub_map
        from kay.utils.db_hook import post_hook
        from kay.utils.db_hook import pre_hook
        apiproxy_stub_map.apiproxy.GetPostCallHooks().Append(
          'post_hook', post_hook, 'datastore_v3')
        apiproxy_stub_map.apiproxy.GetPreCallHooks().Append(
          'pre_hook', pre_hook, 'datastore_v3')
        hook_installed = True
    local.app = self
    if self.url_map is None or self.has_error_on_init_url_map:
      try:
        self.init_url_map(self.app_settings.ROOT_URL_MODULE)
      except (StandardError, exceptions.ImproperlyConfigured):
        self.has_error_on_init_url_map = True
        raise
    local.url_adapter = self.url_map.bind_to_environ(environ)

  def __call__(self, environ, start_response):

    self._prepare(environ)

    local.request = request = Request(environ)
    response = self.get_response(request)

    for middleware_method in self._response_middleware:
      response = middleware_method(request, response)
    if hasattr(local, "override_headers") and hasattr(response, "headers"):
      response.headers.extend(getattr(local, "override_headers"))
      del local.override_headers
    if hasattr(local, "override_cookies") and hasattr(response, "set_cookie"):
      for d in local.override_cookies:
        key = d.pop("key")
        response.set_cookie(key, **d)
      del local.override_cookies
    return ClosingIterator(response(environ, start_response),
        [local_manager.cleanup])

