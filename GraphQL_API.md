# GraphQL API for Saathimart

## Overview

Saathimart provides a GraphQL API for flexible, efficient data fetching. This is an alternative to the REST API endpoints.

## Base URL

```
/api/method/saathimart.api.graphql.graphql
```

## Authentication

Most GraphQL queries are guest-accessible. For protected operations (like adding reviews), include:
- `X-Frappe-CSRF-Token`: CSRF token
- `Cookie`: session cookie

## Querying Products

### List Products

```graphql
query GetProducts {
  products(page: 1, page_size: 20, search: "milk", category: "beverages") {
    name
    product_name
    price
    avg_rating
    review_count
    vendor
  }
  total
  page
  page_size
}
```

### Get Single Product

```graphql
query GetProduct {
  product(slug: "SM-DEMO-FRESH-MILK-1L") {
    name
    product_name
    description
    price
    brand {
      name
      brand_name
    }
    category {
      name
      category_name
    }
    avg_rating
    review_count
  }
}
```

## Querying Categories

```graphql
query GetCategories {
  categories {
    name
    category_name
    slug
    image
  }
}
```

## Querying Brands

```graphql
query GetBrands {
  brands {
    name
    brand_name
    slug
    logo
  }
}
```

## Example: Product with Reviews

```graphql
query GetProductWithReviews {
  product(slug: "SM-DEMO-FRESH-MILK-1L") {
    name
    product_name
    price
    reviews {
      name
      rating
      comment
      reviewer_name
    }
  }
}
```

## Schema Documentation

Access the schema at:
```
GET /api/method/saathimart.api.graphql.graphql_schema
```

## Comparison: GraphQL vs REST

| Feature | REST API | GraphQL API |
|---------|----------|-------------|
| Data fetching | Multiple endpoints | Single endpoint |
| Over-fetching | Possible | Not possible |
| Under-fetching | Possible | Not possible |
| Caching | Easy (HTTP caching) | Harder (complex queries) |
| Type safety | Weak | Strong (schema) |

## Best Practices

1. **Use GraphQL for complex queries** - When you need related data (product + reviews + vendor)
2. **Use REST for simple operations** - When you need a single resource
3. **Cache GraphQL responses** - Use Redis caching for repeated queries
4. **Pagination** - Always use page/page_size to avoid loading too much data

## Error Handling

GraphQL errors include:
- `message`: Error description
- `path`: Field path where error occurred
- `locations`: Line/column in the query

```json
{
  "errors": [
    {
      "message": "Product not found",
      "path": ["product"],
      "locations": [{"line": 2, "column": 3}]
    }
  ],
  "data": null
}
```