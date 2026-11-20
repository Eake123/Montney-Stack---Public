__version__ = "0.1.0"

# Resolve time zones from the `tzdata` package only (see pyproject.toml): a stale OS database
# would still apply the pre-2026 Alberta DST rule after the 2026-11-01 switch to permanent UTC-6.
import zoneinfo as _zoneinfo

_zoneinfo.reset_tzpath(to=[])
