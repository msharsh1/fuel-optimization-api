from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"

    def ready(self) -> None:
        """Build the static city index from the DB once at server startup."""
        from core.utils.city_index import get_city_index

        get_city_index().load_from_db()
