"""GraphQL endpoint for Saathimart.

Provides a GraphQL API for frontend consumption. Requires the `graphql-core`
package (pip install graphql-core). If the package is not installed the
endpoint returns a clear error instead of crashing at import time.
"""

import frappe
from frappe import _
from frappe.utils import cint

try:
    from graphql import graphql as execute_graphql
    from saathimart.graphql.schema import schema

    _HAS_GRAPHQL = True
except ImportError:
    _HAS_GRAPHQL = False


@frappe.whitelist(allow_guest=True)
def graphql_endpoint(query, variables=None, operation_name=None):
    """Execute a GraphQL query.

    Args:
        query: GraphQL query string (required)
        variables: Optional JSON-encoded variables dict
        operation_name: Optional operation name for named operations

    Returns:
        dict with ``data`` and optional ``errors`` keys.
    """
    if not _HAS_GRAPHQL:
        frappe.throw(
            _("GraphQL support requires the ``graphql-core`` package. "
              "Install it with ``pip install graphql-core``."),
            title=_("GraphQL Not Available"),
        )

    import json as _json

    # Parse variables
    vars_dict = {}
    if variables:
        try:
            vars_dict = _json.loads(variables)
        except _json.JSONDecodeError:
            frappe.throw(_("Invalid JSON in variables"))

    # Build context
    context = {"frappe": frappe}

    # Execute query
    result = execute_graphql(
        schema=schema,
        source=query,
        variable_values=vars_dict,
        operation_name=operation_name,
        context_value=context,
    )

    response = {"data": result.data}

    if result.errors:
        response["errors"] = [
            {
                "message": str(error),
                "path": list(error.path) if error.path else None,
                "locations": [
                    {"line": loc.line, "column": loc.column}
                    for loc in (error.locations or [])
                ],
            }
            for error in result.errors
        ]

    return response


@frappe.whitelist(allow_guest=True)
def graphql_schema():
    """Get the GraphQL schema introspection result."""
    if not _HAS_GRAPHQL:
        frappe.throw(
            _("GraphQL support requires the ``graphql-core`` package."),
            title=_("GraphQL Not Available"),
        )

    return {
        "query": str(schema.query),
        "types": [str(t) for t in schema.types.values()],
    }
