"""Listing fetchers for each supported 3D-model site, one module per site.

Each module exposes a single fetch_listing(...) entry point with the same shape:
takes the source's configured URL plus paging/cancel/progress hooks, and returns
a list of (model_url, title, image_url, created_at) tuples. app.py dispatches to
the right module by the source's parser_type.
"""
