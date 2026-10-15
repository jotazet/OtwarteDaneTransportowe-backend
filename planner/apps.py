from django.apps import AppConfig


def allow_any_origin_for_public_planner_api(sender, request, **kwargs):
    """django-cors-headers: the public planner API is meant to be called from any site."""
    return request.path_info.startswith('/api/planner/v1/')


class PlannerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'planner'

    def ready(self):
        from corsheaders.signals import check_request_enabled

        check_request_enabled.connect(allow_any_origin_for_public_planner_api)
