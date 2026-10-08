class SkillError(ValueError):
    """Base error for invalid or unavailable skill bundles."""


class SkillFormatError(SkillError):
    pass


class SkillNotFoundError(SkillError):
    pass


class DuplicateSkillError(SkillError):
    pass


class SkillReferenceError(SkillError):
    pass
