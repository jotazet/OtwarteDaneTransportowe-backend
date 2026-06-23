from __future__ import annotations

from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView


class RolesInTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        # Frontend-oriented claim: list of Django group names ("roles").
        # Kept simple on purpose: backend remains the source of truth for permissions.
        if getattr(user, "is_authenticated", False):
            token["roles"] = sorted(user.groups.values_list("name", flat=True))
            # Profile fields for the frontend (also present after token refresh).
            token["first_name"] = (getattr(user, "first_name", None) or "").strip()
            token["last_name"] = (getattr(user, "last_name", None) or "").strip()
        else:
            token["roles"] = []
            token["first_name"] = ""
            token["last_name"] = ""

        return token


class RolesInTokenObtainPairView(TokenObtainPairView):
    serializer_class = RolesInTokenObtainPairSerializer
    # Rate-limit credential submission (brute-force protection); see
    # REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['login'].
    throttle_scope = 'login'


class ThrottledTokenRefreshView(TokenRefreshView):
    throttle_scope = 'login'

