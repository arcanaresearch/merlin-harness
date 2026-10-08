class PackError(ValueError):
    """Base error for invalid or unavailable declarative packs."""


class PackFormatError(PackError):
    pass


class PackNotFoundError(PackError):
    pass


class DuplicatePackError(PackError):
    pass
