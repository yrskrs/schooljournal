"""Keep data writes separate from PostgreSQL's deferred schema/index operations."""
from django.db import migrations
from apps.journals.services.connection_migration import (
    migrate_legacy_connections, restore_plaintext_legacy_tokens,
)


class Migration(migrations.Migration):
    dependencies = [('journals', '0021_configured_network_ports')]
    operations = [migrations.RunPython(migrate_legacy_connections, restore_plaintext_legacy_tokens)]
