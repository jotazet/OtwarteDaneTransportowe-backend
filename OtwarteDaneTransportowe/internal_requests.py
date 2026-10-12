"""Requests from ODT's own services over the internal Docker network.

The journey planner's OpenTripPlanner polls proxied realtime files from this
app every few seconds, from one address; it identifies itself with a shared
token so those polls are not throttled like public downloads.
"""

import hmac

from django.conf import settings

INTERNAL_TOKEN_HEADER = 'X-ODT-Internal-Token'


def has_internal_token(request) -> bool:
    token = settings.PLANNER_INTERNAL_TOKEN
    sent = request.headers.get(INTERNAL_TOKEN_HEADER, '')
    return bool(token and sent and hmac.compare_digest(sent, token))
