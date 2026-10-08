class NotConfiguredError(RuntimeError):
    """Raised when an integration is called before its credentials are set.

    Deliberately loud: for anything touching money or tax filings, silently
    returning mock data in an unconfigured environment is worse than failing.
    """

    def __init__(self, integration: str, missing: list[str]):
        self.integration = integration
        self.missing = missing
        super().__init__(
            f"{integration} is not configured. Missing env vars: {', '.join(missing)}"
        )


class UpstreamError(RuntimeError):
    """Raised when a configured external API call fails."""
