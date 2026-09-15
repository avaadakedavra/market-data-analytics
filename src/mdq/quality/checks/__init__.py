"""The shipped quality checks — one module per check, discovered by `pkgutil`.

Nothing is imported here on purpose. `mdq.quality.registry.load_checks()` walks this
package and imports every module it finds, so adding a check means adding a file and
decorating the class with `@register_check` — there is no list to keep in step.
"""
