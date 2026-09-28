"""Label Studio settings for the PyroMind deployment.

Inherits the stock settings and redirects two of Label Studio's own extension
hooks at this package, which is what gives every portal user a private
workspace. No Label Studio source file is modified.

Selected with DJANGO_SETTINGS_MODULE=pyromind_ls.settings.

Both hook names are read once at import time by Label Studio
(core/settings/base.py), so a missing or renamed hook shows up as an import
error at startup rather than as silently unisolated projects.

It also widens the cookie domain so the portal's SSO handoff leaves one
session cookie instead of two; see the note below the hooks.
"""

# core.settings.label_studio calls sentry.init_sentry() while it is being
# imported, and that reads settings.SENTRY_DSN. Django answers by building a
# Settings object from this module, which is still half-initialised at that
# point. Re-exporting base first is what gives that inner build the names it
# needs; the outer build then runs to completion and replaces it.
from core.settings.base import *  # noqa: F403
from core.settings.label_studio import *  # noqa: F403

SAVE_USER = 'pyromind_ls.isolation.save_user'
USER_SERIALIZER_UPDATE = 'pyromind_ls.isolation.UserSerializerUpdate'

# The portal logs the browser into Label Studio by handing it Label Studio's
# own session cookie, set for the whole pyromind.ai domain. Label Studio never
# sets SESSION_COOKIE_DOMAIN -- the name appears nowhere in its source -- so
# its cookie is host-only and the browser holds two sessionid cookies that
# drift apart, until it is bounced to a login page it cannot pass (a
# portal-managed account's password is derived from the SSO secret). Matching
# the portal's domain makes Label Studio replace that cookie instead.
# Keep in sync with the portal's label_studio cookie_domain.
SESSION_COOKIE_DOMAIN = '.pyromind.ai'
CSRF_COOKIE_DOMAIN = '.pyromind.ai'

# 导出钩子要替换 Label Studio 模型上的方法，而设置加载期间不能 import 模型，
# 所以它挂在自己的 AppConfig.ready() 里（见 apps.py / export_hook.py）。
INSTALLED_APPS = [*INSTALLED_APPS, 'pyromind_ls']

# Label Studio 的根 URLconf 以若干个 catch-all include 收尾，插件的路由必须排在
# 它们之前，所以这里换成插件自己的 urls.py，由它把原生 urlpatterns 接在后面。
ROOT_URLCONF = 'pyromind_ls.urls'

# 未登录的页面请求改交给本站的 SSO 入口（见 sso.py）。排在导出中间件之前，
# 让跳转在进入视图之前发生，同时保住下面那条"导出中间件最靠内"的约束。
# 导出交互：把 Label Studio 自己的导出弹窗换成 PyroMind 的（见 export_ui.py）。
# 它要替换响应正文，必须拿到已经渲染完成的 HTML，而最内层中间件是响应回流时
# 最先拿到它的那个，所以它必须留在列表最后。
MIDDLEWARE = [
    *MIDDLEWARE,
    'pyromind_ls.sso.PortalSsoMiddleware',
    'pyromind_ls.export_ui.ExportUiMiddleware',
]
