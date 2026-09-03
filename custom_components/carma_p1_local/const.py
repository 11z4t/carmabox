DOMAIN = "carma_p1_local"
# Matches the value actually set in every live site's YAML (scan_interval: 1,
# per Börje 2026-08-24) - this is only the fallback used if a site's config
# omits scan_interval entirely, not the deployed value itself (PLAT-1975 QC, 901).
DEFAULT_SCAN_INTERVAL_S = 1
