"""Project-wide default pagination.

Every list endpoint returns the DRF envelope ``{count, next, previous,
results}``. Views may still override ``pagination_class`` for a different page
size (e.g. blog posts, fetch errors), but the envelope shape is uniform across
the API — clients can rely on one list contract.
"""
from rest_framework.pagination import PageNumberPagination


class DefaultPageNumberPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200
