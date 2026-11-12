"""Django admin login with the same rate limit as the JWT token views.

The admin form checks the same passwords as ``/api/auth/token/``, so its POSTs
count against the ``login`` throttle scope in the same cache bucket (per
trusted-proxy client IP): the two paths share one budget.
"""

import math

from django.contrib import admin
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_protect

from OtwarteDaneTransportowe.throttling import ScopedRateThrottle


class _LoginScope:
    # ScopedRateThrottle reads only throttle_scope from the view; 'login' is the
    # scope of the token views in jwt_views.py.
    throttle_scope = 'login'


# CSRF first, so a cross-site POST cannot use up a visitor's login budget.
@csrf_protect
def throttled_admin_login(request):
    if request.method == 'POST':
        throttle = ScopedRateThrottle()
        if not throttle.allow_request(request, _LoginScope):
            response = HttpResponse(status=429)
            wait = throttle.wait()
            if wait is not None:
                response['Retry-After'] = str(math.ceil(wait))
            return response
    return admin.site.login(request)
