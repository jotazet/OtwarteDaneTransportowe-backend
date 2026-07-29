from django.apps import AppConfig

class BlogConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'blog'

    def ready(self):
        # Deployment check: IP-based reaction limits break silently without
        # TRUSTED_PROXY_CIDRS behind a reverse proxy.
        import blog.checks  # noqa: F401
