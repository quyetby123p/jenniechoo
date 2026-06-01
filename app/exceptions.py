class ConfigError(Exception):
    """Raised when required application configuration is missing or invalid."""


class CommandParseError(Exception):
    """Raised when a Telegram command cannot be parsed."""


class ValidationError(Exception):
    """Raised when command content fails business validation."""


class MetaApiError(Exception):
    """Raised when Meta API request fails."""


class PancakeApiError(Exception):
    """Raised when Pancake POS API request fails."""
