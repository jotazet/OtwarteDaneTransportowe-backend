from django.apps import AppConfig

class DataManagerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'data_manager'

    def ready(self):
        # Implicitly connect signal handlers decorated with @receiver.
        import data_manager.signals
