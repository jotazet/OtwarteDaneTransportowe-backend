from django.apps import AppConfig

class DataManagerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'data_manager'

    def ready(self):
        # Implicitly connect signal handlers decorated with @receiver.
        import data_manager.signals  # noqa: F401
        # File-cleanup receivers (blog images, feed files, validation reports).
        # Must happen here: importing models from the project package's
        # __init__ raises AppRegistryNotReady and never registers anything.
        import OtwarteDaneTransportowe.cleanup_files  # noqa: F401
