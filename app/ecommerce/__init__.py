"""Sparton Intelligence — the product domain.

This is the code a customer actually pays for. The loop is:

    add shop URL
      -> discover competitors           (ecommerce/discovery.py)
      -> crawl them on a schedule       (ecommerce/crawl.py)
      -> detect what changed            (ecommerce/changes.py)
      -> write a report about it        (ecommerce/reports.py)

Everything here is tenant-scoped: every function that touches the database
takes ``organization_id`` explicitly, and every query filters on it. That is not
decorative — `TenantContext.scoped` deliberately returns *unscoped* queries for
machine principals, so product code must never rely on it.
"""
