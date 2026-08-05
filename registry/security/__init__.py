"""Security sub-package: PII scanner and pattern modules.

``queries`` holds the plain, session-taking read/write functions behind the
admin PII-pattern and field-policy endpoints — the tenant-configuration
tables ``registry.api.pii_guard`` reads at scan time, given a home in this
package rather than inline in the admin router.
"""
