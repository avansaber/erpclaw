"""Fixture for the static import check: not a real plugin, never imported."""
import erpclaw_sales  # another plugin's package
import erpclaw_sales.service as sales_service  # a module of it
from erpclaw_core import ledger  # core: allowed
from erpclaw_email_folder import deliver_message  # another plugin, by name
from erpclaw_planted import tables  # its own package: allowed

from . import helpers  # relative, its own package: allowed
