from django.db.backends.mysql.base import DatabaseWrapper as MySQLDatabaseWrapper

from .features import DatabaseFeatures


class DatabaseWrapper(MySQLDatabaseWrapper):
    # XAMPP on this workstation ships MariaDB 10.4.x, which is sufficient for
    # the project queries and schema even though Django 6 raises its built-in
    # MariaDB floor to 10.5.
    features_class = DatabaseFeatures
